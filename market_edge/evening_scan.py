"""Evening scan: after NSE publishes the day's file (~6 PM IST), find tomorrow's candidates.

Signals per stock (vs its own previous 20 sessions unless noted):
  VOL_SPIKE     volume >= 3x average
  DELIV_SPIKE   delivered qty >= 3x average            (real buying/selling, not just churn)
  HIGH_DELIV    delivery % >= 60 and >= 1.5x its average
  BRK_20D       close above prior 20-day high          (BRKDN_20D: below 20-day low)
  HIGH_52W      close above prior 52-week high         (LOW_52W: below 52-week low)
  BIG_MOVE      |change| >= 5%
Liquidity filter: EQ series, price >= Rs 20, 20-day avg turnover >= Rs 10 crore.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pandas as pd

from . import config, nse

WEIGHTS = {"VOL_SPIKE": 2, "DELIV_SPIKE": 2, "HIGH_DELIV": 1, "BRK_20D": 1, "BRKDN_20D": 1,
           "HIGH_52W": 2, "LOW_52W": 2, "BIG_MOVE": 1}


def load_history(con: sqlite3.Connection, scan_date: str | None = None, sessions: int = 260) -> pd.DataFrame:
    if scan_date is None:
        scan_date = con.execute("SELECT MAX(date) FROM daily").fetchone()[0]
    if scan_date is None:
        raise SystemExit("No data yet. Run:  python -m market_edge.run update --days 380")
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM daily WHERE date <= ? ORDER BY date DESC LIMIT ?", (scan_date, sessions))]
    q = f"SELECT * FROM daily WHERE series IN ({','.join('?' * len(config.SERIES))}) AND date >= ? AND date <= ?"
    df = pd.read_sql_query(q, con, params=(*config.SERIES, dates[-1], scan_date))
    # Stocks only: drop ETFs / funds (they are not in NSE's equity list).
    equities = {r[0] for r in con.execute("SELECT symbol FROM companies WHERE is_equity=1")}
    if equities:
        df = df[df["symbol"].isin(equities)]
    return df


def compute_signals(df: pd.DataFrame, scan_date: str | None = None, all_dates: bool = False,
                    keep_all: bool = False) -> pd.DataFrame:
    """Signals for scan_date (default: latest), or for every date when all_dates=True (backtests).
    Only data up to and including each date is used (rolling windows are shifted), so no look-ahead."""
    df = df.sort_values(["symbol", "date"]).copy()
    scan_date = scan_date or df["date"].max()
    g = df.groupby("symbol", sort=False)

    def prior(col, fn, window, min_periods):
        return g[col].transform(lambda s: getattr(s.shift(1).rolling(window, min_periods=min_periods), fn)())

    df["avg_vol20"] = prior("volume", "mean", 20, 10)
    df["avg_deliv20"] = prior("deliv_qty", "mean", 20, 10)
    df["avg_delivpct20"] = prior("deliv_pct", "mean", 20, 10)
    df["avg_turn20_cr"] = prior("turnover_lacs", "mean", 20, 10) / 100
    df["hh20"] = prior("high", "max", 20, 15)
    df["ll20"] = prior("low", "min", 20, 15)
    df["hh52w"] = prior("high", "max", 250, 200)
    df["ll52w"] = prior("low", "min", 250, 200)
    df["sessions"] = g.cumcount() + 1

    t = df.copy() if all_dates else df[df["date"] == scan_date].copy()
    t = t[(t["close"] >= config.MIN_PRICE) & (t["avg_turn20_cr"] >= config.MIN_AVG_TURNOVER_CR)]

    t["chg_pct"] = (t["close"] / t["prev_close"] - 1) * 100
    t["vol_x"] = t["volume"] / t["avg_vol20"]
    t["deliv_x"] = t["deliv_qty"] / t["avg_deliv20"]

    flags = {
        "VOL_SPIKE": t["vol_x"] >= config.VOL_SPIKE_X,
        # Delivered qty spike only counts if delivery % is also meaningful; otherwise it is
        # just a by-product of a volume spike (speculative churn, not accumulation).
        "DELIV_SPIKE": (t["deliv_x"] >= config.DELIV_SPIKE_X)
                       & (t["deliv_pct"] >= t["avg_delivpct20"].clip(lower=config.MIN_DELIV_PCT_FOR_SPIKE)),
        "HIGH_DELIV": (t["deliv_pct"] >= config.HIGH_DELIV_PCT) & (t["deliv_pct"] >= 1.5 * t["avg_delivpct20"]),
        "BRK_20D": t["close"] > t["hh20"],
        "BRKDN_20D": t["close"] < t["ll20"],
        "HIGH_52W": t["close"] > t["hh52w"],
        "LOW_52W": t["close"] < t["ll52w"],
        "BIG_MOVE": t["chg_pct"].abs() >= config.BIG_MOVE_PCT,
    }
    for k, v in flags.items():
        t[k] = v.fillna(False).astype(bool)
    t["signals"] = t[list(flags)].apply(lambda r: [k for k in flags if r[k]], axis=1)
    t["score"] = t[list(flags)].mul(pd.Series(WEIGHTS)).sum(axis=1)

    # A breakout and a breakdown can't both be "the" direction; use the day's move.
    t["direction"] = t["chg_pct"].apply(lambda c: "bullish" if c > 0 else "bearish")
    t["setup"] = t.apply(_setup_label, axis=1)

    # Next-day intraday plan: trade a break of today's extreme, stop at the pivot, 2R target.
    p = (t["high"] + t["low"] + t["close"]) / 3
    t["pivot"], t["r1"], t["s1"] = p, 2 * p - t["low"], 2 * p - t["high"]
    bull = t["direction"] == "bullish"
    t["trigger"] = t["high"].where(bull, t["low"])
    t["stop"] = p
    risk = (t["trigger"] - t["stop"]).abs()
    t["target"] = (t["trigger"] + 2 * risk).where(bull, t["trigger"] - 2 * risk)
    t["risk_pct"] = risk / t["trigger"] * 100
    t["wide_stop"] = t["risk_pct"] > config.MAX_INTRADAY_RISK_PCT
    if not keep_all:  # keep_all: every liquid stock (used for the random baseline)
        t = t[t["score"] > 0]
    return t.sort_values(["score", "vol_x"], ascending=False)


def _setup_label(r) -> str:
    up = r["chg_pct"] > 0
    if r["DELIV_SPIKE"] or r["HIGH_DELIV"]:
        base = "Accumulation" if up else "Distribution"
    elif r["VOL_SPIKE"]:
        base = "Volume breakout" if up else "Volume breakdown"
    else:
        base = "Momentum" if up else "Weakness"
    if r["HIGH_52W"]:
        base += " · 52W high"
    elif r["LOW_52W"]:
        base += " · 52W low"
    elif r["BRK_20D"]:
        base += " · 20D breakout"
    elif r["BRKDN_20D"]:
        base += " · 20D breakdown"
    return base


def market_breadth(df: pd.DataFrame, scan_date: str) -> dict:
    t = df[df["date"] == scan_date]
    chg = t["close"] / t["prev_close"] - 1
    return {"advances": int((chg > 0).sum()), "declines": int((chg < 0).sum()),
            "unchanged": int((chg == 0).sum())}


def run(scan_date: str | None = None) -> dict:
    con = nse.connect()
    hist = load_history(con, scan_date)
    scan_date = scan_date or hist["date"].max()
    res = compute_signals(hist, scan_date)
    names = pd.read_sql_query("SELECT symbol, name, industry, in_nifty500 FROM companies", con)
    con.close()
    res = res.merge(names, on="symbol", how="left")

    def pack(rows):
        cols = ["symbol", "name", "industry", "setup", "score", "signals", "close", "chg_pct", "vol_x",
                "deliv_pct", "deliv_x", "high", "low", "pivot", "r1", "s1", "trigger", "stop", "target",
                "risk_pct", "wide_stop", "avg_turn20_cr", "in_nifty500"]
        out = rows[cols].copy()
        out = out.astype(object).where(pd.notna(out), None)
        return [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items()} for r in out.to_dict("records")]

    bull = res[res["direction"] == "bullish"].head(config.TOP_BULLISH)
    bear = res[res["direction"] == "bearish"].head(config.TOP_BEARISH)
    report = {
        "scan_date": scan_date,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "breadth": market_breadth(hist, scan_date),
        "universe": int(((hist["date"] == scan_date)).sum()),
        "flagged": int(len(res)),
        "bullish": pack(bull),
        "bearish": pack(bear),
    }
    config.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (config.REPORT_DIR / f"evening_{scan_date}.json").write_text(json.dumps(report, indent=2, default=str))
    return report


def attach_catalysts(rep: dict, tags: dict[str, list[str]]) -> None:
    """Mark watchlist stocks that also have a corporate catalyst (announcement / bulk deal)."""
    for side in ("bullish", "bearish"):
        for r in rep[side]:
            r["catalysts"] = tags.get(r["symbol"], [])


def format_telegram(rep: dict) -> str:
    b = rep["breadth"]
    lines = [f"<b>📊 Evening Scan · {rep['scan_date']}</b>",
             f"Breadth: 🟢 {b['advances']} ▲ / 🔴 {b['declines']} ▼ · {rep['flagged']} stocks flagged", ""]

    def row(r):
        sig = ", ".join(s.replace("_", " ") for s in r["signals"])
        deliv = f" · Deliv {r['deliv_pct']:.0f}%" if r["deliv_pct"] is not None else ""
        side = "above" if r["trigger"] >= r["stop"] else "below"
        wide = " ⚠️wide" if r["wide_stop"] else ""
        cat = f"\n   📌 {', '.join(r['catalysts'])}" if r.get("catalysts") else ""
        return (f"<b>{r['symbol']}</b> ₹{r['close']:,.2f} ({r['chg_pct']:+.1f}%) · <i>{r['setup']}</i>{cat}\n"
                f"   Vol {r['vol_x']:.1f}x{deliv} · {sig}\n"
                f"   Plan: {side} {r['trigger']:,.1f} · SL {r['stop']:,.1f} · Tgt {r['target']:,.1f} "
                f"(risk {r['risk_pct']:.1f}%{wide})")

    if rep["bullish"]:
        lines.append("<b>🟢 Bullish watch</b>")
        lines += [row(r) for r in rep["bullish"][:10]]
        lines.append("")
    if rep["bearish"]:
        lines.append("<b>🔴 Bearish watch</b>")
        lines += [row(r) for r in rep["bearish"][:6]]
    lines.append("\n<i>Screening output, not advice. Paper-trade first.</i>")
    return "\n".join(lines)
