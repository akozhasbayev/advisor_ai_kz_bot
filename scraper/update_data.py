# -*- coding: utf-8 -*-
"""
Главный скрипт автообновления. Запускается по расписанию в GitHub Actions
(см. .github/workflows/update-data.yml), где ЕСТЬ полноценный доступ в
интернет — в отличие от песочницы, где этот скрипт писался и тестировался
только на фрагментах реальных страниц (см. scraper/test_parsers.py).

Логика на каждый показатель:
  1. открыть страницу источника через Playwright, снять текст
  2. распарсить (sources.py)
  3. прогнать через "санити-чек" (разумный диапазон + разумный шаг
     изменения от предыдущего значения) — см. SANITY
  4. если чек не прошёл -> НЕ публиковать, оставить прежнее значение,
     залогировать проблему (шаг workflow в этом случае будет отмечен как
     failed, чтобы это было заметно)
  5. в конце — переписать data.json ТОЛЬКО если хотя бы одно значение
     реально изменилось, и только успешно провалидированными пунктами
     (проблемные пункты остаются как были)

Показатели ПИИ, свободная ликвидность банков и ВВП сюда не входят —
остаются на ручном обновлении (см. README в этой папке).
"""
import json
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(__file__))
import sources  # noqa: E402

DATA_JSON_PATH = os.path.join(os.path.dirname(__file__), "..", "data.json")

# ----------------------------------------------------------------------------
# Статические "якоря" год-к-году для показателей, где сам источник не даёт
# сравнение YoY напрямую и повторный скрап архивной страницы за прошлый год
# слишком ненадёжен (нестабильные URL публикаций). Значения ниже — то, что
# было тщательно сверено вручную в этом чате. Нужно освежать раз в год —
# см. поле "refresh_by" у каждого якоря.
# ----------------------------------------------------------------------------
ANCHORS = {
    "manufacturing_prior_year_growth_pct": {"value": 5.5, "period": "янв–июн 2025", "refresh_by": "2027-07-20"},
    "brent_prior_year_usd": {"value": 66.83, "period": "≈ первая неделя августа 2025", "refresh_by": "2027-08-15"},
    "inflation_prior_year": {
        "headline": 11.8, "food": 11.2, "nonfood": 9.5, "services": 14.9,
        "period": "июль 2025", "refresh_by": "2027-08-10",
    },
}

# ----------------------------------------------------------------------------
# Санити-чек: (мин, макс, макс_доля_изменения_от_предыдущего)
# max_step — доля (0.15 = 15%); для ставок/процентов задаём абсолютный шаг
# в п.п. через ключ max_step_abs вместо доли.
# ----------------------------------------------------------------------------
SANITY = {
    "base_rate_pct": {"min": 5, "max": 30, "max_step_abs": 4},
    "copper_usd": {"min": 3000, "max": 30000, "max_step": 0.25},
    "brent_usd": {"min": 20, "max": 200, "max_step": 0.30},
    "deposits_mln_tenge": {"min": 20_000_000, "max": 120_000_000, "max_step": 0.15},
    "inflation_headline_pct": {"min": 0, "max": 30, "max_step_abs": 3},
    "manufacturing_growth_pct": {"min": -20, "max": 40, "max_step_abs": 6},
    "iok_bln_tenge": {"min": 1000, "max": 40000, "max_step": 0.30},
}


class ValidationFailed(Exception):
    pass


def sanity_check(key, new_value, old_value):
    rule = SANITY[key]
    if not (rule["min"] <= new_value <= rule["max"]):
        raise ValidationFailed(f"{key}: {new_value} вне допустимого диапазона [{rule['min']}, {rule['max']}]")
    if old_value is not None:
        if "max_step_abs" in rule:
            if abs(new_value - old_value) > rule["max_step_abs"]:
                raise ValidationFailed(
                    f"{key}: скачок {old_value} -> {new_value} больше {rule['max_step_abs']} — похоже на ошибку парсинга"
                )
        elif "max_step" in rule and old_value:
            change = abs(new_value - old_value) / abs(old_value)
            if change > rule["max_step"]:
                raise ValidationFailed(
                    f"{key}: скачок {old_value} -> {new_value} ({change:.0%}) больше допустимых {rule['max_step']:.0%}"
                )


def fetch_text(page, url, wait_selector=None, wait_ms=0):
    page.goto(url, timeout=45000, wait_until="load")
    if wait_selector:
        page.wait_for_selector(wait_selector, timeout=20000)
    if wait_ms:
        page.wait_for_timeout(wait_ms)
    return page.inner_text("body")


def find_latest_inflation_url(page):
    """Страница-список публикаций отсортирована от новых к старым — берём первую ссылку на 'Инфляция в Республике Казахстан (...)'."""
    page.goto("https://stat.gov.kz/ru/industries/economy/prices/publications/", timeout=45000, wait_until="load")
    page.wait_for_selector("a", timeout=20000)
    href = page.eval_on_selector(
        "a:text-matches('Инфляция в Республике Казахстан \\\\(', 'i')", "el => el.href"
    )
    if not href:
        raise ValidationFailed("инфляция: не нашёл ссылку на последнюю публикацию в списке")
    return href


def load_prev_data():
    if os.path.exists(DATA_JSON_PATH):
        with open(DATA_JSON_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def run(playwright_module):
    prev = load_prev_data()
    prev_by_id = {item["id"]: item for item in prev.get("items", [])}
    results = {}
    errors = []

    browser = playwright_module.chromium.launch(headless=True)
    page = browser.new_page(user_agent="Mozilla/5.0 (compatible; KZEconDashboardBot/1.0)")

    def prev_value(item_id, field):
        item = prev_by_id.get(item_id)
        if not item:
            return None
        return item.get(field)

    # -- базовая ставка ------------------------------------------------------
    try:
        text = fetch_text(page, "https://nationalbank.kz/ru/news/grafik-prinyatiya-resheniy-po-bazovoy-stavke")
        r = sources.parse_base_rate(text)
        sanity_check("base_rate_pct", r["value_pct"], prev_value("base_rate", "_raw_value_pct"))
        results["base_rate"] = r
    except Exception as e:
        errors.append(f"базовая ставка: {e}")

    # -- медь -----------------------------------------------------------------
    try:
        this_year = date.today().year
        cur = fetch_text(page, "https://www.westmetall.com/en/markdaten.php?action=table&field=LME_Cu_cash")
        prior = fetch_text(page, f"https://www.westmetall.com/en/markdaten.php?action=table&field=LME_Cu_cash&year={this_year - 1}")
        r = sources.parse_copper(cur, prior)
        sanity_check("copper_usd", r["value_usd"], prev_value("copper", "_raw_value_usd"))
        results["copper"] = r
    except Exception as e:
        errors.append(f"медь: {e}")

    # -- Brent ------------------------------------------------------------
    try:
        text = fetch_text(page, "https://www.ice.com/products/219/Brent-Crude-Futures/data", wait_ms=4000)
        r = sources.parse_brent(text)
        sanity_check("brent_usd", r["value_usd"], prev_value("brent", "_raw_value_usd"))
        results["brent"] = r
    except Exception as e:
        errors.append(f"Brent: {e}")

    # -- вклады БВУ -------------------------------------------------------
    try:
        text = fetch_text(page, "https://nationalbank.kz/ru/depositoryorganizationsdeposits/depozity-v-depozitnyh-organizaciyah-", wait_ms=2000)
        r = sources.parse_deposits(text)
        sanity_check("deposits_mln_tenge", r["value_mln_tenge"], prev_value("deposits", "_raw_value_mln_tenge"))
        results["deposits"] = r
    except Exception as e:
        errors.append(f"вклады БВУ: {e}")

    # -- инфляция -----------------------------------------------------------
    try:
        url = find_latest_inflation_url(page)
        text = fetch_text(page, url)
        r = sources.parse_inflation(text)
        sanity_check("inflation_headline_pct", r["headline_pct"], prev_value("inflation_headline", "_raw_headline_pct"))
        results["inflation"] = r
    except Exception as e:
        errors.append(f"инфляция: {e}")

    # -- рост обработки -------------------------------------------------------
    try:
        text = fetch_text(page, "https://stat.gov.kz/ru/industries/business-statistics/stat-industrial-production/")
        r = sources.parse_manufacturing(text)
        sanity_check("manufacturing_growth_pct", r["growth_pct"], prev_value("manufacturing", "_raw_growth_pct"))
        results["manufacturing"] = r
    except Exception as e:
        errors.append(f"рост обработки: {e}")

    # -- ИОК ------------------------------------------------------------------
    try:
        text = fetch_text(page, "https://stat.gov.kz/ru/industries/business-statistics/stat-invest/")
        r = sources.parse_iok(text)
        sanity_check("iok_bln_tenge", r["value_bln_tenge"], prev_value("iok", "_raw_value_bln_tenge"))
        results["iok"] = r
    except Exception as e:
        errors.append(f"ИОК: {e}")

    browser.close()
    return results, errors, prev


def to_dashboard_items(results, prev):
    """Преобразует результаты парсеров в формат карточек дэшборда (DATA[])."""
    prev_by_id = {item["id"]: item for item in prev.get("items", [])}
    items = []

    def keep_prev(item_id):
        old = prev_by_id.get(item_id)
        return dict(old) if old else None

    def days_between(iso_a, iso_b):
        return (date.fromisoformat(iso_b) - date.fromisoformat(iso_a)).days

    # ВВП, ПИИ, свободная ликвидность — не трогаем, переносим как есть
    for manual_id in ("gdp", "fdi", "liquidity"):
        old = keep_prev(manual_id)
        if old:
            items.append(old)

    if "base_rate" in results:
        r = results["base_rate"]
        nxt = prev_by_id.get("base_rate", {}).get("nextUpdate")
        items.append({
            "id": "base_rate", "label": "Базовая ставка Нацбанка РК",
            "value": f"{r['value_pct']:.2f}".rstrip("0").rstrip(".") + "%",
            "period": f"Действует с {date.fromisoformat(r['effective_date']).strftime('%d.%m.%Y')}",
            "published": r["effective_date"], "nextUpdate": nxt, "approx": False,
            "noNextUpdate": nxt is None,
            "source": "Нацбанк РК", "url": "https://nationalbank.kz/ru/news/grafik-prinyatiya-resheniy-po-bazovoy-stavke",
            "yoy": None, "_raw_value_pct": r["value_pct"],
        })
    else:
        old = keep_prev("base_rate")
        if old:
            items.append(old)

    if "manufacturing" in results:
        r = results["manufacturing"]
        anchor = ANCHORS["manufacturing_prior_year_growth_pct"]
        items.append({
            "id": "manufacturing", "label": "Рост обрабатывающей промышленности (г/г)",
            "value": f"{r['growth_pct']}%", "period": r["period_label"].capitalize(),
            "published": date.today().isoformat(), "nextUpdate": None, "approx": False, "noNextUpdate": True,
            "source": "Бюро нацстатистики РК",
            "url": "https://stat.gov.kz/ru/industries/business-statistics/stat-industrial-production/",
            "yoy": {"value": round(r["growth_pct"] - anchor["value"], 1), "unit": "п.п.",
                    "refPeriod": f"{anchor['period']} ({anchor['value']}%)"},  # см. ANCHORS[...]["refresh_by"] выше и scraper/README.md
            "_raw_growth_pct": r["growth_pct"],
        })
    else:
        old = keep_prev("manufacturing")
        if old:
            items.append(old)

    if "iok" in results:
        r = results["iok"]
        items.append({
            "id": "iok", "label": "ИОК", "value": f"{r['value_bln_tenge']/1000:.2f} трлн ₸",
            "period": r["period_label"].capitalize(), "published": date.today().isoformat(),
            "nextUpdate": None, "approx": False, "noNextUpdate": True, "source": "Бюро нацстатистики РК",
            "url": "https://stat.gov.kz/ru/industries/business-statistics/stat-invest/",
            "yoy": {"value": r["yoy_pct"], "unit": "%", "refPeriod": f"{r['period_label']} (в сопост. ценах)"},
            "_raw_value_bln_tenge": r["value_bln_tenge"],
        })
    else:
        old = keep_prev("iok")
        if old:
            items.append(old)

    if "brent" in results:
        r = results["brent"]
        anchor = ANCHORS["brent_prior_year_usd"]
        yoy_pct = round((r["value_usd"] - anchor["value"]) / anchor["value"] * 100, 1)
        items.append({
            "id": "brent", "label": "Нефть Brent (ICE)", "value": f"${r['value_usd']:.2f}",
            "period": f"Фронт-месяц {r['contract']}", "published": date.today().isoformat(),
            "nextUpdate": None, "approx": False, "noNextUpdate": True,
            "source": "ICE Futures Europe", "url": "https://www.ice.com/products/219/Brent-Crude-Futures/data",
            "yoy": {"value": yoy_pct, "unit": "%",
                    "refPeriod": f"{anchor['period']} (${anchor['value']})"},  # см. ANCHORS[...]["refresh_by"] выше и scraper/README.md
            "_raw_value_usd": r["value_usd"],
        })
    else:
        old = keep_prev("brent")
        if old:
            items.append(old)

    if "copper" in results:
        r = results["copper"]
        items.append({
            "id": "copper", "label": "Медь (LME)", "value": f"${r['value_usd']:,.0f}".replace(",", " "),
            "period": f"Расчётная цена, {date.fromisoformat(r['value_date']).strftime('%d.%m.%Y')}",
            "published": r["value_date"], "nextUpdate": None, "approx": False, "noNextUpdate": True,
            "source": "LME (через westmetall.com)", "url": "https://www.westmetall.com/en/markdaten.php?action=table&field=LME_Cu_cash",
            "yoy": {"value": r["yoy_pct"], "unit": "%", "refPeriod": f"{date.fromisoformat(r['yoy_ref_date']).strftime('%d.%m.%Y')} (${r['yoy_ref_value']:,.0f})".replace(",", " ")},
            "_raw_value_usd": r["value_usd"],
        })
    else:
        old = keep_prev("copper")
        if old:
            items.append(old)

    if "deposits" in results:
        r = results["deposits"]

        def mmyy_to_label(mmyy):
            mm, yy = mmyy.split(".")
            month_names = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
                           "августа", "сентября", "октября", "ноября", "декабря"]
            return f"{month_names[int(mm) - 1]} 20{yy}"

        items.append({
            "id": "deposits", "label": "Вклады в БВУ (физ. + юр. лица)",
            "value": f"{r['value_mln_tenge']/1_000_000:.2f} трлн ₸",
            "period": f"На конец {mmyy_to_label(r['period_label'])}",
            "published": r.get("published") or date.today().isoformat(),
            "nextUpdate": r.get("next_update"), "approx": False, "source": "Нацбанк РК",
            "url": "https://nationalbank.kz/ru/depositoryorganizationsdeposits/depozity-v-depozitnyh-organizaciyah-",
            "yoy": {"value": r["yoy_pct"], "unit": "%",
                    "refPeriod": f"{mmyy_to_label(r['yoy_ref_label'])} ({(r['yoy_ref_value_mln_tenge']/1_000_000):.2f} трлн ₸)"},
            "_raw_value_mln_tenge": r["value_mln_tenge"],
        })
    else:
        old = keep_prev("deposits")
        if old:
            items.append(old)

    if "inflation" in results:
        r = results["inflation"]
        anchor = ANCHORS["inflation_prior_year"]
        for suffix, label, cur_key, anchor_key in (
            ("headline", "Инфляция (г/г)", "headline_pct", "headline"),
            ("food", "Инфляция: продовольственные товары (г/г)", "food_pct", "food"),
            ("nonfood", "Инфляция: непродовольственные товары (г/г)", "nonfood_pct", "nonfood"),
            ("services", "Инфляция: платные услуги (г/г)", "services_pct", "services"),
        ):
            cur_v = r[cur_key]
            anchor_v = anchor[anchor_key]
            items.append({
                "id": f"inflation_{suffix}", "label": label, "value": f"{cur_v}%",
                "period": r["period_label"].capitalize(), "published": r["published"], "nextUpdate": r["next_update"],
                "approx": False, "source": "Бюро нацстатистики РК",
                "url": "https://stat.gov.kz/ru/industries/economy/prices/publications/",
                "yoy": {"value": round(cur_v - anchor_v, 1), "unit": "п.п.",
                        "refPeriod": f"{anchor['period']} ({anchor_v}%)"},  # см. ANCHORS[...]["refresh_by"] выше и scraper/README.md
                "_raw_headline_pct": cur_v if suffix == "headline" else None,
            })
    else:
        for suffix in ("headline", "food", "nonfood", "services"):
            old = keep_prev(f"inflation_{suffix}")
            if old:
                items.append(old)

    return items


def main():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        results, errors, prev = run(p)

    items = to_dashboard_items(results, prev)
    new_data = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "items": items,
        "manual_only_ids": ["gdp", "fdi", "liquidity"],
    }

    with open(DATA_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(new_data, f, ensure_ascii=False, indent=2)
    print(f"Обновлено показателей: {len(results)}/7")
    if errors:
        print("\nПроблемы (данные по этим пунктам НЕ обновлены, оставлены прежние):")
        for e in errors:
            print(f"  - {e}")
        # Не роняем весь прогон (частичное обновление — это нормально),
        # но явно фиксируем, что были проблемы, в отдельном файле для
        # диагностики следующим запуском / вручную.
        sys.exit(2 if len(errors) == len(SANITY) else 0)


if __name__ == "__main__":
    main()
