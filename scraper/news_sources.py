"""
Чистые функции (без сети) для сборки и разметки кандидатов в новостную ленту
"Новости"/"Возможности". Сеть дёргает update_news.py — сюда он передаёт уже
скачанные и распарсенные rss_lite.parse() записи (обычные dict'ы), поэтому
все функции здесь легко тестировать оффлайн (см. test_news.py).
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta, timezone

ALMATY_TZ = timezone(timedelta(hours=5))  # Казахстан — без перехода на летнее время


def almaty_today() -> date:
    return datetime.now(ALMATY_TZ).date()


def almaty_yesterday() -> date:
    return almaty_today() - timedelta(days=1)


# ---------------------------------------------------------------------------
# Список RSS-лент по регионам/темам. Это ПЕРВАЯ версия списка — часть URL
# подтверждена вручную (CNBC, Интерфакс, OilPrice.com — реально открыты и
# проверены на живой контент 2026-08-20), часть добавлена по общеизвестным
# адресам без прямой проверки. update_news.py оборачивает каждую ленту в
# try/except и молча пропускает недоступную (см. её лог "лент не удалось
# прочитать") — так что битая ссылка не роняет весь прогон, а видна в логах
# GitHub Actions, и список можно поправить по факту первого реального прогона,
# как это было со скрапером ВВП.
# ---------------------------------------------------------------------------
FEEDS = [
    # -- США / международные деловые ------------------------------------------------
    {"name": "CNBC (US Top News)", "region": "us", "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html"},
    {"name": "CNBC (World Economy)", "region": "us", "url": "https://www.cnbc.com/id/10000109/device/rss/rss.html"},
    {"name": "MarketWatch (Top Stories)", "region": "us", "url": "https://feeds.marketwatch.com/marketwatch/topstories/"},
    # -- Европа -----------------------------------------------------------------------
    {"name": "Deutsche Welle (Business)", "region": "eu", "url": "https://rss.dw.com/rdf/rss-en-bus"},
    {"name": "Euronews (Business)", "region": "eu", "url": "https://www.euronews.com/rss?level=theme&name=business"},
    # -- Россия -----------------------------------------------------------------------
    {"name": "Интерфакс", "region": "ru", "url": "https://www.interfax.ru/rss.asp", "category_filter": "Экономика"},
    # -- Китай / Азия -------------------------------------------------------------------
    {"name": "South China Morning Post (Business)", "region": "cn", "url": "https://www.scmp.com/rss/92/feed"},
    {"name": "Japan Times (Business)", "region": "asia", "url": "https://www.japantimes.co.jp/news_category/business/feed/"},
    # -- Сырьевые товары ----------------------------------------------------------------
    {"name": "OilPrice.com", "region": "commodities", "url": "https://oilprice.com/rss/main"},
    {"name": "Kitco News (Metals)", "region": "commodities", "url": "https://www.kitco.com/rss/KitcoNews.xml"},
    # -- Центробанки --------------------------------------------------------------------
    {"name": "Federal Reserve (Press Releases)", "region": "central_banks", "url": "https://www.federalreserve.gov/feeds/press_all.xml"},
    {"name": "European Central Bank (Press)", "region": "central_banks", "url": "https://www.ecb.europa.eu/rss/press.html"},
]


def entry_to_candidate(entry: dict, source_name: str, source_region: str) -> dict | None:
    """Приводит одну запись rss_lite.parse() (dict с title/link/summary/
    published/image_url/categories) к единому формату кандидата. None, если
    не хватает обязательных полей (без заголовка или ссылки использовать
    запись невозможно)."""
    title = (entry.get("title") or "").strip()
    link = (entry.get("link") or "").strip()
    if not title or not link:
        return None

    summary = (entry.get("summary") or "").strip()
    summary = re.sub(r"<[^>]+>", " ", summary)  # на случай литеральных тегов внутри CDATA-описания
    summary = re.sub(r"\s+", " ", summary).strip()

    return {
        "title": title,
        "summary": summary[:600],
        "link": link,
        "image_url": entry.get("image_url"),
        "published": entry.get("published"),
        "source_name": source_name,
        "region": source_region,
        "categories": entry.get("categories") or [],
    }


def is_from_almaty_date(candidate: dict, target_date: date) -> bool:
    """True, если новость опубликована в течение указанного календарного дня
    по времени Алматы (используется, чтобы отобрать «новости за вчера»)."""
    published = candidate.get("published")
    if not published:
        return False
    try:
        dt = datetime.fromisoformat(published)
    except ValueError:
        return False
    return dt.astimezone(ALMATY_TZ).date() == target_date


def stable_id(candidate: dict) -> str:
    return hashlib.sha1(candidate["link"].encode("utf-8")).hexdigest()[:12]


def dedupe(candidates: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for c in candidates:
        if c["link"] in seen:
            continue
        seen.add(c["link"])
        out.append(c)
    return out
