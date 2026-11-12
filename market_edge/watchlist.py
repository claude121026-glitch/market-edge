"""Morning watchlist: re-rank last evening's picks with catalysts, macro mood, sector tilt and
each setup's historical track record. Writes the rank onto the paper log."""
from __future__ import annotations

import json

from . import config, paper

# Nifty 500 industry -> news sectors (for applying the macro sector tilt)
INDUSTRY_SECTORS = {
    "Financial Services": ["Banks", "NBFC & Financials", "Insurance"],
    "Information Technology": ["IT"], "Healthcare": ["Pharma", "Healthcare"],
    "Automobile and Auto Components": ["Auto"], "Fast Moving Consumer Goods": ["FMCG"],
    "Metals & Mining": ["Metals"], "Oil Gas & Consumable Fuels": ["Oil & Gas Upstream", "OMCs"],
    "Power": ["Power", "Renewables"], "Capital Goods": ["Capital Goods & Infra", "Defence"],
    "Construction": ["Capital Goods & Infra"], "Construction Materials": ["Cement"], "Realty": ["Realty"],
    "Telecommunication": ["Telecom"], "Chemicals": ["Chemicals", "Paints"], "Textiles": ["Textiles & Exporters"],
    "Consumer Durables": ["Consumer Durables", "Gold Financiers & Jewellers"], "Services": ["Logistics", "Aviation"],
    "Media Entertainment & Publication": ["Media"],
}


def _edge() -> dict:
    try:
        return json.loads(paper.EDGE_JSON.read_text())["setups"]
    except (OSError, ValueError, KeyError):
        return {}


def build(rep: dict, mood: dict | None = None, sector_tilt: list[tuple[str, float]] | None = None,
          record: bool = True) -> list[dict]:
    edge, tilt = _edge(), dict(sector_tilt or [])
    label = (mood or {}).get("label", "Neutral")
    picks = []
    for side in ("bullish", "bearish"):
        for r in rep[side]:
            sgn = 1 if side == "bullish" else -1
            reasons, adj = [], 0.0
            cats = r.get("catalysts") or []
            if any(c != "Results filed" for c in cats):
                adj += 1; reasons.append("catalyst")
            elif cats:
                adj += 0.5; reasons.append("results day")
            secs = INDUSTRY_SECTORS.get(r.get("industry") or "", [])
            t = sum(tilt.get(s, 0) for s in secs)
            if abs(t) >= 0.4:
                adj += max(-1.5, min(1.5, sgn * t)); reasons.append(f"sector news {'with' if sgn * t > 0 else 'against'}")
            if (label == "Risk-off" and side == "bullish") or (label == "Risk-on" and side == "bearish"):
                adj -= 1; reasons.append(f"against macro ({label})")
            e = edge.get(r["setup"].split(" · ")[0])
            if e and e["n"] >= 100:
                adj += max(-3.0, min(3.0, e["intraday_pct"] * 8))   # track record weighs heavily
            if r.get("wide_stop"):
                adj -= 1.5
            picks.append({**r, "direction": side, "rank_score": round(r["score"] + adj, 2),
                          "reasons": reasons, "history": e, **paper.plan_size(r["trigger"], r["stop"])})
    picks.sort(key=lambda p: -p["rank_score"])
    top = picks[:config.WATCHLIST_SIZE]
    if not record:          # display only (dashboard): don't touch the paper log
        return top
    con = paper.connect()
    # Never re-rank a watchlist that has already been traded (e.g. a late manual re-run).
    from datetime import datetime, time as dtime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    already = con.execute("SELECT COUNT(*) FROM paper_trades WHERE source='live' AND signal_date=? AND morning_rank IS NOT NULL",
                          (rep["scan_date"],)).fetchone()[0]
    if already and now.date().isoformat() > rep["scan_date"] and now.time() >= dtime(9, 10):
        con.close()
        return top
    # A re-rank replaces the earlier ranking (otherwise a second run would leave 11-12 ranked trades).
    con.execute("UPDATE paper_trades SET morning_rank=NULL WHERE source='live' AND signal_date=?", (rep["scan_date"],))
    for i, p in enumerate(top, 1):
        con.execute("UPDATE paper_trades SET morning_rank=?, macro_label=? WHERE source='live' AND signal_date=? "
                    "AND symbol=? AND direction=?", (i, label, rep["scan_date"], p["symbol"], p["direction"]))
    con.commit()
    con.close()
    return top


def _hist(e: dict | None) -> str:
    if not e or e["n"] < 100:
        return "history: too few cases"
    verdict = "held up" if e["intraday_pct"] > 0.15 and e["quarters_positive"] >= 3 else \
              "faded" if e["intraday_pct"] < -0.1 else "no clear edge"
    return f"history: {verdict} ({e['intraday_pct']:+.2f}% next day, {e['quarters_positive']}/{e['quarters']} qtrs, n={e['n']})"


def format_telegram(top: list[dict], scan_date: str) -> str:
    if not top:
        return ""
    lines = [f"<b>🎯 Watchlist for today</b> <i>(from {scan_date} close)</i>"]
    for i, p in enumerate(top, 1):
        arrow = "🟢 LONG" if p["direction"] == "bullish" else "🔴 SHORT"
        side = "above" if p["direction"] == "bullish" else "below"
        why = f" · {', '.join(p['reasons'])}" if p["reasons"] else ""
        wide = " ⚠️wide" if p.get("wide_stop") else ""
        lines.append(f"{i}. {arrow} <b>{p['symbol']}</b> · <i>{p['setup']}</i>{why}\n"
                     f"   {side} {p['trigger']:,.1f} · SL {p['stop']:,.1f} · Tgt {p['target']:,.1f} (risk {p['risk_pct']:.1f}%{wide})\n"
                     f"   Qty {p['qty']:,} · capital {paper.lakh(p['capital'])} · at risk {paper.lakh(p['risk_rs'])}\n"
                     f"   {_hist(p.get('history'))}")
    total = sum(p["capital"] for p in top)
    lines.append(f"\n<b>Capital if all {len(top)} trigger: {paper.lakh(total)}</b> "
                 f"(₹{config.RISK_PER_TRADE:,.0f} risk per trade, max {paper.lakh(config.MAX_POSITION_VALUE)} each)")
    lines.append("<i>Backtest (1 yr): this plan had no proven edge after costs. Paper-trade only.</i>")
    return "\n".join(lines)


def chart_context(symbol: str, scan_date: str, sessions: int = 260) -> dict:
    """Daily bars up to the signal day plus the reference levels the scan used (for the chart analysis)."""
    import pandas as pd
    con = paper.connect()
    d = pd.read_sql_query("SELECT date, open, high, low, close, volume, deliv_pct FROM daily WHERE symbol=? AND series='EQ' "
                          "AND date<=? ORDER BY date DESC LIMIT ?", con, params=(symbol, scan_date, sessions))
    con.close()
    d = d.iloc[::-1].reset_index(drop=True)
    prior = d.iloc[:-1]
    return {"bars": d,
            "hh20": float(prior["high"].tail(20).max()) if len(prior) >= 15 else None,
            "ll20": float(prior["low"].tail(20).min()) if len(prior) >= 15 else None,
            "hh52": float(prior["high"].tail(250).max()) if len(prior) >= 200 else None,
            "ll52": float(prior["low"].tail(250).min()) if len(prior) >= 200 else None,
            "avg_vol20": float(prior["volume"].tail(20).mean()) if len(prior) >= 10 else None,
            "avg_deliv20": float(prior["deliv_pct"].tail(20).mean()) if len(prior) >= 10 else None}


SIGNAL_TEXT = {"VOL_SPIKE": "volume spike", "DELIV_SPIKE": "delivery spike", "HIGH_DELIV": "high delivery",
               "BRK_20D": "20-day breakout", "BRKDN_20D": "20-day breakdown", "HIGH_52W": "new 52-week high",
               "LOW_52W": "new 52-week low", "BIG_MOVE": "big move"}


def analysis_lines(p: dict, ctx: dict) -> list[str]:
    """Plain-language reading of the chart: what the scan saw on the signal day and what the plan is."""
    out = []
    last = ctx["bars"].iloc[-1]
    long_ = p["direction"] == "bullish"
    out.append(f"Closed ₹{last['close']:,.2f} ({p.get('chg_pct', 0):+.1f}%) on {p.get('vol_x', 0):.1f}× its 20-day average volume"
               + (f", delivery {last['deliv_pct']:.0f}% vs {ctx['avg_deliv20']:.0f}% usual" if ctx.get("avg_deliv20") and last["deliv_pct"] == last["deliv_pct"] else ""))
    if ctx.get("hh20") and ctx.get("ll20"):
        if last["close"] > ctx["hh20"]:
            out.append(f"Broke above the 20-day high of ₹{ctx['hh20']:,.2f}")
        elif last["close"] < ctx["ll20"]:
            out.append(f"Broke below the 20-day low of ₹{ctx['ll20']:,.2f}")
        else:
            out.append(f"Inside its 20-day range ₹{ctx['ll20']:,.2f}–{ctx['hh20']:,.2f}")
    if ctx.get("hh52") and last["close"] > ctx["hh52"]:
        out.append(f"New 52-week high (previous ₹{ctx['hh52']:,.2f})")
    elif ctx.get("ll52") and last["close"] < ctx["ll52"]:
        out.append(f"New 52-week low (previous ₹{ctx['ll52']:,.2f})")
    out.append(f"Signals: {', '.join(SIGNAL_TEXT.get(x, x) for x in p.get('signals', []))}")
    side = "above" if long_ else "below"
    out.append(f"Plan: {'buy' if long_ else 'short'} {side} ₹{p['trigger']:,.2f}, stop ₹{p['stop']:,.2f} (pivot), "
               f"target ₹{p['target']:,.2f} (2× risk), square off by 3:20 PM")
    out.append(f"Qty {p['qty']:,} · capital {paper.lakh(p['capital'])} · at risk {paper.lakh(p['risk_rs'])}")
    e = p.get("history")
    if e and e.get("n", 0) >= 100:
        out.append(f"This setup's record: {e['intraday_pct']:+.2f}% next day on average, {e['hit_pct']:.0f}% hit rate, "
                   f"{e['quarters_positive']}/{e['quarters']} quarters positive ({e['n']} past cases, before costs)")
    if p.get("catalysts"):
        out.append(f"Catalyst: {', '.join(p['catalysts'])}")
    if p.get("reasons"):
        out.append(f"Ranking notes: {', '.join(p['reasons'])}")
    return out


def format_analysis(top: list[dict], contexts: dict) -> str:
    """Text version of the chart analysis (WhatsApp, or if the charts can't be drawn)."""
    if not top:
        return ""
    parts = ["<b>📈 Chart analysis for today's watchlist</b>"]
    for i, p in enumerate(top, 1):
        ctx = contexts.get(p["symbol"])
        if not ctx or ctx["bars"].empty:
            continue
        side = "LONG" if p["direction"] == "bullish" else "SHORT"
        parts.append(f"\n<b>{i}. {p['symbol']} · {side}</b> · <i>{p['setup']}</i>")
        parts += [f"• {line}" for line in analysis_lines(p, ctx)]
    return "\n".join(parts)
