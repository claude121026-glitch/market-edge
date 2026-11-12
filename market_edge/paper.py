"""Step 5: paper trading log + backtest of the evening scan.

Trade plan (from the scan): bullish = buy if next session trades above `trigger` (today's high),
stop = pivot, target = 2R; bearish mirrored (intraday short). Square off at close.

Only DAILY bars are available, so the order of high/low within the day is unknown. Every trade is
therefore simulated twice:
  pessimistic: if the stop was touched at any point after entry was possible -> loss;
               if both stop and target were touched -> loss.
  optimistic:  if the target was touched -> win; a stop touch counts only if the close confirms it.
The real result lies between the two. Costs and slippage are always deducted.
"""
from __future__ import annotations

import json
import math
import random
import sqlite3
from dataclasses import dataclass

import pandas as pd

from . import config, evening_scan, nse

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_trades (
    source TEXT, signal_date TEXT, symbol TEXT, direction TEXT, rank INTEGER, morning_rank INTEGER,
    setup TEXT, signals TEXT, score REAL, catalysts TEXT, macro_label TEXT, wide_stop INTEGER,
    trigger REAL, stop REAL, target REAL,
    trade_date TEXT, status TEXT,                          -- pending | no_entry | evaluated
    entry REAL, qty INTEGER,
    exit_pess REAL, outcome_pess TEXT, net_pess REAL, r_pess REAL,
    exit_opt REAL, outcome_opt TEXT, net_opt REAL, r_opt REAL, costs REAL,
    PRIMARY KEY (source, signal_date, symbol, direction)
);
"""


def connect() -> sqlite3.Connection:
    con = nse.connect()
    con.executescript(SCHEMA)
    return con


# --------------------------------------------------------------------------- #
# Costs & simulation
# --------------------------------------------------------------------------- #
def costs(buy_value: float, sell_value: float) -> float:
    brok = sum(min(v * config.BROKERAGE_PCT / 100, config.BROKERAGE_MAX) for v in (buy_value, sell_value))
    exch = (buy_value + sell_value) * config.EXCHANGE_PCT / 100
    sebi = (buy_value + sell_value) * config.SEBI_PCT / 100
    stt = sell_value * config.STT_SELL_PCT / 100
    stamp = buy_value * config.STAMP_BUY_PCT / 100
    gst = (brok + exch + sebi) * config.GST_PCT / 100
    return brok + exch + sebi + stt + stamp + gst


@dataclass
class Fill:
    outcome: str          # no_entry | win | loss | close
    entry: float | None = None
    exit: float | None = None


def simulate(direction: str, trigger: float, stop: float, target: float,
             o: float, h: float, l: float, c: float, mode: str) -> Fill:
    bull = direction == "bullish"
    if bull:
        if h < trigger or o >= target:      # never triggered, or gapped past the target
            return Fill("no_entry")
        entry = max(trigger, o)
        hit_stop, hit_tgt, close_bad = l <= stop, h >= target, c <= stop
    else:
        if l > trigger or o <= target:
            return Fill("no_entry")
        entry = min(trigger, o)
        hit_stop, hit_tgt, close_bad = h >= stop, l <= target, c >= stop
    if mode == "pess":
        if hit_stop:
            return Fill("loss", entry, stop)
        if hit_tgt:
            return Fill("win", entry, target)
    else:
        if hit_tgt:
            return Fill("win", entry, target)
        if hit_stop and close_bad:
            return Fill("loss", entry, stop)
    return Fill("close", entry, c)


def pnl(direction: str, entry: float, exit_: float, qty: int) -> tuple[float, float]:
    """(net P&L after slippage and costs, costs)."""
    s = config.SLIPPAGE_PCT / 100
    if direction == "bullish":
        buy, sell = entry * (1 + s), exit_ * (1 - s)
    else:
        sell, buy = entry * (1 - s), exit_ * (1 + s)
    ch = costs(buy * qty, sell * qty)
    return (sell - buy) * qty - ch, ch


def size(entry: float, stop: float) -> int:
    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0:
        return 0
    return int(max(0, math.floor(min(config.RISK_PER_TRADE / risk_per_share, config.MAX_POSITION_VALUE / entry))))


def plan_size(entry: float, stop: float) -> dict:
    """Quantity, capital used (position value) and money at risk for a trade entered at `entry`."""
    qty = size(entry, stop)
    return {"qty": qty, "capital": round(qty * entry, 2), "risk_rs": round(qty * abs(entry - stop), 2)}


def lakh(v: float) -> str:
    """Indian-style money: Rs 48,150 / Rs 4.21 lakh / Rs 1.2 cr."""
    v = float(v)
    if abs(v) >= 1e7:
        return f"₹{v / 1e7:.2f} cr"
    if abs(v) >= 1e5:
        return f"₹{v / 1e5:.2f} lakh"
    return f"₹{v:,.0f}"


def evaluate_row(r: dict, bar: dict) -> dict:
    out = {"trade_date": bar["date"]}
    fp = simulate(r["direction"], r["trigger"], r["stop"], r["target"], bar["open"], bar["high"], bar["low"], bar["close"], "pess")
    if fp.outcome == "no_entry":
        return {**out, "status": "no_entry"}
    fo = simulate(r["direction"], r["trigger"], r["stop"], r["target"], bar["open"], bar["high"], bar["low"], bar["close"], "opt")
    qty = size(fp.entry, r["stop"])
    if qty == 0:
        return {**out, "status": "no_entry"}
    risk = config.RISK_PER_TRADE
    np_, ch = pnl(r["direction"], fp.entry, fp.exit, qty)
    no_, _ = pnl(r["direction"], fo.entry, fo.exit, qty)
    return {**out, "status": "evaluated", "entry": fp.entry, "qty": qty, "costs": round(ch, 2),
            "exit_pess": fp.exit, "outcome_pess": fp.outcome, "net_pess": round(np_, 2), "r_pess": round(np_ / risk, 3),
            "exit_opt": fo.exit, "outcome_opt": fo.outcome, "net_opt": round(no_, 2), "r_opt": round(no_ / risk, 3)}


# --------------------------------------------------------------------------- #
# Live paper log
# --------------------------------------------------------------------------- #
COLS = ["source", "signal_date", "symbol", "direction", "rank", "setup", "signals", "score", "catalysts",
        "wide_stop", "trigger", "stop", "target", "status"]


def record_from_report(rep: dict, source: str = "live") -> int:
    con = connect()
    n = 0
    for side in ("bullish", "bearish"):
        for i, r in enumerate(rep[side], 1):
            cur = con.execute(f"INSERT OR IGNORE INTO paper_trades ({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))})",
                        (source, rep["scan_date"], r["symbol"], side, i, r["setup"], json.dumps(r["signals"]),
                         r["score"], json.dumps(r.get("catalysts", [])), int(bool(r["wide_stop"])),
                         r["trigger"], r["stop"], r["target"], "pending"))
            n += cur.rowcount
    con.commit()
    con.close()
    return n


def finalize_our_trades() -> dict:
    """Our trades are the morning top-10 watchlist only. Once a signal day's trade day is in the data,
    the other scan picks are moved to source 'scan_only' (kept for reference, excluded from every
    result and capital figure). If the morning never ranked a day, the live tracker's fallback is used:
    the top WATCHLIST_SIZE by score."""
    con = connect()
    latest = con.execute("SELECT MAX(date) FROM daily").fetchone()[0]
    days = [r[0] for r in con.execute(
        "SELECT DISTINCT signal_date FROM paper_trades WHERE source IN ('live','scan_only')")]
    done_days = moved = 0
    for sd in days:
        td = con.execute("SELECT MIN(date) FROM daily WHERE date > ?", (sd,)).fetchone()[0]
        if not td or not latest or td > latest:
            continue                                   # not traded yet: keep every pick available
        rows = con.execute("SELECT source, symbol, direction, morning_rank, score FROM paper_trades "
                           "WHERE source IN ('live','scan_only') AND signal_date=?", (sd,)).fetchall()
        ranked = {(s, d) for _, s, d, mr, _ in rows if mr is not None}
        ours = ranked or {(s, d) for _, s, d, _, _ in sorted(rows, key=lambda r: -(r[4] or 0))[:config.WATCHLIST_SIZE]}
        for src, s, d, _, _ in rows:
            want = "live" if (s, d) in ours else "scan_only"
            if src != want:
                con.execute("UPDATE paper_trades SET source=? WHERE source=? AND signal_date=? AND symbol=? AND direction=?",
                            (want, src, sd, s, d))
                moved += 1
        done_days += 1
    con.commit()
    con.close()
    return {"days": done_days, "moved": moved}


def evaluate_pending() -> int:
    """Score pending trades whose next session's data is now available."""
    con = connect()
    con.row_factory = sqlite3.Row
    pend = [dict(r) for r in con.execute("SELECT * FROM paper_trades WHERE status='pending' AND source='live'")]
    done = 0
    for r in pend:
        bar = con.execute("SELECT date, open, high, low, close FROM daily WHERE symbol=? AND series='EQ' AND date>? "
                          "ORDER BY date LIMIT 1", (r["symbol"], r["signal_date"])).fetchone()
        if not bar:
            continue
        res = evaluate_row(r, dict(bar))
        sets = ", ".join(f"{k}=?" for k in res)
        con.execute(f"UPDATE paper_trades SET {sets} WHERE source=? AND signal_date=? AND symbol=? AND direction=?",
                    (*res.values(), r["source"], r["signal_date"], r["symbol"], r["direction"]))
        done += 1
    con.commit()
    con.close()
    return done


# --------------------------------------------------------------------------- #
# Backtest
# --------------------------------------------------------------------------- #
def backtest(sessions: int = 260, seed: int = 7) -> dict:
    """Replay the evening scan on every past session and paper-trade the next session.
    Also runs a random baseline (same rules, random liquid stocks) to see if the signals add value."""
    con = connect()
    hist = evening_scan.load_history(con, None, sessions + 30)
    sig = evening_scan.compute_signals(hist, all_dates=True)
    all_liquid = evening_scan.compute_signals(hist, all_dates=True, keep_all=True)
    bars = hist.set_index(["symbol", "date"])[["open", "high", "low", "close"]]
    dates = sorted(hist["date"].unique())
    next_date = dict(zip(dates[:-1], dates[1:]))

    con.execute("DELETE FROM paper_trades WHERE source IN ('backtest','baseline')")
    rng = random.Random(seed)
    rows = []
    for d, day in sig.groupby("date"):
        nd = next_date.get(d)
        if nd is None:
            continue
        picks = []
        for side, n in (("bullish", config.TOP_BULLISH), ("bearish", config.TOP_BEARISH)):
            sub = day[day["direction"] == side].sort_values(["score", "vol_x"], ascending=False).head(n)
            picks += [("backtest", side, i, r) for i, r in enumerate(sub.to_dict("records"), 1)]
        pool = all_liquid[all_liquid["date"] == d].to_dict("records")
        for i, r in enumerate(rng.sample(pool, min(len(picks), len(pool))), 1):
            picks.append(("baseline", r["direction"], i, r))
        for source, side, i, r in picks:
            key = (r["symbol"], nd)
            if key not in bars.index:
                continue
            b = bars.loc[key]
            bar = {"date": nd, "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"]}
            base = {"source": source, "signal_date": d, "symbol": r["symbol"], "direction": side, "rank": i,
                    "setup": r["setup"], "signals": json.dumps(r["signals"]), "score": float(r["score"]),
                    "catalysts": "[]", "wide_stop": int(bool(r["wide_stop"])),
                    "trigger": float(r["trigger"]), "stop": float(r["stop"]), "target": float(r["target"])}
            rows.append({**base, **evaluate_row(base, bar)})
    df = pd.DataFrame(rows)
    df.to_sql("paper_trades", con, if_exists="append", index=False)
    con.commit()
    con.close()
    return {"sessions": df["signal_date"].nunique() if len(df) else 0, "trades": len(df)}


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #
def load(source: str) -> pd.DataFrame:
    con = connect()
    df = pd.read_sql_query("SELECT * FROM paper_trades WHERE source=?", con, params=(source,))
    con.close()
    return df


def stats(df: pd.DataFrame) -> dict:
    ev = df[df["status"] == "evaluated"]
    out = {"signals": len(df), "entered": len(ev),
           "fill_rate_pct": round(100 * len(ev) / len(df), 1) if len(df) else 0.0}
    if ev.empty:
        return out
    for m in ("pess", "opt"):
        net, r = ev[f"net_{m}"], ev[f"r_{m}"]
        daily = ev.groupby("trade_date")[f"net_{m}"].sum().sort_index().cumsum()
        gains, losses = net[net > 0].sum(), -net[net < 0].sum()
        out[m] = {
            "win_rate_pct": round(100 * (net > 0).mean(), 1),
            "avg_R": round(r.mean(), 3),
            "expectancy_rs": round(net.mean(), 0),
            "total_rs": round(net.sum(), 0),
            "profit_factor": round(gains / losses, 2) if losses else None,
            "max_drawdown_rs": round((daily.cummax() - daily).max(), 0),
        }
    out["costs_per_trade_rs"] = round(ev["costs"].mean(), 0)
    cap = ev["entry"] * ev["qty"]
    out["capital_per_trade_rs"] = round(cap.mean(), 0)
    out["max_daily_capital_rs"] = round(cap.groupby(ev["trade_date"]).sum().max(), 0)
    return out


def breakdown(df: pd.DataFrame, by: str) -> pd.DataFrame:
    ev = df[df["status"] == "evaluated"]
    if ev.empty:
        return pd.DataFrame()
    g = ev.groupby(by).agg(trades=("r_pess", "size"), avg_R_pess=("r_pess", "mean"), avg_R_opt=("r_opt", "mean"),
                           win_pess=("net_pess", lambda s: (s > 0).mean() * 100))
    return g.round(2).sort_values("trades", ascending=False)


def format_stats(title: str, s: dict) -> str:
    if "pess" not in s:
        return f"<b>{title}</b>: {s['signals']} signals, none entered/evaluated yet."
    p, o = s["pess"], s["opt"]
    return (f"<b>{title}</b>\n"
            f"Signals {s['signals']} · entered {s['entered']} ({s['fill_rate_pct']}%) · costs ~₹{s['costs_per_trade_rs']:.0f}/trade\n"
            f"Win rate {p['win_rate_pct']}–{o['win_rate_pct']}% · avg {p['avg_R']:+.2f}R to {o['avg_R']:+.2f}R\n"
            f"Per trade ₹{p['expectancy_rs']:+,.0f} to ₹{o['expectancy_rs']:+,.0f} · "
            f"total ₹{p['total_rs']:+,.0f} to ₹{o['total_rs']:+,.0f}\n"
            f"Max drawdown ₹{p['max_drawdown_rs']:,.0f} (pessimistic)\n"
            f"Capital used: {lakh(s['capital_per_trade_rs'])} per trade on average · "
            f"busiest day {lakh(s['max_daily_capital_rs'])}")


# --------------------------------------------------------------------------- #
# Setup track record (exact, from daily data: no intraday ambiguity)
# --------------------------------------------------------------------------- #
EDGE_JSON = config.DATA_DIR / "setup_edge.json"


def setup_edge(source: str = "backtest") -> dict:
    """Per setup: mean next-day open->close move in the predicted direction, hit rate, 5-day move,
    and how many of 4 quarters were positive. Saved for the morning watchlist."""
    con = connect()
    px = pd.read_sql_query("SELECT symbol, date, open, close FROM daily WHERE series='EQ'", con)
    con.close()
    px = px.sort_values(["symbol", "date"])
    g = px.groupby("symbol")
    px["nopen"], px["nclose"], px["c5"] = g["open"].shift(-1), g["close"].shift(-1), g["close"].shift(-5)
    df = load(source).rename(columns={"signal_date": "date"})
    if df.empty:
        return {}
    j = df.merge(px, on=["symbol", "date"]).dropna(subset=["nopen", "nclose"])
    sgn = j["direction"].map({"bullish": 1, "bearish": -1})
    j["intraday"] = sgn * (j["nclose"] / j["nopen"] - 1) * 100
    j["five_day"] = sgn * (j["c5"] / j["close"] - 1) * 100
    j["setup0"] = j["setup"].str.split(" · ").str[0]
    dates = sorted(j["date"].unique())
    cuts = [dates[int(len(dates) * k / 4)] for k in (1, 2, 3)]
    j["q"] = j["date"].apply(lambda d: sum(d >= c for c in cuts))
    out = {}
    for s, t in j.groupby("setup0"):
        qm = t.groupby("q")["intraday"].mean()
        out[s] = {"n": int(len(t)), "intraday_pct": round(float(t["intraday"].mean()), 3),
                  "hit_pct": round(float((t["intraday"] > 0).mean() * 100), 1),
                  "five_day_pct": round(float(t["five_day"].mean()), 3),
                  "quarters_positive": int((qm > 0).sum()), "quarters": int(len(qm))}
    EDGE_JSON.write_text(json.dumps({"source": source, "from": dates[0], "to": dates[-1], "setups": out}, indent=2))
    return out
