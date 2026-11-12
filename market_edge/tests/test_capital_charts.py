"""Capital per trade, chart analysis cards and Telegram albums."""
import struct
from datetime import datetime, timedelta
from datetime import time as dtime

import pandas as pd

from market_edge import cards, config, intraday as I, notify, nse, paper, watchlist

PNG = b"\x89PNG\r\n\x1a\n"


def test_plan_size_and_money_format():
    sz = paper.plan_size(3210.0, 3273.17)          # AIAENG short from the real watchlist
    assert sz == {"qty": 15, "capital": 48150.0, "risk_rs": 947.55}
    assert paper.lakh(48150) == "₹48,150" and paper.lakh(421000) == "₹4.21 lakh" and paper.lakh(1.2e7) == "₹1.20 cr"


def test_watchlist_carries_capital_and_total(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    row = dict(signals=["VOL_SPIKE"], score=5, catalysts=[], wide_stop=False, risk_pct=2.0, industry=None,
               setup="Accumulation", chg_pct=2.0, vol_x=3.0)
    rep = {"scan_date": "2026-11-04", "bearish": [],
           "bullish": [{**row, "symbol": "A", "trigger": 100.0, "stop": 98.0, "target": 104.0},
                       {**row, "symbol": "B", "trigger": 1000.0, "stop": 999.0, "target": 1002.0}]}
    top = watchlist.build(rep, {"label": "Neutral"}, [], record=False)
    by = {p["symbol"]: p for p in top}
    assert by["A"]["qty"] == 500 and by["A"]["capital"] == 50000.0
    assert by["B"]["qty"] == 200 and by["B"]["capital"] == 200000.0          # capped at Rs 2 lakh exposure
    text = watchlist.format_telegram(top, "2026-11-04")
    assert "Qty 500 · capital ₹50,000" in text and "Capital if all 2 trigger: ₹2.50 lakh" in text
    assert cards.watchlist_card(top, "2026-11-04").startswith(PNG)


def _bars(n=80):
    d0 = datetime(2026, 7, 1)
    rows = []
    for i in range(n):
        c = 100 + i * 0.2
        rows.append(dict(date=(d0 + timedelta(days=i)).strftime("%Y-%m-%d"), open=c - 0.5, high=c + 1, low=c - 1,
                         close=c, volume=1000 + i, deliv_pct=40.0))
    rows[-1].update(close=118.0, high=119.0, volume=6000, deliv_pct=70.0)
    return pd.DataFrame(rows)


def test_chart_context_analysis_and_card(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    con = nse.connect()
    b = _bars()
    con.executemany("INSERT INTO daily (symbol, series, date, open, high, low, close, volume, deliv_pct) "
                    "VALUES ('ABC','EQ',?,?,?,?,?,?,?)", b[["date", "open", "high", "low", "close", "volume", "deliv_pct"]]
                    .itertuples(index=False, name=None))
    con.commit()
    con.close()
    ctx = watchlist.chart_context("ABC", b["date"].iloc[-1])
    assert len(ctx["bars"]) == 80 and ctx["hh20"] < 118 and ctx["avg_vol20"] < 2000
    p = {"symbol": "ABC", "direction": "bullish", "setup": "Accumulation · 20D breakout", "trigger": 119.0,
         "stop": 117.0, "target": 123.0, "chg_pct": 2.5, "vol_x": 5.5, "signals": ["VOL_SPIKE", "BRK_20D"],
         "catalysts": ["Order win"], "reasons": ["catalyst"], **paper.plan_size(119.0, 117.0),
         "history": {"n": 800, "intraday_pct": 0.1, "hit_pct": 47.0, "quarters_positive": 3, "quarters": 4}}
    lines = watchlist.analysis_lines(p, ctx)
    text = " | ".join(lines)
    assert "Broke above the 20-day high" in text and "Qty 500 · capital ₹59,500" in text and "Catalyst: Order win" in text
    png = cards.trade_chart(p, ctx, 1, b["date"].iloc[-1], lines)
    w, h = struct.unpack(">II", png[16:24])
    assert png.startswith(PNG) and w == 1274 and 900 < h < 2500
    assert "Chart analysis" in watchlist.format_analysis([p], {"ABC": ctx})


def test_album_then_fallbacks(monkeypatch):
    for k, v in (("TELEGRAM_BOT_TOKEN", "t"), ("TELEGRAM_CHAT_ID", "1"), ("WHATSAPP_PHONE", None), ("WHATSAPP_APIKEY", None)):
        monkeypatch.setattr(config, k, v)
    monkeypatch.setattr(notify.time, "sleep", lambda s: None)
    albums, photos, texts = [], [], []
    monkeypatch.setattr(notify, "_tg_album", lambda pngs, cap=None: albums.append(len(pngs)) or (True, ""))
    monkeypatch.setattr(notify, "_tg_photo", lambda png, cap=None: photos.append(png) or (True, ""))
    monkeypatch.setattr(notify, "_post_telegram", lambda t: texts.append(t) or True)
    assert notify.send("<b>Charts</b>", images=[b"x"] * 12, album=True)
    assert albums == [10, 2] and photos == [] and texts == []            # 12 charts: album of 10, then album of 2
    albums.clear(); photos.clear()
    monkeypatch.setattr(notify, "_tg_album", lambda pngs, cap=None: (False, "Bad Request"))
    assert notify.send("<b>Charts</b>", images=[b"a", b"b"], album=True)
    assert photos == [b"a", b"b"] and texts == []                       # album refused: one by one
    monkeypatch.setattr(notify, "_tg_photo", lambda png, cap=None: (False, "nope"))
    assert notify.send("fallback", images=[b"a", b"b"], album=True) and texts == ["fallback"]


def test_multipart_many_has_every_file():
    body, ctype = notify._multipart_many({"chat_id": "1", "media": "[]"}, [("p0", "a.png", b"AAA"), ("p1", "b.png", b"BBB")])
    assert b'name="p0"; filename="a.png"' in body and b"BBB" in body and ctype.startswith("multipart/form-data")


def test_live_alert_and_summary_show_capital(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    sent = []
    monkeypatch.setattr(notify, "send", lambda text, **k: sent.append(text) or True)
    nse.connect().close()
    paper.record_from_report({"scan_date": "2026-11-04", "bearish": [], "bullish": [{
        "symbol": "ABC", "setup": "x", "signals": [], "score": 1, "catalysts": [], "wide_stop": False,
        "trigger": 100.0, "stop": 98.0, "target": 104.0}]})
    day = datetime(2026, 11, 5).date()
    t0 = datetime.combine(day, dtime(9, 15), I.IST)
    path = [I.Bar(t0 + timedelta(minutes=5 * i), *ohlc) for i, ohlc in
            enumerate([(99, 100.5, 99.5, 100.3), (100.3, 102, 100, 101.5), (101.5, 104.5, 101, 104)])]
    clock = {"t": t0}
    I.run_live(poll=300, now_fn=lambda: clock["t"], refresh_fn=None,
               sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + timedelta(minutes=5)),
               fetch_fn=lambda sym, d: ([b for b in path if b.ts <= clock["t"]], "test"))
    text = "\n".join(sent)
    assert "Qty 500 · capital ₹50,000 · at risk ₹1,000" in text
    assert "Capital used: ₹50,000" in text and "return" in text


def _report(day, n):
    row = dict(setup="Accumulation", signals=[], catalysts=[], wide_stop=False, trigger=100.0, stop=98.0, target=104.0)
    return {"scan_date": day, "bearish": [], "bullish": [{**row, "symbol": f"S{i}", "score": float(n - i)} for i in range(n)]}


def test_only_the_top10_watchlist_counts_as_our_trades(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    con = nse.connect()
    for d in ("2026-11-05", "2026-11-06", "2026-11-09"):
        con.executemany("INSERT INTO daily (symbol, series, date, open, high, low, close) VALUES (?, 'EQ', ?, 99, 105, 99.5, 103)",
                        [(f"S{i}", d) for i in range(25)])
    con.commit()
    paper.record_from_report(_report("2026-11-05", 25))                      # traded on 11-06
    c = paper.connect()
    c.executemany("UPDATE paper_trades SET morning_rank=? WHERE signal_date='2026-11-05' AND symbol=?",
                  [(i + 1, f"S{i * 2}") for i in range(10)])                 # the morning picked 10 of them
    c.commit(); c.close()
    paper.record_from_report(_report("2026-11-09", 25))                      # not traded yet
    res = paper.finalize_our_trades()
    assert res == {"days": 1, "moved": 15}
    live = paper.load("live")
    assert set(live[live.signal_date == "2026-11-05"].symbol) == {f"S{i * 2}" for i in range(10)}
    assert len(live[live.signal_date == "2026-11-09"]) == 25                 # untouched until it is traded
    assert len(paper.load("scan_only")) == 15
    assert paper.evaluate_pending() == 10                                    # only our trades are scored
    s = paper.stats(paper.load("live"))
    assert s["signals"] == 35 and s["entered"] == 10                          # 10 traded + 25 still pending
    assert paper.finalize_our_trades()["moved"] == 0                          # idempotent


def test_unranked_day_falls_back_to_top_scores(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    con = nse.connect()
    con.executemany("INSERT INTO daily (symbol, series, date, close) VALUES (?, 'EQ', '2026-11-06', 1)", [(f"S{i}", ) for i in range(25)])
    con.commit()
    paper.record_from_report(_report("2026-11-05", 25))
    paper.finalize_our_trades()
    assert set(paper.load("live").symbol) == {f"S{i}" for i in range(config.WATCHLIST_SIZE)}   # highest scores


def test_traded_watchlist_is_never_re_ranked(tmp_path, monkeypatch):
    from datetime import datetime as real_dt
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    nse.connect().close()
    rep = _report("2026-11-11", 12)
    rep["bullish"] = [{**r, "risk_pct": 2.0, "industry": None} for r in rep["bullish"]]
    paper.record_from_report(rep)
    c = paper.connect()
    c.execute("UPDATE paper_trades SET morning_rank=1 WHERE symbol='S11'")      # what the morning ranked
    c.commit(); c.close()

    class Evening(real_dt):
        @classmethod
        def now(cls, tz=None):
            return real_dt(2026, 11, 12, 19, 22, tzinfo=tz)
    import datetime as dtmod
    monkeypatch.setattr(dtmod, "datetime", Evening)
    watchlist.build(rep, {"label": "Neutral"}, [], record=True)
    ranks = paper.load("live").set_index("symbol")["morning_rank"].dropna()
    assert list(ranks.index) == ["S11"]                                       # unchanged after the trade day


def test_re_rank_before_trading_replaces_the_old_ranking(tmp_path, monkeypatch):
    from datetime import datetime as real_dt
    import datetime as dtmod
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    nse.connect().close()
    rep = _report("2026-11-09", 12)
    rep["bullish"] = [{**r, "risk_pct": 2.0, "industry": None} for r in rep["bullish"]]
    paper.record_from_report(rep)

    class HolidayMorning(real_dt):          # 10 Nov (holiday) ranked first, 11 Nov re-ranked before the open
        @classmethod
        def now(cls, tz=None):
            return real_dt(2026, 11, 11, 7, 55, tzinfo=tz)
    monkeypatch.setattr(dtmod, "datetime", HolidayMorning)
    c = paper.connect()
    c.execute("UPDATE paper_trades SET morning_rank=1 WHERE symbol IN ('S10','S11')")   # an earlier ranking
    c.commit(); c.close()
    watchlist.build(rep, {"label": "Neutral"}, [], record=True)
    ranked = paper.load("live")["morning_rank"].notna().sum()
    assert ranked == config.WATCHLIST_SIZE                                    # exactly 10, old ranks cleared
