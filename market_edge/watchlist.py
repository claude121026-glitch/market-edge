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
                          "reasons": reasons, "history": e})
    picks.sort(key=lambda p: -p["rank_score"])
    top = picks[:config.WATCHLIST_SIZE]
    if not record:          # display only (dashboard): don't touch the paper log
        return top
    con = paper.connect()
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
                     f"   {_hist(p.get('history'))}")
    lines.append("\n<i>Backtest (1 yr): this plan had no proven edge after costs. Paper-trade only.</i>")
    return "\n".join(lines)
