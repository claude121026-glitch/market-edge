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


from market_edge import config, notify, nse, paper  # noqa: E402


def _setup_day(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    nse.connect().close()
    rows = [dict(setup="x", signals=[], catalysts=[], wide_stop=False, score=s, symbol=sym, trigger=100.0, stop=98.0,
                 target=104.0) for sym, s in (("FIRST", 9), ("SECOND", 8), ("THIRD", 7), ("FOURTH", 6))]
    paper.record_from_report({"scan_date": "2026-11-04", "bearish": [], "bullish": rows})
    c = paper.connect()
    c.executemany("UPDATE paper_trades SET morning_rank=? WHERE symbol=?", [(1, "FIRST"), (2, "SECOND"), (3, "THIRD"), (4, "FOURTH")])
    c.commit(); c.close()


def test_live_paper_account_takes_only_the_first_trigger(tmp_path, monkeypatch):
    _setup_day(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(notify, "send", lambda text, **k: sent.append(text) or True)
    quiet = path((99, 99.5, 99, 99.2), (99.2, 99.6, 99.1, 99.5), (99.5, 99.8, 99.3, 99.6), (99.6, 99.9, 99.4, 99.7))
    second = path((99, 99.5, 99, 99.4), (99.4, 100.3, 99.3, 100.2), (100.2, 103.2, 100.1, 103.1), (103.1, 103.5, 102.8, 103.3))
    third = path((99.8, 100.4, 99.7, 100.3), (100.3, 100.5, 98.4, 98.5), (98.5, 98.7, 98.0, 98.2), (98.2, 98.4, 97.9, 98.0))
    paths = {"FIRST": quiet, "SECOND": second, "THIRD": third, "FOURTH": second}
    clock = {"t": datetime.combine(DAY, dtime(9, 15), I.IST)}
    p = sa.Params(plan="fixed", target_pct=3, stop_pct=1.5, candidates=3)
    I.run_live(poll=300, now_fn=lambda: clock["t"], refresh_fn=None, small=p,
               sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + timedelta(minutes=5)),
               fetch_fn=lambda sym, d: ([b for b in paths[sym] if b.ts <= clock["t"]], "test"))
    text = "\n".join(sent)
    # THIRD triggered at 09:15, before SECOND (09:20): THIRD is the only trade, stopped out at -1.5%
    alerts = text.split("Day summary")[0]
    assert alerts.count("₹10,000 account: BUY") == 1 and "₹10,000 account: BUY THIRD" in alerts   # one trade only
    assert "🛑 stop hit on <b>THIRD</b>" in text and "of capital" in text
    assert "₹10,000 account:</b> BUY THIRD" in text                      # in the day summary too


def test_record_recent_summary_and_plan(tmp_path, monkeypatch):
    _setup_day(tmp_path, monkeypatch)
    con = I.connect()
    con.execute("INSERT INTO daily (symbol, series, date, close) VALUES ('FIRST','EQ','2026-11-05', 1)")
    win = path((99.8, 100.4, 99.7, 100.3), (100.3, 103.5, 100.2, 103.4))
    for sym in ("FIRST", "SECOND", "THIRD"):
        I.store_bars(con, sym, win if sym == "SECOND" else path((99, 99.2, 98.9, 99.1)), "test")
    con.commit(); con.close()
    import pandas as pd
    monkeypatch.setattr(sa, "datetime", type("D", (sa.datetime,), {"now": classmethod(lambda cls, tz=None: sa.datetime(2026, 11, 6, 19, 0, tzinfo=tz))}))
    p = sa.Params(plan="fixed", target_pct=3, stop_pct=1.5, candidates=3)
    rows = sa.record_recent(p=p)
    assert len(rows) == 1 and rows[0]["symbol"] == "SECOND" and rows[0]["outcome"] == "win" and rows[0]["net"] > 1300
    assert sa.record_recent(p=p) == []                                   # stored once
    s = sa.live_summary(p)
    assert s["days"] == 1 and s["targets"] == 1 and s["account_rs"] > 11300
    assert "paper account" in sa.format_summary(s, p)
    top = [{"symbol": "FIRST", "direction": "bullish", "trigger": 500.0, "stop": 490, "target": 520}]
    plan = sa.format_plan(top, p)
    assert "qty 100 (₹50,000)" in plan and "target ≈515.00 (+₹1,500)" in plan and "stop ≈492.50 (−₹750)" in plan
