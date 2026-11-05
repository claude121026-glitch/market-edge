"""Global macro prices board (Yahoo Finance chart endpoint, free, no key).

Fetches each ticker politely (1 request/sec, backoff on 429), keeps the last good value
when a fetch fails (marked stale), computes 1-day / 1-week / 1-month change, raises alerts
on big moves, and adds a simple rule-based "what it means for India" read.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import config, net, nse

log = logging.getLogger("market_edge.global")
IST = ZoneInfo("Asia/Kolkata")
SUSPECT_FUTURES_MOVE = 6.0  # % in a day: flag for manual check (possible contract roll)
CHART_URL = "https://query{n}.finance.yahoo.com/v8/finance/chart/{sym}?range=1mo&interval=1d"


def parse_chart(payload: dict) -> dict:
    res = payload["chart"]["result"][0]
    meta = res["meta"]
    closes = [c for c in res["indicators"]["quote"][0].get("close", []) if c is not None]
    price = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
    if price is None or len(closes) < 2:
        raise ValueError("not enough data")
    # Last daily bar = current/latest session; the one before it = previous close.
    prev = closes[-2]
    week = closes[-6] if len(closes) >= 6 else closes[0]
    month = closes[0]
    ts = meta.get("regularMarketTime")
    return {
        "price": float(price), "prev_close": float(prev),
        "chg_1d_pct": (price / prev - 1) * 100,
        "chg_1w_pct": (price / week - 1) * 100,
        "chg_1m_pct": (price / month - 1) * 100,
        "chg_1d_abs": price - prev,
        "currency": meta.get("currency"),
        "market_time_utc": datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None,
    }


def fetch_one(yahoo_symbol: str, attempt_host: int = 1) -> dict:
    sym = urllib.parse.quote(yahoo_symbol, safe="")
    raw = net.get(CHART_URL.format(n=attempt_host, sym=sym), timeout=20, retries=2, backoff=(4, 10))
    return parse_chart(json.loads(raw))


def _load_cache() -> dict:
    try:
        return {r["key"]: r for r in json.loads(config.GLOBAL_JSON.read_text())["prices"]}
    except (OSError, ValueError, KeyError):
        return {}


def run() -> dict:
    cache = _load_cache()
    now = datetime.now(timezone.utc)
    rows, alerts = [], []
    for group, key, name, ysym, kind, alert, level_alert in config.GLOBAL_TICKERS:
        row = {"group": group, "key": key, "name": name, "symbol": ysym, "kind": kind}
        try:
            try:
                data = fetch_one(ysym, 1)
            except net.HttpError:
                data = fetch_one(ysym, 2)  # second Yahoo host
            row.update(data, stale=False, fetched_at=now.isoformat(timespec="seconds"))
        except Exception as e:
            old = cache.get(key)
            if old and "price" in old:
                row.update({k: v for k, v in old.items() if k not in row}, stale=True)
            row["error"] = str(e)[:120]
            log.warning("%s (%s): %s", name, ysym, row["error"])
        if "price" in row and not row.get("stale"):
            # Continuous futures (=F) can jump when Yahoo rolls to the next contract.
            row["verify"] = ysym.endswith("=F") and abs(row["chg_1d_pct"]) >= SUSPECT_FUTURES_MOVE
            a = _alert(row, kind, alert, level_alert)
            if a:
                alerts.append(a)
        rows.append(row)

    payload = {
        "generated_at_utc": now.isoformat(timespec="seconds"),
        "generated_at_ist": now.astimezone(IST).strftime("%Y-%m-%d %H:%M"),
        "prices": rows,
        "alerts": alerts,
        "india_read": india_read({r["key"]: r for r in rows if "price" in r}),
        "missing": [r["name"] for r in rows if "price" not in r],
    }
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.GLOBAL_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(config.GLOBAL_JSON)
    return payload


def _alert(r: dict, kind: str, threshold: float, level_alert: float | None) -> str | None:
    msgs = []
    if kind == "bp":
        bp = r["chg_1d_abs"] * 100
        if abs(bp) >= threshold:
            msgs.append(f"{r['name']} {bp:+.0f}bp to {r['price']:.2f}%")
    elif abs(r["chg_1d_pct"]) >= threshold:
        msgs.append(f"{r['name']} {r['chg_1d_pct']:+.1f}% to {r['price']:,.2f}"
                    + (" ⚠️ verify: possible futures contract roll" if r.get("verify") else ""))
    if level_alert is not None and r["price"] >= level_alert:
        msgs.append(f"{r['name']} at {r['price']:.1f} (≥ {level_alert}: fear elevated)")
    return " · ".join(msgs) or None


def _move_level(r: dict, kind: str, threshold: float) -> int:
    move = abs(r["chg_1d_abs"] * 100) if kind == "bp" else abs(r["chg_1d_pct"])
    return int(move // threshold) if threshold else 0


def new_alerts(payload: dict, today: str | None = None) -> list[str]:
    """Alerts not yet sent today. A market alerts once when it first crosses its threshold and again only
    if the move doubles, triples and so on, so a 30-minute refresh never repeats the same message."""
    today = today or datetime.now(IST).date().isoformat()
    spec = {t[1]: t for t in config.GLOBAL_TICKERS}
    con = nse.connect()
    con.execute("CREATE TABLE IF NOT EXISTS price_alerts (date TEXT, key TEXT, level INTEGER, PRIMARY KEY (date, key))")
    out = []
    for r in payload["prices"]:
        if "price" not in r or r.get("stale") or r["key"] not in spec:
            continue
        _, key, _, _, kind, thr, lvl_alert = spec[r["key"]]
        text = _alert(r, kind, thr, lvl_alert)
        if not text:
            continue
        level = _move_level(r, kind, thr) + (1000 if lvl_alert is not None and r["price"] >= lvl_alert else 0)
        prev = con.execute("SELECT level FROM price_alerts WHERE date=? AND key=?", (today, key)).fetchone()
        if prev is None or level > prev[0]:
            out.append(text)
            con.execute("INSERT OR REPLACE INTO price_alerts VALUES (?,?,?)", (today, key, level))
    con.commit()
    con.close()
    return out


def india_read(p: dict) -> list[str]:
    """Plain rules of thumb for how overnight global moves tend to hit Indian stocks."""
    out = []
    g = lambda k: p.get(k, {}).get("chg_1d_pct")  # noqa: E731
    brent, dxy, inr, gold = g("BRENT"), g("DXY"), g("USDINR"), g("GOLD")
    spx, nasdaq = g("SPX"), g("NASDAQ")
    us10y = p.get("US10Y", {}).get("chg_1d_abs")
    vix = p.get("VIX", {}).get("price")

    if p.get("BRENT", {}).get("verify"):
        out.append(f"⚠️ Brent {brent:+.1f}% looks extreme: confirm on another source before acting.")
    elif brent is not None and brent >= 2:
        out.append(f"🔴 Crude +{brent:.1f}%: negative for India (import bill, INR). Watch OMCs, paints, aviation, tyres. Positive ONGC / Oil India.")
    elif brent is not None and brent <= -2:
        out.append(f"🟢 Crude {brent:.1f}%: positive for India. OMCs, paints, aviation benefit; ONGC / Oil India weaker.")
    if dxy is not None and dxy >= 0.5:
        out.append(f"🔴 Dollar index +{dxy:.1f}%: FII outflow risk from emerging markets.")
    if inr is not None and inr >= 0.3:
        out.append(f"🟠 Rupee weaker (USD/INR +{inr:.2f}%): helps IT & pharma exporters, hurts importers.")
    elif inr is not None and inr <= -0.3:
        out.append(f"🟢 Rupee stronger (USD/INR {inr:.2f}%): supportive for FII flows.")
    if us10y is not None and us10y * 100 >= 8:
        out.append(f"🔴 US 10Y yield +{us10y * 100:.0f}bp: pressure on EM equities and rate-sensitive stocks.")
    if nasdaq is not None and abs(nasdaq) >= 1.5:
        out.append(f"{'🟢' if nasdaq > 0 else '🔴'} Nasdaq {nasdaq:+.1f}%: likely read-through for Indian IT.")
    if spx is not None and spx <= -1.5:
        out.append(f"🔴 S&P 500 {spx:.1f}%: weak global cue for the open.")
    if vix is not None and vix >= 25:
        out.append(f"🔴 US VIX {vix:.1f}: global risk-off; reduce position size.")
    if gold is not None and abs(gold) >= 1.5:
        out.append(f"{'🟡' if gold > 0 else '⚪'} Gold {gold:+.1f}%: affects gold financiers (Muthoot, Manappuram) and jewellers (Titan, Kalyan).")
    if not out:
        out.append("⚪ No major global shock overnight.")
    return out


def format_telegram(p: dict) -> str:
    lines = [f"<b>🌍 Global Markets · {p['generated_at_ist']} IST</b>"]
    group = None
    for r in p["prices"]:
        if r["group"] != group:
            group = r["group"]
            lines.append(f"\n<b>{group}</b>")
        if "price" not in r:
            lines.append(f"{r['name']}: n/a")
            continue
        if r["kind"] == "bp":
            chg = f"{r['chg_1d_abs'] * 100:+.0f}bp"
        else:
            chg = f"{r['chg_1d_pct']:+.2f}%"
        dot = "🟢" if r["chg_1d_abs"] > 0 else ("🔴" if r["chg_1d_abs"] < 0 else "⚪")
        stale = " (stale)" if r.get("stale") else (" ⚠️verify" if r.get("verify") else "")
        lines.append(f"{dot} {r['name']}: {r['price']:,.2f} {chg}{stale}")
    if p["alerts"]:
        lines.append("\n<b>⚠️ Alerts</b>")
        lines += [f"• {a}" for a in p["alerts"]]
    lines.append("\n<b>🇮🇳 What it means for India</b>")
    lines += [f"• {x}" for x in p["india_read"]]
    return "\n".join(lines)
