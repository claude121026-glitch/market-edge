"""Offline tests for step 6 (intraday parsing, exact path simulation, live tracker with fake clock)."""
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime

import pytest

from market_edge import config, intraday as I, notify, nse, paper

IST = I.IST
DAY = date(2026, 11, 5)


def bars(*spec, start=dtime(9, 15), step=5):
    """spec: (o,h,l,c) tuples -> 5-minute bars from 9:15 IST."""
    t0 = datetime.combine(DAY, start, IST)
    return [I.Bar(t0 + timedelta(minutes=step * i), *ohlc) for i, ohlc in enumerate(spec)]


def test_parse_nse_chart_handles_both_time_conventions():
    real = datetime.combine(DAY, dtime(10, 0), IST)
    true_ms = real.timestamp() * 1000                                    # proper epoch
    shifted_ms = real.replace(tzinfo=timezone.utc).timestamp() * 1000    # IST wall clock stored as UTC
    for ms in (true_ms, shifted_ms):
        b = I.parse_nse_chart({"grapthData": [[ms, 100.0], [ms + 60000, 101.0]]}, DAY)
        assert [x.ts.time() for x in b] == [dtime(10, 0), dtime(10, 1)] and b[1].close == 101.0


def test_parse_yahoo_converts_to_ist_and_filters_day():
    ts = int(datetime.combine(DAY, dtime(9, 15), IST).timestamp())
    payload = {"chart": {"result": [{"timestamp": [ts - 86400, ts, ts + 300],
                                     "indicators": {"quote": [{"open": [1, 2, None], "high": [1, 3, 1],
                                                               "low": [1, 1, 1], "close": [1, 2, 1]}]}}]}}
    b = I.parse_yahoo(payload, DAY)
    assert len(b) == 1 and b[0].ts.time() == dtime(9, 15) and b[0].high == 3


@pytest.mark.parametrize("spec,expected,ambiguous", [
    # trigger 100, stop 98, target 104 (long)
    ([(99, 99.5, 98.8, 99.2), (99.2, 100.5, 99.1, 100.3), (100.3, 104.2, 100.2, 104)], "win", False),
    ([(99, 100.5, 97.5, 100)], "loss", True),                         # stop inside the entry bar: unknown order
    ([(99, 100.5, 99, 100.2), (100.2, 104.5, 97.9, 101)], "loss", True),  # stop+target in one later bar
    ([(99, 100.5, 99, 100.2), (100.2, 101, 97.9, 98.5), (98.5, 105, 98, 104)], "loss", False),  # stop first
    ([(99, 99.5, 98.5, 99)], "no_entry", False),
    ([(104.5, 105, 104, 104.8)], "no_entry", False),                 # gapped past target
])
def test_simulate_path_long(spec, expected, ambiguous):
    f = I.simulate_path("bullish", 100, 98, 104, bars(*spec))
    assert (f.outcome, f.ambiguous) == (expected, ambiguous)


def test_square_off_at_1520_uses_last_bar_before():
    b = bars((99, 100.5, 99.5, 100.2), (100.2, 101, 100, 100.8))
    late = I.Bar(datetime.combine(DAY, dtime(15, 20), IST), 101, 110, 101, 109)   # after square-off: ignored
    f = I.simulate_path("bullish", 100, 98, 104, b + [late])
    assert f.outcome == "close" and f.exit == 100.8


def test_simulate_path_short():
    f = I.simulate_path("bearish", 100, 102, 96, bars((101, 101, 99.5, 99.8), (99.8, 100, 95.5, 96)))
    assert f.outcome == "win" and f.entry == 100 and f.exit == 96


def test_live_tracker_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    sent = []
    monkeypatch.setattr(notify, "send", lambda text: sent.append(text) or True)
    nse.connect().close()
    rep = {"scan_date": "2026-11-04", "bearish": [], "bullish": [{
        "symbol": "ABC", "setup": "Volume breakdown", "signals": [], "score": 5, "catalysts": [],
        "wide_stop": False, "trigger": 100.0, "stop": 98.0, "target": 104.0}]}
    paper.record_from_report(rep)

    path = bars((99, 99.5, 98.8, 99.2), (99.2, 100.5, 99.1, 100.3), (100.3, 102, 100, 101.5),
                (101.5, 104.3, 101, 104), start=dtime(9, 15))
    clock = {"t": datetime.combine(DAY, dtime(9, 15), IST)}
    def now(): return clock["t"]
    def sleep(s): clock["t"] += timedelta(minutes=5)
    def fetch(symbol, day):
        return [b for b in path if b.ts <= clock["t"]], "test"   # path revealed as time passes

    res = I.run_live(poll=300, now_fn=now, sleep_fn=sleep, fetch_fn=fetch)
    text = "\n".join(sent)
    assert "LONG triggered @ 100.00" in text and "target hit @ 104.00" in text
    assert res["tracked"] == 1 and res["total"] > 0
    t = paper.load("live").iloc[0]
    assert t["outcome_exact"] == "win" and t["exact_source"] == "test" and t["ambiguous"] == 0


def test_live_tracker_stops_on_holiday(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    sent = []
    monkeypatch.setattr(notify, "send", lambda text: sent.append(text) or True)
    nse.connect().close()
    paper.record_from_report({"scan_date": "2026-11-04", "bearish": [], "bullish": [{
        "symbol": "ABC", "setup": "x", "signals": [], "score": 1, "catalysts": [], "wide_stop": False,
        "trigger": 100.0, "stop": 98.0, "target": 104.0}]})
    clock = {"t": datetime.combine(DAY, dtime(9, 15), IST)}
    res = I.run_live(poll=600, now_fn=lambda: clock["t"],
                     sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + timedelta(minutes=10)),
                     fetch_fn=lambda sym, day: ([], "test"))
    assert res.get("holiday") and "holiday" in sent[-1]


def _one_pick(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    sent = []
    monkeypatch.setattr(notify, "send", lambda text: sent.append(text) or True)
    nse.connect().close()
    paper.record_from_report({"scan_date": "2026-11-04", "bearish": [], "bullish": [{
        "symbol": "ABC", "setup": "x", "signals": [], "score": 1, "catalysts": [], "wide_stop": False,
        "trigger": 100.0, "stop": 98.0, "target": 104.0}]})
    return sent


def test_afternoon_resume_does_not_repeat_morning_alerts(tmp_path, monkeypatch):
    sent = _one_pick(tmp_path, monkeypatch)
    # Triggered at 9:20 and hit target at 9:30 - both before the afternoon job starts at 12:05.
    path = bars((99, 100.5, 99.5, 100.3), (100.3, 102, 100, 101.5), (101.5, 104.5, 101, 104))
    clock = {"t": datetime.combine(DAY, dtime(12, 5), IST)}
    res = I.run_live(poll=3600, now_fn=lambda: clock["t"],
                     sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + timedelta(hours=1)),
                     fetch_fn=lambda sym, day: (path, "test"))
    text = "\n".join(sent)
    assert "triggered" not in text and "target hit" not in text and "Live tracker started" not in text
    assert "Day summary" in text and res["tracked"] == 1           # afternoon session ends the day


def test_morning_only_session_skips_summary(tmp_path, monkeypatch):
    sent = _one_pick(tmp_path, monkeypatch)
    path = bars((99, 99.5, 98.8, 99.2))
    clock = {"t": datetime.combine(DAY, dtime(9, 15), IST)}
    res = I.run_live(poll=1800, until=dtime(12, 10), now_fn=lambda: clock["t"],
                     sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + timedelta(minutes=30)),
                     fetch_fn=lambda sym, day: (path, "test"))
    assert res.get("partial") and not any("Day summary" in s for s in sent)


def test_export_and_merge_intraday(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "a.db")
    con = I.connect()
    I.store_bars(con, "ABC", bars((1, 2, 0.5, 1.5), (1.5, 2, 1, 1.8)), "test")
    con.close()
    assert I.export_day(str(tmp_path / "x.db"), DAY.isoformat()) == 2
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "b.db")
    assert I.merge_files([str(tmp_path / "x.db")]) == 2
    assert len(I.load_bars(I.connect(), "ABC", DAY.isoformat())) == 2
