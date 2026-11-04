"""Offline tests for step 5 (paper trading, backtest helpers, watchlist)."""
import json

import pytest

from market_edge import config, nse, paper, watchlist


def test_costs_realistic_for_1_lakh_round_trip():
    c = paper.costs(100_000, 100_000)
    assert 75 < c < 95            # brokerage 40 + STT 25 + exch ~6 + stamp 3 + GST ~8


def test_position_size_respects_risk_and_cap():
    assert paper.size(100, 98) == 500                  # Rs 1000 risk / Rs 2 per share
    assert paper.size(100, 99.99) == 2000              # capped by Rs 2 lakh exposure


@pytest.mark.parametrize("bar,mode,expected", [
    ((99, 99.5, 97, 99), "pess", "no_entry"),          # never reached trigger 100
    ((105, 106, 104, 105), "pess", "no_entry"),        # gapped past target 104
    ((99, 105, 97, 103), "pess", "loss"),              # touched both: pessimistic = loss
    ((99, 105, 97, 103), "opt", "win"),                # touched both: optimistic = win
    ((99, 101, 97, 99), "opt", "close"),               # stop touched but close above it: optimistic keeps it
    ((99, 101, 97, 97.5), "opt", "loss"),              # close below stop confirms the loss
    ((99, 101.5, 98.5, 101), "pess", "close"),         # neither: square off at close
])
def test_simulate_bullish(bar, mode, expected):
    assert paper.simulate("bullish", 100, 98, 104, *bar, mode).outcome == expected


def test_simulate_bearish_mirrors():
    assert paper.simulate("bearish", 100, 102, 96, 101, 101.5, 95, 97, "pess").outcome == "win"
    f = paper.simulate("bearish", 100, 102, 96, 99, 99.5, 98, 98.5, "pess")
    assert f.entry == 99 and f.outcome == "close"     # gap below trigger fills at open


def test_record_and_evaluate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    con = nse.connect()
    con.execute("INSERT INTO daily (symbol, series, date, open, high, low, close) VALUES "
                "('ABC','EQ','2026-11-05', 99, 105, 99.5, 103)")
    con.commit()
    rep = {"scan_date": "2026-11-04", "bearish": [], "bullish": [{
        "symbol": "ABC", "setup": "Accumulation", "signals": ["VOL_SPIKE"], "score": 4, "catalysts": [],
        "wide_stop": False, "trigger": 100.0, "stop": 98.0, "target": 104.0}]}
    assert paper.record_from_report(rep) == 1
    assert paper.record_from_report(rep) == 0            # idempotent
    assert paper.evaluate_pending() == 1
    t = paper.load("live").iloc[0]
    assert t["outcome_pess"] == "win" and t["qty"] == 500 and t["trade_date"] == "2026-11-05"
    assert 1880 < t["net_pess"] < 1910     # Rs 2,000 gross - ~Rs 51 slippage - ~Rs 54 costs
    s = paper.stats(paper.load("live"))
    assert s["entered"] == 1 and s["pess"]["win_rate_pct"] == 100.0


def test_watchlist_prefers_setups_with_track_record(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(paper, "EDGE_JSON", tmp_path / "edge.json")
    (tmp_path / "edge.json").write_text(json.dumps({"setups": {
        "Volume breakout": {"n": 2000, "intraday_pct": -0.25, "hit_pct": 41, "five_day_pct": -0.3, "quarters_positive": 0, "quarters": 4},
        "Volume breakdown": {"n": 900, "intraday_pct": 0.55, "hit_pct": 60, "five_day_pct": 1.5, "quarters_positive": 4, "quarters": 4}}}))
    pick = dict(signals=[], score=5, catalysts=[], wide_stop=False, trigger=1, stop=1, target=1, risk_pct=1, industry=None)
    rep = {"scan_date": "2026-11-04",
           "bullish": [{**pick, "symbol": "UP", "setup": "Volume breakout"}],
           "bearish": [{**pick, "symbol": "DOWN", "setup": "Volume breakdown"}]}
    top = watchlist.build(rep, {"label": "Neutral"}, [])
    assert [p["symbol"] for p in top] == ["DOWN", "UP"]
    assert "held up" in watchlist.format_telegram(top, "2026-11-04")
