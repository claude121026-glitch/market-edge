"""Step 4: corporate announcements, bulk/block deals, FII/DII flows (all NSE, free).

Sources (verified):
  announcements  https://www.nseindia.com/api/corporate-announcements?index=equities&from_date=DD-MM-YYYY&to_date=...
  bulk deals     https://nsearchives.nseindia.com/content/equities/bulk.csv   (latest session only)
  block deals    https://nsearchives.nseindia.com/content/equities/block.csv  (latest session only)
  FII/DII cash   https://www.nseindia.com/api/fiidiiTradeReact               (latest session only)
Deals and FII/DII only cover the latest session, so history builds up as you run this daily.
"""
from __future__ import annotations

import html
import io
import json
import logging
import re
import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from . import config, net, nse

log = logging.getLogger("market_edge.corporate")
IST = ZoneInfo("Asia/Kolkata")

ANN_URL = "https://www.nseindia.com/api/corporate-announcements?index=equities&from_date={d:%d-%m-%Y}&to_date={d:%d-%m-%Y}"
BULK_URL = "https://nsearchives.nseindia.com/content/equities/bulk.csv"
BLOCK_URL = "https://nsearchives.nseindia.com/content/equities/block.csv"
FIIDII_URL = "https://www.nseindia.com/api/fiidiiTradeReact"

SCHEMA = """
CREATE TABLE IF NOT EXISTS announcements (
    seq_id TEXT PRIMARY KEY, symbol TEXT, name TEXT, industry TEXT, dt TEXT, type TEXT, text TEXT, url TEXT,
    label TEXT, bias INTEGER, importance INTEGER, amount_cr REAL, liquid INTEGER, alerted INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_ann_dt ON announcements(dt);
CREATE TABLE IF NOT EXISTS deals (
    date TEXT, kind TEXT, symbol TEXT, name TEXT, client TEXT, side TEXT, qty INTEGER, price REAL,
    PRIMARY KEY (date, kind, symbol, client, side, qty, price)
);
CREATE TABLE IF NOT EXISTS fii_dii (
    date TEXT, category TEXT, buy_cr REAL, sell_cr REAL, net_cr REAL, PRIMARY KEY (date, category)
);
"""


def connect() -> sqlite3.Connection:
    con = nse.connect()
    con.executescript(SCHEMA)
    return con


def liquid_symbols(con: sqlite3.Connection) -> set[str]:
    """Symbols passing the scan's liquidity filter over the last 20 sessions."""
    rows = con.execute(
        "SELECT symbol FROM daily WHERE series='EQ' AND date IN "
        "(SELECT DISTINCT date FROM daily ORDER BY date DESC LIMIT 20) "
        "GROUP BY symbol HAVING AVG(turnover_lacs)/100 >= ? AND MAX(close) >= ?",
        (config.MIN_AVG_TURNOVER_CR, config.MIN_PRICE)).fetchall()
    return {r[0] for r in rows}


# --------------------------------------------------------------------------- #
# Announcements
# --------------------------------------------------------------------------- #
_AMT = re.compile(r"(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d+)?)\s*(crores?|cr\.?|lakhs?|lacs?|million|mn|billion|bn)\b", re.I)


def amount_cr(text: str) -> float | None:
    """Largest rupee amount mentioned, in crore (Rs 1 cr = 10 mn = 100 lakh)."""
    best = None
    for num, unit in _AMT.findall(text or ""):
        v = float(num.replace(",", ""))
        u = unit.lower()
        v = v / 100 if u.startswith("la") else v / 10 if u in ("million", "mn") else v * 100 if u in ("billion", "bn") else v
        best = v if best is None or v > best else best
    return best


TOP_ROLE = r"\b(chief executive|ceo|chief financial|cfo|managing director|\bmd\b|whole[- ]time director|chairman)"


def classify(ann_type: str, text: str) -> tuple[str, int, int] | None:
    """(label, bias -1/0/+1, importance 1-10) or None if not worth showing."""
    t, x = ann_type.lower(), (text or "").lower()
    if re.search(r"\b(fraud|default(ed)? (on|in) (payment|repayment|interest)|forensic audit)\b", x):
        return "Default / fraud disclosure", -1, 9
    if "insolvency" in t:
        return "Insolvency (CIRP)", -1, 9
    if "statutory auditor" in t and "resign" in t:
        return "Auditor resigned", -1, 8
    if "orders/contracts" in t:
        return "Order win", 1, 6
    if "buyback" in t or "buy back" in t or re.search(r"\bbuy ?back\b", x):
        return "Buyback", 1, 7
    if "open offer" in t:
        return "Open offer", 1, 7
    if "delisting" in t or "delisting" in x:
        return "Delisting", 1, 8
    if "credit rating" in t:
        if re.search(r"upgrad", x):
            return "Rating upgrade", 1, 6
        if re.search(r"downgrad|negative watch|watch with negative", x):
            return "Rating downgrade", -1, 7
        return None
    if t in ("resignation", "resignation of director/kmp/smp", "cessation") and re.search(TOP_ROLE, x):
        return "Top management exit", -1, 6
    internal = re.search(r"wholly[- ]owned subsidiar|step[- ]down subsidiar|\(wos\)|\bwos\b|transfer (of )?ownership", x)
    if "amalgamation" in t or "merger" in t or "scheme of arrangement" in t or "demerger" in x:
        return None if internal else ("Merger / demerger", 0, 6)
    if t == "acquisition":
        return None if internal else ("Acquisition", 0, 5)  # internal restructuring is noise
    if "strikes" in t:
        return "Strike / disruption", -1, 6
    if "capacity addition" in t:
        return "Capacity addition", 1, 5
    if "integrated filing- financial" in t or "financial result" in t:
        return "Results filed", 0, 4
    if t in ("bonus", "sub-division / stock split") or re.search(r"\bbonus (issue|shares)\b|sub-?division|stock split", x):
        return "Bonus / split", 1, 6
    if t == "outcome of board meeting":
        if re.search(r"financial results?", x):
            return "Results filed", 0, 4
        if re.search(r"\b(qip|qualified institutions|preferential|rights issue|fund ?raising)\b", x):
            return "Fund raising", 0, 5
        return None
    if t.startswith("press release"):
        if re.search(r"\b(order|contract|bag(s|ged)?|wins?|awarded|letter of award|loa)\b", x):
            return "Order win", 1, 6
        if re.search(r"\bacqui(re|res|red|sition)\b", x):
            return "Acquisition", 0, 5
        return None
    if "action(s) taken" in t and re.search(r"penalt|demand|show cause", x):
        return "Regulatory penalty / demand", -1, 4
    return None


def fetch_announcements(d: date) -> list[dict]:
    return json.loads(net.get(ANN_URL.format(d=d), timeout=60))


def update_announcements(d: date | None = None) -> list[dict]:
    d = d or datetime.now(IST).date()
    con = connect()
    liquid = liquid_symbols(con)
    rows = fetch_announcements(d)
    kept = []
    for a in rows:
        cls = classify(a.get("desc") or "", a.get("attchmntText") or "")
        if not cls:
            con.execute("DELETE FROM announcements WHERE seq_id=?", (str(a.get("seq_id")),))  # rules changed
            continue
        label, bias, imp = cls
        amt = amount_cr(a.get("attchmntText") or "")
        if label == "Order win" and amt and amt >= 1000:
            imp = 7
        rec = (str(a.get("seq_id")), a.get("symbol"), a.get("sm_name"), a.get("smIndustry"),
               datetime.strptime(a["an_dt"], "%d-%b-%Y %H:%M:%S").isoformat(), a.get("desc"),
               (a.get("attchmntText") or "")[:600], a.get("attchmntFile"), label, bias, imp, amt,
               int(a.get("symbol") in liquid))
        con.execute("INSERT INTO announcements (seq_id, symbol, name, industry, dt, type, text, url, "
                    "label, bias, importance, amount_cr, liquid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(seq_id) DO UPDATE SET label=excluded.label, bias=excluded.bias, "
                    "importance=excluded.importance, amount_cr=excluded.amount_cr, liquid=excluded.liquid", rec)
        kept.append(rec)
    con.commit()
    con.close()
    log.info("announcements %s: %d total, %d kept", d, len(rows), len(kept))
    return kept


def announcements_for(day: str, min_importance: int = 5, liquid_only: bool = True) -> list[dict]:
    con = connect()
    con.row_factory = sqlite3.Row
    q = "SELECT * FROM announcements WHERE substr(dt,1,10)=? AND importance>=?" + (" AND liquid=1" if liquid_only else "")
    rows = [dict(r) for r in con.execute(q + " ORDER BY importance DESC, dt DESC", (day, min_importance))]
    con.close()
    # one line per (symbol, label)
    seen, out = set(), []
    for r in rows:
        if (r["symbol"], r["label"]) not in seen:
            seen.add((r["symbol"], r["label"]))
            out.append(r)
    return out


def pending_alerts(min_importance: int = 8) -> list[dict]:
    con = connect()
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM announcements WHERE alerted=0 AND liquid=1 AND importance>=?", (min_importance,))]
    con.executemany("UPDATE announcements SET alerted=1 WHERE seq_id=?", [(r["seq_id"],) for r in rows])
    con.commit()
    con.close()
    return rows


# --------------------------------------------------------------------------- #
# Bulk / block deals
# --------------------------------------------------------------------------- #
def parse_deals(raw: bytes, kind: str) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(raw), dtype=str)
    df.columns = [c.strip() for c in df.columns]
    if df.empty:
        return pd.DataFrame(columns=["date", "kind", "symbol", "name", "client", "side", "qty", "price"])
    out = pd.DataFrame({
        "date": pd.to_datetime(df["Date"].str.strip(), format="%d-%b-%Y").dt.strftime("%Y-%m-%d"),
        "kind": kind,
        "symbol": df["Symbol"].str.strip(),
        "name": df["Security Name"].str.strip(),
        "client": df["Client Name"].str.strip(),
        "side": df["Buy/Sell"].str.strip().str.upper(),
        "qty": pd.to_numeric(df["Quantity Traded"].str.replace(",", ""), errors="coerce"),
        "price": pd.to_numeric(df[[c for c in df.columns if c.startswith("Trade Price")][0]].str.replace(",", ""),
                               errors="coerce"),
    })
    return out.dropna(subset=["qty", "price"])


def update_deals() -> int:
    frames = []
    for kind, url in (("bulk", BULK_URL), ("block", BLOCK_URL)):
        try:
            frames.append(parse_deals(net.get(url), kind))
        except Exception as e:
            log.warning("%s deals: %s", kind, e)
    if not frames:
        return 0
    df = pd.concat(frames)
    con = connect()
    con.executemany("INSERT OR IGNORE INTO deals VALUES (?,?,?,?,?,?,?,?)", df.itertuples(index=False, name=None))
    con.commit()
    con.close()
    return len(df)


INSTITUTION = re.compile(r"\b(?:mutual fund|\bmf\b|fund|insurance|life insurance|\blic\b|pension|asset management|"
                         r"amc|capital|investments?|ventures|holdings|limited partnership|\blp\b|plc|pte|"
                         r"trust|foundation|sovereign|government of)\b", re.I)


def deal_summary(day: str) -> list[dict]:
    """Net position per (symbol, client). Same-day buy+sell (market makers / arbitrage) nets out."""
    con = connect()
    df = pd.read_sql_query("SELECT * FROM deals WHERE date=?", con, params=(day,))
    turn = pd.read_sql_query(
        "SELECT symbol, AVG(turnover_lacs)/100 AS avg_turn_cr FROM daily WHERE series='EQ' AND date IN "
        "(SELECT DISTINCT date FROM daily WHERE date<=? ORDER BY date DESC LIMIT 20) GROUP BY symbol",
        con, params=(day,))
    con.close()
    if df.empty:
        return []
    df["signed"] = df["qty"].where(df["side"] == "BUY", -df["qty"])
    df["value_cr"] = df["qty"] * df["price"] / 1e7
    g = df.groupby(["symbol", "name", "client"]).agg(net_qty=("signed", "sum"), gross_qty=("qty", "sum"),
                                                     gross_cr=("value_cr", "sum"), kinds=("kind", lambda s: "/".join(sorted(set(s)))))
    g = g.reset_index()
    g = g[g["net_qty"].abs() >= 0.5 * g["gross_qty"]]          # drop churn
    g["avg_price"] = g["gross_cr"] * 1e7 / g["gross_qty"]
    g["net_cr"] = g["net_qty"] * g["avg_price"] / 1e7
    g["institutional"] = g["client"].str.contains(INSTITUTION)
    g = g.merge(turn, on="symbol", how="left")
    g["pct_of_avg_turnover"] = g["net_cr"].abs() / g["avg_turn_cr"] * 100
    g = g.sort_values("net_cr", key=lambda s: s.abs(), ascending=False)
    return g.drop(columns=["gross_qty"]).round(2).to_dict("records")


# --------------------------------------------------------------------------- #
# FII / DII
# --------------------------------------------------------------------------- #
def update_fii_dii() -> list[dict]:
    rows = json.loads(net.get(FIIDII_URL))
    con = connect()
    for r in rows:
        d = datetime.strptime(r["date"], "%d-%b-%Y").date().isoformat()
        cat = "FII" if "FII" in r["category"] else "DII"
        con.execute("INSERT OR REPLACE INTO fii_dii VALUES (?,?,?,?,?)",
                    (d, cat, float(r["buyValue"]), float(r["sellValue"]), float(r["netValue"])))
    con.commit()
    con.close()
    return rows


def fii_dii_summary(days: int = 5) -> dict | None:
    con = connect()
    df = pd.read_sql_query("SELECT * FROM fii_dii ORDER BY date DESC", con)
    con.close()
    if df.empty:
        return None
    last = df["date"].max()
    recent_dates = sorted(df["date"].unique())[-days:]
    out = {"date": last, "sessions": len(recent_dates)}
    for cat in ("FII", "DII"):
        s = df[df["category"] == cat].set_index("date")["net_cr"]
        out[cat] = {"net": float(s.get(last, 0.0)), "net_period": float(s.reindex(recent_dates).sum(min_count=1) or 0.0)}
        streak, sign = 0, (s.get(last, 0) > 0)
        for d in sorted(s.index, reverse=True):
            if (s[d] > 0) == sign:
                streak += 1
            else:
                break
        out[cat]["streak"] = streak if sign else -streak
    return out


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def _cr(v: float) -> str:
    return f"₹{v:,.0f} cr" if abs(v) >= 1 else f"₹{v:,.2f} cr"


def format_fii_dii(s: dict | None) -> str:
    if not s:
        return ""
    lines = [f"<b>🏦 FII / DII cash · {s['date']}</b>"]
    for cat in ("FII", "DII"):
        x = s[cat]
        dot = "🟢" if x["net"] > 0 else "🔴"
        k = s["sessions"]
        streak = f" · {abs(x['streak'])}-day {'buying' if x['streak'] > 0 else 'selling'} streak" if abs(x["streak"]) >= 2 else ""
        period = f" (last {k}d: {x['net_period']:+,.0f} cr)" if k > 1 else ""
        lines.append(f"{dot} {cat}: {x['net']:+,.0f} cr{period}{streak}")
    return "\n".join(lines)


def results_filed(day: str) -> list[str]:
    con = connect()
    rows = [r[0] for r in con.execute(
        "SELECT DISTINCT symbol FROM announcements WHERE substr(dt,1,10)=? AND label='Results filed' AND liquid=1 "
        "ORDER BY symbol", (day,))]
    con.close()
    return rows


def format_announcements(anns: list[dict], top: int = 12, results: list[str] | None = None) -> str:
    if not anns and not results:
        return ""
    lines = ["<b>🏢 Key announcements (liquid stocks)</b>"]
    for a in anns[:top]:
        dot = "🟢" if a["bias"] > 0 else ("🔴" if a["bias"] < 0 else "⚪")
        amt = f" · {_cr(a['amount_cr'])}" if a.get("amount_cr") else ""
        lines.append(f"{dot} <b>{a['symbol']}</b>: {a['label']}{amt}")
    if results:
        more = f" +{len(results) - 25} more" if len(results) > 25 else ""
        lines.append(f"📑 Results filed ({len(results)}): {', '.join(results[:25])}{more}")
    return "\n".join(lines)


def format_deals(deals: list[dict], top: int = 8) -> str:
    big = [d for d in deals if abs(d["net_cr"]) >= 5]
    if not big:
        return ""
    lines = ["<b>🐋 Bulk / block deals (net, ≥ ₹5 cr)</b>"]
    for d in big[:top]:
        dot = "🟢 BUY" if d["net_cr"] > 0 else "🔴 SELL"
        inst = " 🏛" if d["institutional"] else ""
        rel = f" · {d['pct_of_avg_turnover']:.0f}% of avg daily value" if pd.notna(d.get("pct_of_avg_turnover")) else ""
        lines.append(f"{dot} <b>{d['symbol']}</b> {_cr(abs(d['net_cr']))} @ {d['avg_price']:,.2f} · "
                     f"{html.escape(d['client'][:40])}{inst}{rel}")
    return "\n".join(lines)


def format_alert(a: dict) -> str:
    amt = f" · {_cr(a['amount_cr'])}" if a.get("amount_cr") else ""
    return (f"<b>🚨 {a['symbol']}: {a['label']}{amt}</b>\n{html.escape(a['name'] or '')}\n"
            f"<i>{html.escape((a['text'] or '')[:300])}</i>\n{a['url'] or ''}")


def run(d: date | None = None) -> dict:
    """Fetch everything for the day (default: today IST)."""
    d = d or datetime.now(IST).date()
    out = {"date": d.isoformat()}
    for name, fn in (("announcements", lambda: len(update_announcements(d))),
                     ("deals", update_deals), ("fii_dii", lambda: len(update_fii_dii()))):
        try:
            out[name] = fn()
        except Exception as e:
            out[name] = f"error: {e}"[:150]
            log.error("%s: %s", name, e)
    return out


def catalysts_by_symbol(day: str) -> dict[str, list[str]]:
    """For tagging evening-scan picks: symbol -> ['Order win', 'Bulk BUY ₹12 cr', ...]."""
    tags: dict[str, list[str]] = {}
    for a in announcements_for(day, min_importance=4, liquid_only=False):  # incl. results filed
        tags.setdefault(a["symbol"], []).append(a["label"])
    for dl in deal_summary(day):
        if abs(dl["net_cr"]) >= 5:
            tags.setdefault(dl["symbol"], []).append(f"{'Bulk BUY' if dl['net_cr'] > 0 else 'Bulk SELL'} {_cr(abs(dl['net_cr']))}")
    return tags
