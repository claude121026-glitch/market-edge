"""Offline tests for macro news + events (no network, LLM mocked)."""
import json
from datetime import date

from market_edge import config, events, macro_news as m

RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Oil Crashes 9% as traders bet on a US-Iran deal</title><link>https://x/1</link>
<description>&lt;p&gt;Brent slid&lt;/p&gt;</description><pubDate>Wed, 04 Nov 2026 12:00:00 -0600</pubDate></item>
<item><title>RBI cuts repo rate by 25 bps to 5.25%</title><link>https://x/2</link><pubDate>Wed, 04 Nov 2026 10:00:00 +0530</pubDate></item>
<item><title>Celebrity wedding photos</title><link>https://x/3</link></item>
</channel></rss>"""


def _items():
    return m.parse_feed(RSS, "Test", "India")


def test_parse_feed():
    items = _items()
    assert len(items) == 3 and items[0].summary == "Brent slid"
    assert items[0].published.utcoffset().total_seconds() == -6 * 3600
    assert items[2].published is None


def test_rules_direction_and_priority():
    sc = m.score_rules(_items())
    oil, rbi = sc[_items()[0].id], sc[_items()[1].id]
    assert oil["nifty"] == 1 and "OMCs" in oil["sectors_pos"]          # crude down = good for India
    assert rbi["importance"] == 9 and "Banks" in rbi["sectors_pos"]
    assert _items()[2].id not in sc


def test_rules_ignore_far_apart_words():
    item = m.Item("x", "s", "Global", "Democrats win Texas oil regulator seat, shares higher", "", "", None)
    assert m.score_rules([item]) == {}   # "oil ... higher" too far apart


def test_llm_output_validated_and_mapped_by_id(monkeypatch):
    items = _items()
    fake = {"items": [
        {"id": 2, "category": "RBI & Monetary", "importance": 15, "nifty": 5, "banknifty": "2", "inr": -1,
         "sectors_pos": ["banks", "realty", "Spaceships"], "sectors_neg": [], "stocks": ["hdfcbank"],
         "takeaway": "Rate cut helps lenders."},
        {"id": 99, "category": "x", "importance": 5},   # unknown id -> dropped
    ]}
    monkeypatch.setattr(m, "_llm_call", lambda prompt: "```json\n" + json.dumps(fake) + "\n```")
    sc = m.score_llm(items)
    r = sc[items[1].id]
    assert len(sc) == 1 and r["importance"] == 10 and r["nifty"] == 2
    assert r["sectors_pos"] == ["Banks", "Realty"] and r["stocks"] == ["HDFCBANK"]


def test_macro_score_combines_news_and_prices():
    news = [{"category": "Geopolitics", "nifty": -2, "importance": 9, "sectors_pos": [], "sectors_neg": ["Banks"]}]
    prices = {"prices": [
        {"key": "SPX", "price": 1, "chg_1d_pct": -2.0},
        {"key": "VIX", "price": 30, "chg_1d_pct": 20},
        {"key": "BRENT", "price": 1, "chg_1d_pct": -9.0, "verify": True},   # ignored
    ]}
    s = m.macro_score(news, prices)
    assert s["score"] < 3.5 and s["label"] == "Risk-off"
    assert m.macro_score([], {})["label"] == "Neutral"


def test_events_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EVENTS_JSON", tmp_path / "events.json")
    (tmp_path / "events.json").write_text(json.dumps(
        [{"date": "2026-12-04", "time_ist": "10:00", "name": "RBI MPC decision", "importance": 9}]))
    evs = events.upcoming(days=40, today=date(2026, 11, 28))
    names = [(e["date"], e["name"]) for e in evs]
    assert ("2026-12-04", "US jobs report (NFP)") in names            # first Friday of Dec 2026
    assert ("2026-12-09", "US Fed (FOMC) decision") not in names      # 2 PM NY = next day in IST
    assert ("2026-12-10", "US Fed (FOMC) decision") in names
    assert ("2026-11-30", "India GDP (quarterly)") in names
    assert not any(e["approx"] and "RBI MPC" in e["name"] for e in evs if e["date"].startswith("2026-12"))


def test_repeated_stories_are_downweighted():
    one = [{"category": "Crude & Energy", "nifty": 1, "importance": 6, "sectors_pos": ["OMCs"], "sectors_neg": []}]
    four = one * 4
    assert m.story_weights(four) == [1.0, 0.25, 0.25, 0.25]
    assert dict(m.sector_view(four))["OMCs"] == round(0.6 * 1.75, 1)
