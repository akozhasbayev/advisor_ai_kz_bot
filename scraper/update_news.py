"""
Сборщик "Новости" (топ-5 за вчера) и "Возможности" (1-5, без натяжки) для
Telegram Mini App. В отличие от update_data.py (Playwright, конкретные
JS-сайты), этот модуль читает обычные RSS-ленты через urllib — им не нужен
браузер, и они гораздо стабильнее вырезания текста из JS-сайтов, поэтому
здесь нет Playwright.

Пайплайн:
  1. Скачать все ленты из news_sources.FEEDS, собрать кандидатов, оставить
     только опубликованные "вчера" по времени Алматы (UTC+5, без перехода
     на летнее время).
  2. Один запрос к Claude (structured output через tool_choice) — выбрать
     ровно 5 новостей, самых значимых для Казахстана, с русским пересказом
     и оригинальным анализом влияния на РК под каждой.
  3. Второй такой же запрос — но "Возможности": от 0 до 5, не натягивая
     количество, если действительно интересных меньше.
  4. Подставить обратно image_url и ссылку на первоисточник из исходного
     кандидата (сама модель их не придумывает, только ссылается на id).
  5. Записать news.json / opportunities.json — перезаписываются, только
     если реально изменились (как data.json у update_data.py).

Требует переменную окружения ANTHROPIC_API_KEY (секрет в GitHub Actions:
Settings → Secrets and variables → Actions → ANTHROPIC_API_KEY). Без неё
скрипт завершается с понятной ошибкой и ничего не перезаписывает.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import news_sources as ns
import rss_lite

ROOT = Path(__file__).resolve().parent.parent
NEWS_PATH = ROOT / "news.json"
OPPORTUNITIES_PATH = ROOT / "opportunities.json"

# Модель можно переопределить переменной окружения ANTHROPIC_MODEL, если эта
# устареет/будет снята — актуальный список:
# https://docs.claude.com/en/docs/about-claude/models
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

MAX_CANDIDATES = 80  # ограничение размера промпта


def fetch_feed(url: str, timeout: int = 15) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    entries = rss_lite.parse(raw)
    if not entries:
        raise rss_lite.FeedParseError("0 записей — похоже, лента пустая или не распозналась")
    return entries


def collect_candidates(target_date) -> tuple[list[dict], list[str]]:
    """Возвращает (кандидаты за target_date с проставленными id, список
    лент, которые не удалось прочитать — для лога)."""
    candidates = []
    failed = []
    for feed_cfg in ns.FEEDS:
        try:
            entries = fetch_feed(feed_cfg["url"])
        except Exception as e:
            failed.append(f"{feed_cfg['name']}: {e}")
            continue

        category_filter = feed_cfg.get("category_filter")
        for entry in entries:
            cand = ns.entry_to_candidate(entry, feed_cfg["name"], feed_cfg["region"])
            if not cand:
                continue
            if category_filter and not any(
                category_filter.lower() in c.lower() for c in cand["categories"]
            ):
                continue
            if ns.is_from_almaty_date(cand, target_date):
                candidates.append(cand)

    candidates = ns.dedupe(candidates)[:MAX_CANDIDATES]
    for c in candidates:
        c["id"] = ns.stable_id(c)
    return candidates, failed


# ---------------------------------------------------------------------------
# Anthropic API
# ---------------------------------------------------------------------------

def _build_tool_schema(min_items: int, max_items: int, item_noun: str) -> dict:
    return {
        "name": "select_items",
        "description": f"Выбрать и проаннотировать {item_noun} из списка кандидатов.",
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "minItems": min_items,
                    "maxItems": max_items,
                    "items": {
                        "type": "object",
                        "properties": {
                            "candidate_id": {"type": "string", "description": "id кандидата из входного списка"},
                            "title_ru": {"type": "string", "description": "Заголовок новости на русском языке"},
                            "summary_ru": {"type": "string", "description": "2-4 предложения пересказа новости на русском"},
                            "analysis_ru": {"type": "string", "description": "Оригинальный анализ: как эта новость влияет на Казахстан (2-4 предложения)"},
                        },
                        "required": ["candidate_id", "title_ru", "summary_ru", "analysis_ru"],
                    },
                }
            },
            "required": ["items"],
        },
    }


def _candidates_prompt_block(candidates: list[dict]) -> str:
    lines = []
    for c in candidates:
        lines.append(
            f"- id={c['id']} | источник={c['source_name']} | заголовок: {c['title']}\n"
            f"  саммари: {c['summary']}"
        )
    return "\n".join(lines)


ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"


def _anthropic_request(api_key: str, body: dict, timeout: int = 60) -> dict:
    """Прямой HTTP-вызов Messages API через urllib — без пакета anthropic
    (тот же мотив, что и с rss_lite вместо feedparser: этот код нельзя
    протестировать в sandbox без доступа к PyPI, поэтому лучше не тащить
    ещё одну непроверяемую здесь зависимость; сам HTTP-контракт Anthropic
    API стабилен и хорошо задокументирован)."""
    req = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Anthropic API вернул {e.code}: {detail}") from e


def call_claude(api_key: str, candidates: list[dict], mode: str, target_date_label: str) -> list[dict]:
    """mode: 'news' — ровно 5 новостей за вчера, самых важных для РК;
    'opportunities' — 0-5 новых возможностей для РК, без давления добрать до 5."""
    if not candidates:
        return []

    if mode == "news":
        task = (
            "Отбери 5 новостей из списка ниже — те, что сильнее всего прямо или "
            "косвенно влияют на экономику Казахстана: сырьевые товары, "
            "несырьевые товары, технологии, действия центробанков и другие "
            "макроэкономические темы."
        )
        n = min(5, len(candidates))
        min_items = max_items = n
    else:
        task = (
            "Отбери от 0 до 5 новостей из списка ниже, которые открывают НОВЫЕ "
            "ВОЗМОЖНОСТИ для Казахстана (например: рост спроса на сырьё, которым "
            "богат Казахстан, переориентация цепочек поставок, новые рынки сбыта, "
            "технологические сдвиги, выгодные Казахстану). Если действительно "
            "интересных возможностей меньше 5 — выбери меньше, не натягивай "
            "количество искусственно. Если ни одной убедительной возможности "
            "нет — верни пустой список items."
        )
        min_items, max_items = 0, min(5, len(candidates))

    prompt = (
        f"{task}\n\n"
        "Для каждой выбранной новости напиши на русском языке: краткий пересказ "
        "самой новости (summary_ru) и отдельно — свой оригинальный анализ "
        "(analysis_ru) причинно-следственной связи с Казахстаном (например: через "
        "курс тенге, экспортные доходы, бюджет, приток инвестиций, конкретные "
        "отрасли) — анализ не должен просто повторять новость.\n\n"
        f"Список кандидатов (новости за {target_date_label}):\n"
        f"{_candidates_prompt_block(candidates)}"
    )

    tool = _build_tool_schema(min_items, max_items, "новостей" if mode == "news" else "возможностей")

    resp = _anthropic_request(api_key, {
        "model": MODEL,
        "max_tokens": 4096,
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": "select_items"},
        "messages": [{"role": "user", "content": prompt}],
    })

    for block in resp.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "select_items":
            return block.get("input", {}).get("items", [])
    return []


def build_output_items(selected: list[dict], candidates_by_id: dict) -> list[dict]:
    out = []
    for sel in selected:
        cand = candidates_by_id.get(sel.get("candidate_id"))
        if not cand:
            continue  # модель сослалась на несуществующий id — пропускаем, не падаем
        title_ru = (sel.get("title_ru") or "").strip()
        summary_ru = (sel.get("summary_ru") or "").strip()
        analysis_ru = (sel.get("analysis_ru") or "").strip()
        if not (title_ru and summary_ru and analysis_ru):
            continue  # неполная запись — лучше пропустить, чем показать пустую карточку
        out.append({
            "id": cand["id"],
            "title_ru": title_ru,
            "summary_ru": summary_ru,
            "analysis_ru": analysis_ru,
            "image_url": cand.get("image_url"),
            "source_name": cand["source_name"],
            "source_url": cand["link"],
            "published": cand.get("published"),
        })
    return out


def write_if_changed(path: Path, payload: dict) -> bool:
    new_text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == new_text:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(
            "ANTHROPIC_API_KEY не задан (Settings → Secrets and variables → "
            "Actions в репозитории) — пропускаю обновление новостей.",
            file=sys.stderr,
        )
        sys.exit(1)

    target_date = ns.almaty_yesterday()
    candidates, failed_feeds = collect_candidates(target_date)
    candidates_by_id = {c["id"]: c for c in candidates}

    print(f"Кандидатов за {target_date.isoformat()}: {len(candidates)} (лент не удалось прочитать: {len(failed_feeds)})")
    for f in failed_feeds:
        print(f"  - {f}")

    if not candidates:
        print("Кандидатов нет — news.json/opportunities.json не трогаю.")
        return

    news_selected = call_claude(api_key, candidates, "news", target_date.isoformat())
    news_items = build_output_items(news_selected, candidates_by_id)

    time.sleep(1)  # без спешки между двумя вызовами

    opp_selected = call_claude(api_key, candidates, "opportunities", target_date.isoformat())
    opp_items = build_output_items(opp_selected, candidates_by_id)

    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    news_changed = write_if_changed(NEWS_PATH, {
        "generated_at": generated_at,
        "coverage_date": target_date.isoformat(),
        "items": news_items,
    })
    opp_changed = write_if_changed(OPPORTUNITIES_PATH, {
        "generated_at": generated_at,
        "coverage_date": target_date.isoformat(),
        "items": opp_items,
    })

    print(f"Новости: {len(news_items)} шт. ({'обновлены' if news_changed else 'без изменений'})")
    print(f"Возможности: {len(opp_items)} шт. ({'обновлены' if opp_changed else 'без изменений'})")


if __name__ == "__main__":
    main()
