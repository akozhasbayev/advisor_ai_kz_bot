"""
Оффлайн-тесты для rss_lite.py и news_sources.py — никакой сети и никаких
сторонних пакетов, только стандартная библиотека. Запуск:
python3 scraper/test_news.py
"""
from datetime import date

import news_sources as ns
import rss_lite

SAMPLE_RSS_2 = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://www.rssboard.org/media-rss">
<channel>
<title>Test Feed</title>
<item>
<title>Цены на нефть выросли из-за напряжённости в Ормузском проливе</title>
<link>https://example.com/oil-hormuz</link>
<description><![CDATA[ <p>Нефть Brent подорожала на фоне сокращения танкерного трафика.</p> ]]></description>
<pubDate>Wed, 19 Aug 2026 10:00:00 +0000</pubDate>
<category>Экономика</category>
<media:thumbnail url="https://example.com/img/oil.jpg"/>
</item>
<item>
<title>Новость без ссылки</title>
<description>Эта запись не должна попасть в кандидаты, у неё нет link.</description>
<pubDate>Wed, 19 Aug 2026 09:00:00 +0000</pubDate>
</item>
<item>
<title>Старая новость</title>
<link>https://example.com/old</link>
<description>Позавчерашняя новость — не должна пройти фильтр по дате.</description>
<pubDate>Mon, 17 Aug 2026 10:00:00 +0000</pubDate>
</item>
<item>
<title>С enclosure-картинкой (как у Интерфакса)</title>
<link>https://example.com/enclosure-img</link>
<description>Проверка enclosure type="image/...".</description>
<pubDate>Wed, 19 Aug 2026 11:00:00 +0000</pubDate>
<enclosure url="https://example.com/img/enc.jpg" length="1000" type="image/jpeg"/>
</item>
</channel>
</rss>
"""

SAMPLE_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Test Atom Feed</title>
<entry>
<title>Fed holds rates steady</title>
<link rel="alternate" href="https://example.com/fed-rates"/>
<summary>The Federal Reserve left interest rates unchanged.</summary>
<updated>2026-08-19T15:30:00Z</updated>
</entry>
</feed>
"""

SAMPLE_RDF = """<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns="http://purl.org/rss/1.0/">
<channel rdf:about="https://example.com/">
<title>Test RDF Feed</title>
</channel>
<item rdf:about="https://example.com/rdf-item">
<title>RDF-формат (DW-style)</title>
<link>https://example.com/rdf-item</link>
<description>Проверка RSS 1.0 / RDF, где item — не вложен в channel.</description>
</item>
</rdf:RDF>
"""


def test_rss2_extracts_all_fields():
    entries = rss_lite.parse(SAMPLE_RSS_2)
    assert len(entries) == 4
    first = entries[0]
    assert first["title"] == "Цены на нефть выросли из-за напряжённости в Ормузском проливе"
    assert first["link"] == "https://example.com/oil-hormuz"
    assert "Нефть Brent" in first["summary"]
    assert first["image_url"] == "https://example.com/img/oil.jpg"
    assert first["published"] == "2026-08-19T10:00:00+00:00"
    assert first["categories"] == ["Экономика"]


def test_rss2_enclosure_image():
    entries = rss_lite.parse(SAMPLE_RSS_2)
    enc_entry = next(e for e in entries if e["link"] == "https://example.com/enclosure-img")
    assert enc_entry["image_url"] == "https://example.com/img/enc.jpg"


def test_atom_feed_parses_via_entry_tag():
    entries = rss_lite.parse(SAMPLE_ATOM)
    assert len(entries) == 1
    e = entries[0]
    assert e["title"] == "Fed holds rates steady"
    assert e["link"] == "https://example.com/fed-rates"
    assert e["published"] == "2026-08-19T15:30:00+00:00"


def test_rdf_feed_finds_items_not_nested_in_channel():
    entries = rss_lite.parse(SAMPLE_RDF)
    assert len(entries) == 1
    assert entries[0]["title"] == "RDF-формат (DW-style)"
    assert entries[0]["link"] == "https://example.com/rdf-item"


def test_invalid_xml_raises_feed_parse_error():
    try:
        rss_lite.parse("не xml вообще")
        assert False, "должно было бросить FeedParseError"
    except rss_lite.FeedParseError:
        pass


# ---------------------------------------------------------------------------
# news_sources.py — уровень над rss_lite
# ---------------------------------------------------------------------------

def test_entry_to_candidate_strips_html_and_skips_missing_link():
    entries = rss_lite.parse(SAMPLE_RSS_2)
    cand = ns.entry_to_candidate(entries[0], "Test Feed", "test")
    assert cand is not None
    assert "<p>" not in cand["summary"]
    assert cand["categories"] == ["Экономика"]

    no_link_cand = ns.entry_to_candidate(entries[1], "Test Feed", "test")
    assert no_link_cand is None


def test_is_from_almaty_date_filters_correctly():
    entries = rss_lite.parse(SAMPLE_RSS_2)
    cand_19 = ns.entry_to_candidate(entries[0], "Test Feed", "test")
    cand_17 = ns.entry_to_candidate(entries[2], "Test Feed", "test")

    target = date(2026, 8, 19)
    assert ns.is_from_almaty_date(cand_19, target) is True
    assert ns.is_from_almaty_date(cand_17, target) is False


def test_stable_id_is_deterministic_and_link_based():
    id1 = ns.stable_id({"link": "https://example.com/oil-hormuz"})
    id2 = ns.stable_id({"link": "https://example.com/oil-hormuz"})
    id3 = ns.stable_id({"link": "https://example.com/other"})
    assert id1 == id2
    assert id1 != id3
    assert len(id1) == 12


def test_dedupe_keeps_first_occurrence_only():
    a = {"link": "https://example.com/a", "title": "A1"}
    a_dup = {"link": "https://example.com/a", "title": "A2"}
    b = {"link": "https://example.com/b", "title": "B"}
    out = ns.dedupe([a, a_dup, b])
    assert len(out) == 2
    assert out[0]["title"] == "A1"


def test_almaty_yesterday_is_one_day_before_almaty_today():
    assert (ns.almaty_today() - ns.almaty_yesterday()).days == 1


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"OK   {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} прошли")
    if failures:
        raise SystemExit(1)
