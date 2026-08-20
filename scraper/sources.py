# -*- coding: utf-8 -*-
"""
Парсеры источников для автообновления дэшборда.
Каждая функция parse_* принимает СЫРОЙ ТЕКСТ страницы (то, что вернёт
page.inner_text("body") в Playwright) и возвращает dict с извлечёнными
числами, либо бросает ValueError с понятным сообщением, если не смогла
распарсить. Никакой сетевой логики здесь нет — это чистые функции,
поэтому их можно тестировать на реальных фрагментах без интернета.
"""
import re
from datetime import date

MONTHS_RU_GENITIVE = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
MONTHS_RU_NOMINATIVE = {
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "май": 5, "июнь": 6,
    "июль": 7, "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12,
}
MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def _num(s):
    """'14,195.00' / '13 945,00' / '109,8' -> float, независимо от формата разделителей."""
    s = s.strip().replace("\xa0", " ").replace(" ", "")
    if "," in s and "." in s:
        # западный формат "14,195.00" - запятая тысячи, точка дробная
        s = s.replace(",", "")
    else:
        # казахстанский/русский формат "109,8" - запятая дробная
        s = s.replace(",", ".")
    return float(s)


# ----------------------------------------------------------------------------
# 1. Базовая ставка Нацбанка РК
# ----------------------------------------------------------------------------
BASE_RATE_ROW_RE = re.compile(
    r'(\d{2})\.(\d{2})\.(\d{4})\*?\s*\n*\s*(\d{1,2},\d{2})\s+(\d{1,2},\d{2})\s*-\s*(\d{1,2},\d{2})'
)


def parse_base_rate(text):
    """
    Источник: https://nationalbank.kz/ru/news/grafik-prinyatiya-resheniy-po-bazovoy-stavke
    Строки таблицы вида: "27.07.2026\n16,75 15,75 - 17,75 ..."
    Берём последнюю (самую свежую) строку с датой в прошлом.
    """
    today = date.today()
    rows = []
    for m in BASE_RATE_ROW_RE.finditer(text):
        d, mo, y, rate, lo, hi = m.groups()
        try:
            dt = date(int(y), int(mo), int(d))
        except ValueError:
            continue
        if dt <= today:
            rows.append((dt, _num(rate)))
    if not rows:
        raise ValueError("базовая ставка: не найдено ни одной строки таблицы решений")
    rows.sort(key=lambda r: r[0])
    latest_date, latest_rate = rows[-1]
    return {
        "value_pct": latest_rate,
        "effective_date": latest_date.isoformat(),
    }


# ----------------------------------------------------------------------------
# 2. Медь (LME, через westmetall.com)
# ----------------------------------------------------------------------------
WESTMETALL_ROW_RE = re.compile(
    r'(\d{2})\.\s+(' + "|".join(MONTHS_EN) + r')\s+(\d{4})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+)',
    re.IGNORECASE,
)


def parse_westmetall_rows(text):
    rows = []
    for m in WESTMETALL_ROW_RE.finditer(text):
        d, month_name, y, cash, three_m, stock = m.groups()
        rows.append({
            "date": date(int(y), MONTHS_EN[month_name.lower()], int(d)),
            "cash": _num(cash),
        })
    return rows


def parse_copper(current_year_text, prior_year_text):
    """
    current_year_text: inner_text страницы westmetall.com?...&field=LME_Cu_cash (текущий год)
    prior_year_text:   та же страница с &year=<текущий год - 1>
    Берём самую свежую дату из текущего года и максимально близкую дату
    год назад (точное совпадение день/месяц, иначе ближайшая существующая).
    """
    current_rows = parse_westmetall_rows(current_year_text)
    prior_rows = parse_westmetall_rows(prior_year_text)
    if not current_rows:
        raise ValueError("медь: не удалось распарсить ни одной строки текущего года")
    if not prior_rows:
        raise ValueError("медь: не удалось распарсить ни одной строки прошлого года")

    latest = max(current_rows, key=lambda r: r["date"])
    target_month, target_day = latest["date"].month, latest["date"].day

    exact = [r for r in prior_rows if r["date"].month == target_month and r["date"].day == target_day]
    if exact:
        prior = exact[0]
    else:
        # точного дня год назад нет в таблице (выходной/праздник на бирже) —
        # берём ближайшую по календарю дату (с переносом через границу года)
        def day_distance(d):
            doy_target = date(2001, target_month, min(target_day, 28)).timetuple().tm_yday
            doy_cand = date(2001, d.month, min(d.day, 28)).timetuple().tm_yday
            diff = abs(doy_target - doy_cand)
            return min(diff, 365 - diff)
        prior = min(prior_rows, key=lambda r: day_distance(r["date"]))

    yoy_pct = round((latest["cash"] - prior["cash"]) / prior["cash"] * 100, 1)
    return {
        "value_usd": latest["cash"],
        "value_date": latest["date"].isoformat(),
        "yoy_pct": yoy_pct,
        "yoy_ref_date": prior["date"].isoformat(),
        "yoy_ref_value": prior["cash"],
    }


# ----------------------------------------------------------------------------
# 3. Brent (ICE) — фронт-месяц, без встроенного YoY (см. ANCHORS в update_data.py)
# ----------------------------------------------------------------------------
ICE_ROW_RE = re.compile(
    r'([A-Z][a-z]{2}\d{2})\s+(\d{1,3}\.\d{3})\s+\d{1,2}/\d{1,2}/\d{4}\d{1,2}:\d{2}\s?[AP]M\s+(-?\d{1,3}\.\d{3})'
)


def parse_brent(text):
    """
    Источник: https://www.ice.com/products/219/Brent-Crude-Futures/data
    Таблица со строками вида: "Oct26 84.840 8/10/202611:51 AM 1.544 73920"
    Первая строка = ближайший (фронт-) месяц поставки — это и есть текущая
    котировка Brent. Название контракта (Oct26 и т.п.) каждый месяц меняется
    сама по себе, поэтому мы просто берём первую валидную строку таблицы.
    """
    m = ICE_ROW_RE.search(text)
    if not m:
        raise ValueError("Brent: не нашёл строку котировки во фронт-месяце (изменилась вёрстка ICE?)")
    contract, last, pct_change = m.groups()
    last_v = _num(last)
    if last_v <= 0:
        # контракт есть в списке, но ещё не торговался сегодня (Last = 0.000) —
        # берём следующую строку с ненулевой ценой
        for mm in ICE_ROW_RE.finditer(text):
            v = _num(mm.group(2))
            if v > 0:
                contract, last_v = mm.group(1), v
                break
    return {
        "contract": contract,
        "value_usd": last_v,
    }


# ----------------------------------------------------------------------------
# 4. Вклады в БВУ (Нацбанк, скользящее окно ~19 месяцев в одной таблице)
# ----------------------------------------------------------------------------
MONTH_HEADER_RE = re.compile(r'^(\d{2}\.\d{2}(?:\s+\d{2}\.\d{2})+)\s*$', re.MULTILINE)


def parse_deposits(text):
    """
    Источник: https://nationalbank.kz/ru/depositoryorganizationsdeposits/depozity-v-depozitnyh-organizaciyah-
    Первая строка — заголовок месяцев "12.24 01.25 ... 06.26".
    Строка "Всего депозитов <19 чисел>" — общий итог, значения выровнены
    по тем же месяцам. Год назад = тот же месяц (MM), YY-1.
    """
    header_m = MONTH_HEADER_RE.search(text)
    if not header_m:
        raise ValueError("вклады БВУ: не нашёл строку-заголовок с месяцами (MM.YY MM.YY ...)")
    months = header_m.group(1).split()

    row_m = re.search(r'Всего депозитов\s+([\d\s]+(?:\d))\s*\n', text + "\n")
    if not row_m:
        raise ValueError("вклады БВУ: не нашёл строку 'Всего депозитов'")
    # Числа записаны с пробелом как разделителем тысяч (напр. "50 135 442"),
    # и тем же пробелом между соседними значениями — визуально неотличимо.
    # На сегодняшний день все значения ряда 8-значные (десятки триллионов
    # тенге = "XX XXX XXX"), поэтому жадный "1-3 цифры + группы по 3" даёт
    # верную разбивку. Подстраховка на случай изменения порядка величины —
    # ниже, через сверку количества значений с количеством месяцев и через
    # диапазонную проверку в update_data.py (санити-чек перед публикацией).
    values = [int(v.replace(" ", "")) for v in re.findall(r'\d{1,3}(?:\s\d{3}){1,3}|\d{1,3}', row_m.group(1).strip())]
    if len(values) != len(months):
        raise ValueError(
            f"вклады БВУ: количество значений ({len(values)}) не совпало с количеством месяцев "
            f"({len(months)}) — вероятно, поменялась вёрстка таблицы, нужна ручная проверка"
        )

    latest_label = months[-1]
    latest_mm, latest_yy = latest_label.split(".")
    latest_value = values[-1]

    year_ago_label = f"{latest_mm}.{int(latest_yy) - 1:02d}"
    if year_ago_label not in months:
        raise ValueError(f"вклады БВУ: в окне таблицы нет месяца {year_ago_label} для сравнения год-к-году")
    year_ago_value = values[months.index(year_ago_label)]

    yoy_pct = round((latest_value - year_ago_value) / year_ago_value * 100, 1)

    pub_m = re.search(r'Опубликовано:\s*(\d{2}\.\d{2}\.\d{4})', text)
    next_m = re.search(r'Следующая публикация:\s*(\d{2}\.\d{2}\.\d{4})', text)

    return {
        "value_mln_tenge": latest_value,
        "period_label": latest_label,
        "published": _ddmmyyyy_to_iso(pub_m.group(1)) if pub_m else None,
        "next_update": _ddmmyyyy_to_iso(next_m.group(1)) if next_m else None,
        "yoy_pct": yoy_pct,
        "yoy_ref_label": year_ago_label,
        "yoy_ref_value_mln_tenge": year_ago_value,
    }


# ----------------------------------------------------------------------------
# 5. Инфляция (общая + 3 подтипа) — stat.gov.kz
# ----------------------------------------------------------------------------
INFLATION_HEADLINE_RE = re.compile(
    r'Инфляция в Республике Казахстан в (\S+ \d{4}) года составила (\d{1,2},\d)%'
)
INFLATION_BREAKDOWN_RE = re.compile(
    r'Цены на продовольственные товары за год выросли на (\d{1,2},\d)%.*?'
    r'непродовольственные товары\s*[–-]\s*на (\d{1,2},\d)%.*?'
    r'платные услуги\s*[–-]\s*на (\d{1,2},\d)%',
    re.DOTALL,
)
PUBLISHED_RE = re.compile(r'Дата опубликования:\s*(\d{2}\.\d{2}\.\d{4})')
NEXT_PUBLISHED_RE = re.compile(r'Дата следующего опубликования:\s*(\d{2}\.\d{2}\.\d{4})')


def parse_inflation(text):
    """
    Источник: конкретная публикация вида
    https://stat.gov.kz/ru/industries/economy/prices/publications/<id>/
    ("Инфляция в Республике Казахстан (<месяц год>г.)") — URL публикации
    меняется каждый месяц, см. find_latest_inflation_url().
    """
    hm = INFLATION_HEADLINE_RE.search(text)
    if not hm:
        raise ValueError("инфляция: не нашёл заголовочную фразу с общей инфляцией")
    period_label, headline = hm.groups()

    bm = INFLATION_BREAKDOWN_RE.search(text)
    if not bm:
        raise ValueError("инфляция: не нашёл разбивку продовольственные/непродовольственные/услуги")
    food, nonfood, services = bm.groups()

    pub_m = PUBLISHED_RE.search(text)
    next_m = NEXT_PUBLISHED_RE.search(text)

    return {
        "period_label": period_label,
        "headline_pct": _num(headline),
        "food_pct": _num(food),
        "nonfood_pct": _num(nonfood),
        "services_pct": _num(services),
        "published": _ddmmyyyy_to_iso(pub_m.group(1)) if pub_m else None,
        "next_update": _ddmmyyyy_to_iso(next_m.group(1)) if next_m else None,
    }


def _ddmmyyyy_to_iso(s):
    d, m, y = s.split(".")
    return f"{y}-{m}-{d}"


# ----------------------------------------------------------------------------
# 6. Рост обрабатывающей промышленности — stat.gov.kz (стабильный URL раздела)
# ----------------------------------------------------------------------------
MANUFACTURING_RE = re.compile(
    r'(\d{2,3},\d)\s*\nИндекс промышленного производства обрабатывающей промышленности\s*\n%,\s*за\s*(\S+)\s+(\d{4})г\.'
)


def parse_manufacturing(text):
    """
    Источник: https://stat.gov.kz/ru/industries/business-statistics/stat-industrial-production/
    Плитка "Ключевые показатели" с индексом (уже YoY по методологии Бюро
    нацстатистики — X,X% означает рост на X,X-100 п.п. к тому же периоду
    прошлого года). Страница живая, всегда актуальный период — URL не меняется.
    """
    m = MANUFACTURING_RE.search(text)
    if not m:
        raise ValueError("рост обработки: не нашёл плитку 'Индекс пром. производства обрабатывающей пром-ти'")
    index_value, period_month, year = m.groups()
    growth_pct = round(_num(index_value) - 100, 1)
    return {
        "growth_pct": growth_pct,
        "period_label": f"{period_month} {year}",
    }


# ----------------------------------------------------------------------------
# 7. ИОК (инвестиции в основной капитал) — stat.gov.kz (стабильный URL раздела)
# ----------------------------------------------------------------------------
IOK_VALUE_RE = re.compile(r'([\d\s]+,\d)\s*\nИнвестиций в основной капитал\s*\nмлрд\. тенге,\s*(\S+)\s+(\d{4})г\.')
IOK_YOY_RE = re.compile(
    r'(\d{2,3},\d)\s*\nИФО инвестиций в основной капитал\s*\nв\s*%,\s*(\S+)\s+(\d{4})г\.\s*к\s*\S+\s+\d{4}г\.'
)


def parse_iok(text):
    """
    Источник: https://stat.gov.kz/ru/industries/business-statistics/stat-invest/
    Плитка "Инвестиций в основной капитал" даёт уровень (млрд тенге),
    плитка "ИФО инвестиций в основной капитал ... к январю-июню <YY-1>г."
    даёт готовый темп прироста год-к-году — отдельно вычислять не нужно.
    """
    vm = IOK_VALUE_RE.search(text)
    ym = IOK_YOY_RE.search(text)
    if not vm:
        raise ValueError("ИОК: не нашёл плитку с уровнем 'Инвестиций в основной капитал'")
    if not ym:
        raise ValueError("ИОК: не нашёл плитку с темпом 'ИФО инвестиций в основной капитал ... к ...'")
    value_bln, period_month, year = vm.groups()
    yoy_index, _, _ = ym.groups()
    return {
        "value_bln_tenge": _num(value_bln),
        "period_label": f"{period_month} {year}",
        "yoy_pct": round(_num(yoy_index) - 100, 1),
    }


# ----------------------------------------------------------------------------
# 8. ВВП (рост, г/г) — пресс-релизы МНЭ РК на gov.kz
# ----------------------------------------------------------------------------
RU_MONTH_NUM_WORDS = {
    "один": 1, "два": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6,
    "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12,
}

# Ловим заголовочную фразу-якорь релиза: "по итогам <период> <ГОД> года" /
# "за <период> <ГОД> года" — этот шаблон устойчиво повторяется из релиза в
# релиз (проверено на реальных фрагментах 2025-2026гг.), хотя порядок слов
# вокруг него (глагол, "согласно предварительным данным БНС" и т.п.) гуляет.
GDP_ANCHOR_RE = re.compile(
    r'(?:по\s+итогам|за)\s+([^,.\n]+?)\s+(?:(\d{4})\s+года|текущего\s+года)',
    re.IGNORECASE,
)
# Само значение роста ищем отдельно, рядом с якорем, а не всем текстом —
# дальше в статье попадаются проценты роста ДРУГИХ показателей (промышленность,
# торговля и т.д.), которые не должны попасть в карточку ВВП. Два варианта
# формулировки встречаются примерно поровну: развёрнутая статистическая
# ("ВВП/валовой внутренний продукт Казахстана составил/увеличился на X%") и
# короткая, из протокола заседания Правительства ("прирост ВВП
# составляет/составил X%").
GDP_VALUE_RE = re.compile(
    r'(?:(?:ВВП|валовой\s+внутренний\s+продукт)\s+Казахстана|прирост\s+ВВП)\s+'
    r'(?:составил|составляет|увеличил(?:ся|ась)\s+на|вырос(?:ла)?\s+на|ускорил(?:ся|ась)\s+до)\s+'
    r'(\d{1,2},\d)\s*%',
    re.IGNORECASE,
)
GDP_TITLE_ROST_RE = re.compile(r'\b(?:при)?рост\w*\b', re.IGNORECASE)
GDP_TITLE_FORECAST_RE = re.compile(r'прогноз', re.IGNORECASE)
GDP_PUBLISHED_RE = re.compile(
    r'(\d{1,2})\s+(' + "|".join(MONTHS_RU_GENITIVE) + r')\s+(\d{4})\s+\d{1,2}:\d{2}'
)


def _period_end_month(period_phrase):
    """'первого полугодия' -> 6, 'января-октября'/'январь-октябрь' -> 10,
    '8 месяцев' -> 8, 'семь месяцев' -> 7, 'января-декабря' (годовой итог) -> 12."""
    t = period_phrase.lower().replace("–", "-").replace("—", "-")
    if "полугод" in t:
        return 6
    m = re.search(r"(\d{1,2})\s*месяц", t)
    if m:
        return int(m.group(1))
    for word, num in RU_MONTH_NUM_WORDS.items():
        if re.search(rf"\b{word}\s+месяц", t):
            return num
    last = re.split(r"[-\s]", t.strip())[-1].strip()
    for name, num in list(MONTHS_RU_GENITIVE.items()) + list(MONTHS_RU_NOMINATIVE.items()):
        if last.startswith(name[:6]):
            return num
    return None


def is_gdp_release_title(title):
    """Грубый фильтр заголовков в списке gov.kz (перед тем как открывать статью
    целиком): похоже на фактический релиз с цифрой роста, а не на прогноз,
    методику расчёта, долю МСБ в ВВП и т.п. Заголовки такого рода:
      "Рост ВВП Казахстана за 7 месяцев 2025 года составил 6,3%"          -> True
      "Рост экономики Казахстана по итогам первого полугодия ... 4,1%"    -> True
      "Правительство прогнозирует рост экономики ... на уровне ... 5,3%"  -> False (прогноз)
      "Доля МСБ в ВВП Казахстана выросла до 40,9%"                        -> False (нет "рост")
      "Министерство утвердило методику расчета потенциального ВВП"       -> False (нет "%")
    """
    if "%" not in title:
        return False
    if not GDP_TITLE_ROST_RE.search(title):
        return False
    if GDP_TITLE_FORECAST_RE.search(title):
        return False
    return True


def parse_gdp_release(text):
    """
    Источник: конкретный пресс-релиз вида
    https://www.gov.kz/memleket/entities/economy/press/news/details/<id>?lang=ru
    Первый абзац почти всегда содержит фразу вида "По итогам {периода} {ГОД}
    года ... ВВП Казахстана составил X,X%" (глагол варьируется: составил /
    увеличился на / вырос на / ускорился до). Дата публикации — отдельной
    строкой сразу под заголовком, вида "14 июля 2026 11:58".
    """
    am = GDP_ANCHOR_RE.search(text)
    if not am:
        raise ValueError("ВВП: не нашёл фразу-якорь 'по итогам/за ... <ГОД> года' в тексте релиза")
    period_phrase, year = am.groups()
    period_phrase = period_phrase.strip()

    # Ищем значение и дату публикации в достаточно широком окне с начала
    # текста — короткая формулировка ("прирост ВВП составляет X%", из
    # протокола заседания Правительства) обычно идёт внутри длинной цитаты
    # и оказывается дальше от начала статьи, чем в развёрнутых пресс-релизах.
    window = text[:1600]
    vm = GDP_VALUE_RE.search(window)
    if not vm:
        raise ValueError("ВВП: не нашёл фразу с процентом роста ВВП рядом с началом релиза")
    value_pct = _num(vm.group(1))

    end_month = _period_end_month(period_phrase)
    if end_month is None:
        raise ValueError(f"ВВП: не смог определить последний месяц периода из фразы '{period_phrase}'")

    pub_m = GDP_PUBLISHED_RE.search(window)
    published = None
    if pub_m:
        d, month_name, y = pub_m.groups()
        published = date(int(y), MONTHS_RU_GENITIVE[month_name], int(d)).isoformat()

    if year is None:
        # "... текущего года" — год берём из даты публикации релиза.
        if not published:
            raise ValueError("ВВП: период дан как 'текущего года', но не нашёл дату публикации, чтобы определить год")
        year = date.fromisoformat(published).year
    else:
        year = int(year)

    return {
        "period_phrase": period_phrase,
        "year": year,
        "end_month": end_month,
        "value_pct": value_pct,
        "published": published,
    }
