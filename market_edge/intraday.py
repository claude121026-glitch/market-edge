"""Step 6: live intraday tracking of the watchlist + exact (path-based) paper-trade outcomes.

Live (market hours): polls each watchlist stock every POLL seconds, sends Telegram alerts when a
trigger, target or stop is hit, and stores the price path. At the close every trade is scored
EXACTLY from the stored path, replacing the pessimistic/optimistic guess from daily bars.

Retroactive: `resolve` fetches Yahoo 5-minute bars (available ~60 days back) for paper trades that
were not tracked live, and scores them exactly too.

Data sources (free, public, unofficial; can change):
  NSE intraday chart  https://www.nseindia.com/api/chart-databyindex?index=<SYMBOL>EQN  (today only)
  Yahoo chart         https://query1.finance.yahoo.com/v8/finance/chart/<SYMBOL>.NS?interval=5m&range=...
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from zoneinfo import ZoneInfo

from . import config, net, notify, paper

log = logging.getLogger("market_edge.intraday")
IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN, SQUARE_OFF, MARKET_CLOSE = dtime(9, 15), dtime(15, 20), dtime(15, 30)

NSE_CHART = "https://www.nseindia.com/api/chart-databyindex?index={sym}EQN"
YAHOO_CHART = "https://query{n}.finance.yahoo.com/v8/finance/chart/{sym}.NS?interval={iv}&range={rng}"

SCHEMA = """
CREATE TABLE IF NOT EXISTS intraday_bars (
    symbol TEXT, date TEXT, ts TEXT, open REAL, high REAL, low REAL, close REAL, source TEXT,
    PRIMARY KEY (symbol, date, ts)
);
"""
EXACT_COLS = {"outcome_exact": "TEXT", "exit_exact": "REAL", "net_exact": "REAL", "r_exact": "REAL",
              "ambiguous": "INTEGER", "exact_source": "TEXT"}


def connect() -> sqlite3.Connection:
    con = paper.connect()
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(paper_trades)")}
    for col, typ in EXACT_COLS.items():
        if col not in have:
            con.execute(f"ALTER TABLE paper_trades ADD COLUMN {col} {typ}")
    return con


@dataclass
class Bar:
    ts: datetime          # IST, bar start
    open: float
    high: float
    low: float
    close: float


# --------------------------------------------------------------------------- #
# Data sources
# --------------------------------------------------------------------------- #
def parse_nse_chart(payload: dict, day: date) -> list[Bar]:
    """NSE returns [[epoch_ms, price], ...]. Some NSE feeds encode IST wall-clock time as if it were UTC;
    detect that by checking which interpretation lands inside market hours."""
    pts = payload.get("grapthData") or payload.get("graphData") or []
    if not pts:
        return []
    def as_ist(ms: float, shifted: bool) -> datetime:
        d = datetime.fromtimestamp(ms / 1000, timezone.utc)
        return d.replace(tzinfo=IST) if shifted else d.astimezone(IST)
    mid = pts[len(pts) // 2][0]
    shifted = MARKET_OPEN <= as_ist(mid, True).time() <= MARKET_CLOSE and \
        not (MARKET_OPEN <= as_ist(mid, False).time() <= MARKET_CLOSE)
    bars = []
    for ms, px in pts:
        if px is None:
            continue
        t = as_ist(ms, shifted)
        if t.date() == day:
            bars.append(Bar(t, float(px), float(px), float(px), float(px)))
    return bars


def parse_yahoo(payload: dict, day: date | None = None) -> list[Bar]:
    res = payload["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    bars = []
    for i, ts in enumerate(res.get("timestamp") or []):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        t = datetime.fromtimestamp(ts, timezone.utc).astimezone(IST)
        if day is None or t.date() == day:
            bars.append(Bar(t, float(o), float(h), float(l), float(c)))
    return bars


def fetch_nse(symbol: str, day: date) -> list[Bar]:
    raw = net.get(NSE_CHART.format(sym=urllib.parse.quote(symbol, safe="")), timeout=20, retries=1, backoff=(5,))
    return parse_nse_chart(json.loads(raw), day)


def fetch_yahoo(symbol: str, day: date, interval: str = "5m") -> list[Bar]:
    age = (datetime.now(IST).date() - day).days
    rng = "5d" if age <= 4 else "1mo" if age <= 28 else "60d"
    sym = urllib.parse.quote(symbol, safe="")
    last_err = None
    for n in (1, 2):
        try:
            raw = net.get(YAHOO_CHART.format(n=n, sym=sym, iv=interval, rng=rng), timeout=20, retries=2, backoff=(5, 15))
            return parse_yahoo(json.loads(raw), day)
        except net.HttpError as e:
            last_err = e
    raise last_err


def fetch_path(symbol: str, day: date) -> tuple[list[Bar], str]:
    """Best available intraday path for symbol on day: NSE (today only) then Yahoo 5m."""
    if day == datetime.now(IST).date():
        try:
            bars = fetch_nse(symbol, day)
            if bars:
                return bars, "nse"
        except Exception as e:
            log.info("NSE chart %s: %s", symbol, e)
    return fetch_yahoo(symbol, day), "yahoo5m"


def store_bars(con: sqlite3.Connection, symbol: str, bars: list[Bar], source: str) -> None:
    con.executemany("INSERT OR REPLACE INTO intraday_bars VALUES (?,?,?,?,?,?,?,?)",
                    [(symbol, b.ts.date().isoformat(), b.ts.isoformat(), b.open, b.high, b.low, b.close, source)
                     for b in bars])
    con.commit()


def load_bars(con: sqlite3.Connection, symbol: str, day: str) -> list[Bar]:
    rows = con.execute("SELECT ts, open, high, low, close FROM intraday_bars WHERE symbol=? AND date=? ORDER BY ts",
                       (symbol, day)).fetchall()
    return [Bar(datetime.fromisoformat(r[0]), *r[1:]) for r in rows]


# --------------------------------------------------------------------------- #
# Exact simulation on an intraday path
# --------------------------------------------------------------------------- #
@dataclass
class PathFill:
    outcome: str                # no_entry | win | loss | close
    entry: float | None = None
    exit: float | None = None
    entry_ts: datetime | None = None
    exit_ts: datetime | None = None
    ambiguous: bool = False     # stop and target inside one bar (order unknown -> scored as loss)


def simulate_path(direction: str, trigger: float, stop: float, target: float, bars: list[Bar],
                  square_off: dtime = SQUARE_OFF) -> PathFill:
    bull = direction == "bullish"
    bars = [b for b in bars if MARKET_OPEN <= b.ts.time() < MARKET_CLOSE]
    if not bars:
        return PathFill("no_entry")
    first = bars[0]
    if (bull and first.open >= target) or (not bull and first.open <= target):
        return PathFill("no_entry")                              # gapped past the target
    entry = entry_ts = None
    for b in bars:
        if b.ts.time() >= square_off:
            break
        if entry is None:
            if (bull and b.high >= trigger) or (not bull and b.low <= trigger):
                entry = max(trigger, b.open) if bull else min(trigger, b.open)
                entry_ts = b.ts
                # Inside the entry bar we only know the extremes. A stop touch in the same bar is
                # ambiguous (could have come before the breakout): score it pessimistically.
                stop_hit = b.low <= stop if bull else b.high >= stop
                tgt_hit = b.high >= target if bull else b.low <= target
                if stop_hit:
                    return PathFill("loss", entry, stop, entry_ts, b.ts, ambiguous=True)
                if tgt_hit:
                    return PathFill("win", entry, target, entry_ts, b.ts)  # trigger then target: same direction
            continue
        stop_hit = b.low <= stop if bull else b.high >= stop
        tgt_hit = b.high >= target if bull else b.low <= target
        if stop_hit and tgt_hit:
            return PathFill("loss", entry, stop, entry_ts, b.ts, ambiguous=True)
        if stop_hit:
            return PathFill("loss", entry, stop, entry_ts, b.ts)
        if tgt_hit:
            return PathFill("win", entry, target, entry_ts, b.ts)
    if entry is None:
        return PathFill("no_entry")
    last = [b for b in bars if b.ts.time() < square_off][-1]
    return PathFill("close", entry, last.close, entry_ts, last.ts)


def score_exact(con: sqlite3.Connection, trade: dict, bars: list[Bar], source: str) -> PathFill:
    f = simulate_path(trade["direction"], trade["trigger"], trade["stop"], trade["target"], bars)
    if f.outcome == "no_entry":
        vals = ("no_entry", None, 0.0, 0.0, 0, source)
    else:
        qty = trade.get("qty") or paper.size(f.entry, trade["stop"])
        net_, _ = paper.pnl(trade["direction"], f.entry, f.exit, qty)
        vals = (f.outcome, f.exit, round(net_, 2), round(net_ / config.RISK_PER_TRADE, 3), int(f.ambiguous), source)
    con.execute("UPDATE paper_trades SET outcome_exact=?, exit_exact=?, net_exact=?, r_exact=?, ambiguous=?, "
                "exact_source=? WHERE source=? AND signal_date=? AND symbol=? AND direction=?",
                (*vals, trade["source"], trade["signal_date"], trade["symbol"], trade["direction"]))
    con.commit()
    return f


# --------------------------------------------------------------------------- #
# Live tracker
# --------------------------------------------------------------------------- #
def todays_watchlist(con: sqlite3.Connection) -> list[dict]:
    """Morning top-10 for the latest signal date (falls back to the scan's top 10 if morning didn't run)."""
    con.row_factory = sqlite3.Row
    last = con.execute("SELECT MAX(signal_date) FROM paper_trades WHERE source='live'").fetchone()[0]
    if not last:
        return []
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM paper_trades WHERE source='live' AND signal_date=? AND morning_rank IS NOT NULL "
        "ORDER BY morning_rank", (last,))]
    if not rows:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM paper_trades WHERE source='live' AND signal_date=? ORDER BY score DESC LIMIT ?",
            (last, config.WATCHLIST_SIZE))]
    con.row_factory = None
    return rows


def _fmt_level(t: dict) -> str:
    return f"trigger {t['trigger']:,.2f} · SL {t['stop']:,.2f} · Tgt {t['target']:,.2f}"


def _pull_from_release():
    """On GitHub Actions: re-download the latest data (the morning job may have ranked the watchlist since)."""
    import os
    import subprocess
    script = "market_edge/deploy/github/data.sh"
    if os.getenv("GITHUB_ACTIONS") and os.path.exists(script):
        return lambda: subprocess.run(["bash", script, "pull"], check=False, capture_output=True, timeout=300)
    return None


def run_live(poll: int = 60, until: dtime = dtime(15, 35), now_fn=lambda: datetime.now(IST),
             sleep_fn=time.sleep, fetch_fn=fetch_path, refresh_fn="auto") -> dict:
    con = connect()
    picks = todays_watchlist(con)
    today = now_fn().date()
    if refresh_fn == "auto":
        refresh_fn = _pull_from_release()
    # Started before the morning brief ranked the watchlist? Wait (until 9:10) and re-read the data.
    while refresh_fn and picks and not any(p.get("morning_rank") for p in picks) and now_fn().time() < dtime(9, 10):
        sleep_fn(300)
        try:
            refresh_fn()
        except Exception as e:
            log.warning("data refresh failed: %s", e)
        con.close()
        con = connect()
        picks = todays_watchlist(con)
    if not picks:
        notify.send("📡 Live tracker: no watchlist found. Run `evening` (and `morning`) first.")
        return {"tracked": 0}
    state = {p["symbol"] + p["direction"]: {"t": p, "phase": "waiting", "entry": None} for p in picks}
    started = now_fn()
    # A resumed session (e.g. the afternoon job on GitHub) must not repeat alerts for events that
    # happened before it started; it silently catches up instead.
    fresh = lambda ts: ts is not None and ts >= started - timedelta(minutes=10)  # noqa: E731
    if started.time() < dtime(10, 0):
        notify.send("<b>📡 Live tracker started</b>\n" + "\n".join(
            f"{'🟢' if p['direction'] == 'bullish' else '🔴'} {p['symbol']}: {_fmt_level(p)}" for p in picks))

    seen_data, polls = False, 0
    while now_fn().time() < until:
        if now_fn().time() < MARKET_OPEN:
            sleep_fn(min(poll, 60))
            continue
        if not seen_data and polls >= 1 and now_fn().time() >= dtime(10, 0):
            notify.send("📡 Live tracker: no market data by 10:00 (NSE holiday?). Stopping for today.")
            return {"tracked": 0, "holiday": True}
        for key, s in state.items():
            t = s["t"]
            if s["phase"] in ("done", "no_entry"):
                continue
            try:
                bars, src = fetch_fn(t["symbol"], today)
            except Exception as e:
                log.warning("%s: %s", t["symbol"], e)
                continue
            if not bars:
                continue
            seen_data = True
            store_bars(con, t["symbol"], bars, src)
            f = simulate_path(t["direction"], t["trigger"], t["stop"], t["target"], bars)
            px = bars[-1].close
            bull = t["direction"] == "bullish"
            if s["phase"] == "waiting" and f.entry is not None:
                s["phase"], s["entry"] = "open", f.entry
                if fresh(f.entry_ts):
                    sz = paper.plan_size(f.entry, t["stop"])
                    notify.send(f"⚡ <b>{t['symbol']}</b> {'LONG' if bull else 'SHORT'} triggered @ {f.entry:,.2f}\n"
                                f"SL {t['stop']:,.2f} · Tgt {t['target']:,.2f} · now {px:,.2f}\n"
                                f"Qty {sz['qty']:,} · capital {paper.lakh(sz['capital'])} · at risk {paper.lakh(sz['risk_rs'])}")
            elif s["phase"] == "waiting" and f.outcome == "no_entry" and bars[0].ts.time() <= dtime(9, 20) and \
                    ((bull and bars[0].open >= t["target"]) or (not bull and bars[0].open <= t["target"])):
                s["phase"] = "no_entry"
                if fresh(bars[0].ts):
                    notify.send(f"⏭ {t['symbol']}: gapped past target at open, skipped")
            if s["phase"] == "open" and f.outcome in ("win", "loss"):
                s["phase"] = "done"
                emoji = "✅" if f.outcome == "win" else "🛑"
                if fresh(f.exit_ts):
                    notify.send(f"{emoji} <b>{t['symbol']}</b> {'target' if f.outcome == 'win' else 'stop'} hit @ "
                                f"{f.exit:,.2f} (entry {s['entry']:,.2f})" + (" · ⚠️ ambiguous bar" if f.ambiguous else ""))
        polls += 1
        if now_fn().time() >= SQUARE_OFF:
            break
        sleep_fn(poll)

    if now_fn().time() < SQUARE_OFF:     # a morning-only session: the afternoon session sends the summary
        return {"tracked": len(state), "partial": True}
    # End of day: final path and exact scoring for every pick.
    lines, total, capital = ["<b>📒 Day summary (exact, from intraday path)</b>"], 0.0, 0.0
    for s in state.values():
        t = s["t"]
        bars = load_bars(con, t["symbol"], today.isoformat())
        if not bars:
            lines.append(f"• {t['symbol']}: no intraday data")
            continue
        src = con.execute("SELECT source FROM intraday_bars WHERE symbol=? AND date=? LIMIT 1",
                          (t["symbol"], today.isoformat())).fetchone()[0]
        f = score_exact(con, t, bars, src)
        if f.outcome == "no_entry":
            lines.append(f"• {t['symbol']}: not triggered")
            continue
        row = con.execute("SELECT net_exact, r_exact FROM paper_trades WHERE source=? AND signal_date=? AND symbol=? "
                          "AND direction=?", (t["source"], t["signal_date"], t["symbol"], t["direction"])).fetchone()
        total += row[0]
        cap = (t.get("qty") or paper.size(f.entry, t["stop"])) * f.entry
        capital += cap
        lines.append(f"• {t['symbol']}: {f.outcome} · ₹{row[0]:+,.0f} ({row[1]:+.2f}R) · capital {paper.lakh(cap)}")
    lines.append(f"<b>Total: ₹{total:+,.0f}</b> (paper, after costs)")
    if capital:
        lines.append(f"<b>Capital used: {paper.lakh(capital)}</b> · return {total / capital * 100:+.2f}% on capital")
    notify.send("\n".join(lines))
    return {"tracked": len(state), "total": round(total, 2)}


# --------------------------------------------------------------------------- #
# Retroactive exact scoring
# --------------------------------------------------------------------------- #
def resolve(source: str = "live", days: int = 55, limit: int | None = None) -> dict:
    """Score already-evaluated trades exactly using Yahoo 5m bars (stored, so each day is fetched once)."""
    con = connect()
    con.row_factory = sqlite3.Row
    since = (datetime.now(IST).date() - timedelta(days=days)).isoformat()
    q = ("SELECT * FROM paper_trades WHERE source=? AND status IN ('evaluated','no_entry') AND outcome_exact IS NULL "
         "AND trade_date>=? ORDER BY trade_date DESC")
    rows = [dict(r) for r in con.execute(q, (source, since))][: limit or None]
    con.row_factory = None
    done = failed = 0
    for t in rows:
        bars = load_bars(con, t["symbol"], t["trade_date"])
        src = "stored"
        if not bars:
            try:
                bars = fetch_yahoo(t["symbol"], date.fromisoformat(t["trade_date"]))
                src = "yahoo5m"
                store_bars(con, t["symbol"], bars, src)
            except Exception as e:
                log.warning("%s %s: %s", t["symbol"], t["trade_date"], e)
                failed += 1
                continue
        if bars:
            score_exact(con, t, bars, src)
            done += 1
    con.close()
    return {"candidates": len(rows), "resolved": done, "failed": failed}


def exact_stats(source: str) -> dict:
    df = paper.load(source)
    if "outcome_exact" not in df or df["outcome_exact"].isna().all():
        return {}
    ex = df[df["outcome_exact"].notna() & (df["outcome_exact"] != "no_entry")]
    if ex.empty:
        return {"resolved": int(df["outcome_exact"].notna().sum()), "entered": 0}
    return {"resolved": int(df["outcome_exact"].notna().sum()), "entered": len(ex),
            "win_rate_pct": round(100 * (ex["net_exact"] > 0).mean(), 1),
            "avg_R": round(ex["r_exact"].mean(), 3), "total_rs": round(ex["net_exact"].sum(), 0),
            "ambiguous_pct": round(100 * ex["ambiguous"].fillna(0).mean(), 1),
            "pess_avg_R_same_trades": round(ex["r_pess"].mean(), 3),
            "opt_avg_R_same_trades": round(ex["r_opt"].mean(), 3)}


def format_exact(title: str, s: dict) -> str:
    if not s or not s.get("entered"):
        return f"<b>{title}</b>: no exact results yet."
    return (f"<b>{title}</b>\nResolved {s['resolved']} · entered {s['entered']} · win {s['win_rate_pct']}%\n"
            f"Exact avg {s['avg_R']:+.2f}R (daily-bar range was {s['pess_avg_R_same_trades']:+.2f}R to "
            f"{s['opt_avg_R_same_trades']:+.2f}R) · total ₹{s['total_rs']:+,.0f}\n"
            f"Ambiguous bars: {s['ambiguous_pct']}% (scored as losses)")


# --------------------------------------------------------------------------- #
# Moving a day's intraday bars between machines (GitHub Actions live jobs)
# --------------------------------------------------------------------------- #
def export_day(path: str, day: str) -> int:
    src = connect()
    rows = src.execute("SELECT * FROM intraday_bars WHERE date=?", (day,)).fetchall()
    src.close()
    out = sqlite3.connect(path)
    out.executescript(SCHEMA)
    out.executemany("INSERT OR REPLACE INTO intraday_bars VALUES (?,?,?,?,?,?,?,?)", rows)
    out.commit()
    out.close()
    return len(rows)


def merge_files(paths: list[str]) -> int:
    con = connect()
    n = 0
    for p in paths:
        other = sqlite3.connect(p)
        rows = other.execute("SELECT * FROM intraday_bars").fetchall()
        other.close()
        con.executemany("INSERT OR REPLACE INTO intraday_bars VALUES (?,?,?,?,?,?,?,?)", rows)
        n += len(rows)
    con.commit()
    con.close()
    return n
