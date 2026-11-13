"""Small-account simulator: sizing, fixed levels from the real fill, first trigger wins, gap skip."""
from datetime import date, datetime, timedelta
from datetime import time as dtime

from market_edge import intraday as I, small_account as sa

DAY = date(2026, 11, 5)


def path(*ohlc, start=dtime(9, 15)):
    t0 = datetime.combine(DAY, start, I.IST)
    return [I.Bar(t0 + timedelta(minutes=5 * i), *b) for i, b in enumerate(ohlc)]


def test_sizing_uses_full_position_for_fixed_and_caps_scan():
    fixed = sa.Params(plan="fixed", target_pct=2, stop_pct=1)
    assert sa.qty_for(500.0, 495.0, fixed) == 100                  # Rs 50,000 / Rs 500
    scan = sa.Params(plan="scan", risk_pct=1.0)
    assert sa.qty_for(500.0, 490.0, scan) == 10                    # Rs 100 risk / Rs 10 per share
    assert sa.qty_for(500.0, 499.99, scan) == 100                  # capped at Rs 50,000 position


def test_fixed_levels_measured_from_fill():
    p = sa.Params(plan="fixed", target_pct=2, stop_pct=1)
    assert sa.fixed_from_entry("bullish", 200.0, p) == (198.0, 204.0)
    s, t = sa.fixed_from_entry("bearish", 200.0, p)
    assert round(s, 6) == 202.0 and round(t, 6) == 196.0


def test_first_candidate_to_trigger_in_time_is_taken():
    p = sa.Params(plan="fixed", target_pct=2, stop_pct=1, candidates=2)
    cands = [{"symbol": "LATE", "direction": "bullish", "trigger": 100.0, "stop": 95, "target": 110},
             {"symbol": "EARLY", "direction": "bullish", "trigger": 100.0, "stop": 95, "target": 110}]
    paths = {"LATE": path((99, 99.5, 99, 99.4), (99.4, 100.2, 99.3, 100.1), (100.1, 102.5, 100, 102.4)),
             "EARLY": path((99.8, 100.5, 99.7, 100.4), (100.4, 102.2, 100.3, 102.1))}
    r = sa.day_intraday(cands, paths, p)
    assert r["symbol"] == "EARLY" and r["outcome"] == "win" and r["entry_time"] == "09:15"
    assert 850 < r["net"] < 950                                    # ~Rs 1,000 gross - ~Rs 50 slippage - ~Rs 52 costs


def test_gap_beyond_one_stop_width_is_skipped():
    p = sa.Params(plan="fixed", target_pct=2, stop_pct=1)
    c = [{"symbol": "GAP", "direction": "bullish", "trigger": 100.0, "stop": 95, "target": 110}]
    assert sa.day_intraday(c, {"GAP": path((101.6, 103, 101, 102))}, p) is None   # opened 1.6% above trigger
    assert sa.day_daily(c, {"GAP": {"open": 101.6, "high": 103, "low": 101, "close": 102}}, p) is None


def test_summary_numbers():
    p = sa.Params()
    s = sa.summarize([1000, -500, 0, 1500], sessions=5, p=p, costs=[50, 50, 50, 50])
    assert s["trade_days"] == 4 and s["no_trade_days"] == 1 and s["goal_days_pct"] == 50.0
    assert s["total_rs"] == 2000 and s["max_drawdown_rs"] == 500 and s["worst_day_pct"] == -5.0
