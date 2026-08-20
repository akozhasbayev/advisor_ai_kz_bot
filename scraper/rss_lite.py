"""
Минимальный RSS 2.0 / RSS 1.0 (RDF) / Atom парсер на одной xml.etree —
без сторонних пакетов вроде feedparser.

Почему не feedparser: sandbox, в котором пишется и тестируется этот код, не
имеет доступа к PyPI кроме узкого белого списка пакетов (feedparser и
anthropic в него не входят) — а значит, поведение с feedparser нельзя было
бы реально протестировать до первого прогона в GitHub Actions, где сеть уже
не ограничена. xml.etree.ElementTree — часть стандартной библиотеки, есть
и здесь, и на раннерах GitHub Actions — значит, поведение можно проверить
офлайн прямо сейчас (см. test_news.py) и не открывать новый источник
сюрпризов вроде тех, что были со скрапером ВВП (gov.kz).

Не претендует на поддержку всего RSS/Atom — вытаскивает только то, что
нужно update_news.py: заголовок, ссылку, краткое описание, дату публикации,
картинку (enclosure / media:thumbnail / media:content) и категории.
"""
from __future__ import annotations

import email.utils as eut
import xml.etree.ElementTree as ET
from datetime import datetime, timezone


class FeedParseError(Exception):
    pass


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _text(elem) -> str:
    return "".join(elem.itertext()).strip() if elem is not None else ""


def _direct_child(elem, name):
    for child in elem:
        if _local(child.tag) == name:
            return child
    return None


def _first_of(elem, names):
    """Как _direct_child, но пробует несколько имён по порядку и
    останавливается на первом найденном. НЕ через `a or b or c` — Element
    с текстом, но без дочерних элементов (например, <description>текст
    без тегов внутри</description>) считается "ложным" в bool(), потому
    что Element.__bool__ исторически завязан на len() (число ДОЧЕРНИХ
    элементов), а не на наличие текста. `_direct_child(...) or ...`
    из-за этого молча пропускал бы такие поля."""
    for name in names:
        child = _direct_child(elem, name)
        if child is not None:
            return child
    return None


def _direct_children(elem, name):
    return [child for child in elem if _local(child.tag) == name]


def _parse_date(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    # RFC 822 (RSS): "Wed, 19 Aug 2026 10:00:00 +0000"
    try:
        dt = eut.parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        pass
    # ISO 8601 (Atom): "2026-08-19T10:00:00Z" / "2026-08-19T10:00:00+05:00"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def _entry_link(elem) -> str | None:
    # RSS: <link>текст-ссылки</link>
    link_el = _direct_child(elem, "link")
    if link_el is not None:
        text = _text(link_el)
        if text:
            return text
        href = link_el.get("href")  # изредка Atom-стиль <link href="..."/> внутри RSS
        if href:
            return href
    # Atom: несколько <link rel="..." href="..."/>, берём rel=alternate или первый с href
    candidates = [c for c in elem if _local(c.tag) == "link"]
    alt = [c.get("href") for c in candidates if c.get("rel") in (None, "alternate") and c.get("href")]
    if alt:
        return alt[0]
    any_href = [c.get("href") for c in candidates if c.get("href")]
    return any_href[0] if any_href else None


def _entry_image(elem) -> str | None:
    for child in elem.iter():
        if child is elem:
            continue
        local = _local(child.tag)
        if local in ("thumbnail", "content") and child.get("url"):
            medium_or_type = (child.get("medium", "") + child.get("type", "")).lower()
            if local == "thumbnail" or "image" in medium_or_type:
                return child.get("url")
        if local == "enclosure" and child.get("url") and str(child.get("type", "")).startswith("image/"):
            return child.get("url")
    return None


def _entry_categories(elem) -> list[str]:
    out = []
    for child in _direct_children(elem, "category"):
        term = child.get("term") or _text(child)
        if term:
            out.append(term)
    return out


def _parse_entry(elem) -> dict:
    title = _text(_direct_child(elem, "title"))
    summary_el = _first_of(elem, ("description", "summary", "content"))
    pub_el = _first_of(elem, ("pubDate", "published", "updated", "date"))
    return {
        "title": title,
        "link": _entry_link(elem) or "",
        "summary": _text(summary_el),
        "published": _parse_date(_text(pub_el)) if pub_el is not None else None,
        "image_url": _entry_image(elem),
        "categories": _entry_categories(elem),
    }


def parse(raw) -> list[dict]:
    """Разобрать RSS 2.0 / RSS 1.0 (RDF) / Atom из байтов или строки.

    Бросает FeedParseError при некорректном XML — вызывающий код
    (update_news.py) сам решает, что делать (пропустить ленту), см. его
    try/except по каждой ленте."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise FeedParseError(str(e)) from e

    item_tag = "entry" if _local(root.tag) == "feed" else "item"
    entries = [el for el in root.iter() if _local(el.tag) == item_tag]
    return [_parse_entry(el) for el in entries]
