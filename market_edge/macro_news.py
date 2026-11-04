"""Macro news tracker: RSS feeds -> new items -> impact scoring -> sector map -> macro score.

Scoring uses an LLM if a key is configured (one batched call per run, only NEW items),
otherwise a keyword rule engine. Each scored item gets:
  category, importance 1-10, impact on Nifty / Bank Nifty / INR (-2..+2),
  sectors helped / hurt, one-line summary.
"""
from __future__ import annotations

import email.utils
import hashlib
import html
import json
import logging
import re
import sqlite3
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config, net, nse

log = logging.getLogger("market_edge.news")
IST = ZoneInfo("Asia/Kolkata")
ATOM = "{http://www.w3.org/2005/Atom}"

CATEGORIES = ["RBI & Monetary", "India Data", "Govt Policy & Budget", "Fed & US Macro",
              "Global Markets", "Crude & Energy", "Metals", "Currency", "Geopolitics",
              "Trade & Tariffs", "Corporate & Sector", "Other"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    id TEXT PRIMARY KEY, source TEXT, region TEXT, title TEXT, url TEXT, summary TEXT,
    published_utc TEXT, first_seen_utc TEXT,
    status TEXT,                 -- scored | irrelevant | skipped | pending
    scored_by TEXT, category TEXT, importance INTEGER,
    nifty INTEGER, banknifty INTEGER, inr INTEGER,
    sectors_pos TEXT, sectors_neg TEXT, stocks TEXT, takeaway TEXT, alerted INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_news_pub ON news(published_utc);
"""


@dataclass
class Item:
    id: str
    source: str
    region: str
    title: str
    url: str
    summary: str
    published: datetime | None


# --------------------------------------------------------------------------- #
# Fetch & parse
# --------------------------------------------------------------------------- #
def _clean(text: str | None, limit: int = 300) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=IST)  # Indian feeds omit the zone sometimes


def item_id(title: str) -> str:
    norm = re.sub(r"[^a-z0-9 ]+", "", title.lower())
    return hashlib.sha1(re.sub(r"\s+", " ", norm).strip().encode()).hexdigest()[:16]


def parse_feed(raw: bytes, source: str, region: str) -> list[Item]:
    root = ET.fromstring(raw)
    out = []
    for it in root.iter("item"):
        title = _clean(it.findtext("title"), 250)
        if title:
            out.append(Item(item_id(title), source, region, title, (it.findtext("link") or "").strip(),
                            _clean(it.findtext("description")), _date(it.findtext("pubDate"))))
    for it in root.iter(f"{ATOM}entry"):
        title = _clean(it.findtext(f"{ATOM}title"), 250)
        link = it.find(f"{ATOM}link")
        if title:
            out.append(Item(item_id(title), source, region, title, link.get("href", "") if link is not None else "",
                            _clean(it.findtext(f"{ATOM}summary")),
                            _date(it.findtext(f"{ATOM}published") or it.findtext(f"{ATOM}updated"))))
    return out


def fetch_all() -> tuple[list[Item], dict]:
    items, health = [], {}
    for name, region, url in config.NEWS_FEEDS:
        try:
            got = parse_feed(net.get(url, timeout=20, retries=1, backoff=(5,)), name, region)
            items += got
            health[name] = {"ok": True, "items": len(got)}
        except Exception as e:
            health[name] = {"ok": False, "error": str(e)[:120]}
            log.warning("feed %s: %s", name, e)
    return items, health


# --------------------------------------------------------------------------- #
# Scoring: LLM
# --------------------------------------------------------------------------- #
PROMPT = """You are a macro strategist for Indian equities (Nifty 50, Bank Nifty) and the rupee.
Current time: {now} IST.

Below are news items, each with an id. Return ONLY the items that can move Indian markets,
the rupee, or a listed Indian sector within the next 1-3 sessions. Skip local, lifestyle,
politics without market impact, and single small-company news.

For each relevant item return:
- id: the item's id (integer)
- category: one of {categories}
- importance: 1-10. 9-10 = RBI rate decision, India CPI/GDP surprise, Fed decision, war/major shock,
  budget-level policy. 7-8 = big crude/dollar/yield move, major tariff/trade action, US CPI/jobs.
  4-6 = notable data or policy, sector regulation. 1-3 = minor.
- nifty, banknifty, inr: expected impact from -2 (strongly negative) to +2 (strongly positive).
  For inr, positive = rupee strengthens.
- sectors_pos / sectors_neg: names ONLY from this list: {sectors}
- stocks: NSE symbols explicitly affected (may be empty)
- takeaway: one short sentence: what it means for Indian traders

Rules of thumb: crude up hurts India (OMCs, paints, aviation, tyres; helps upstream oil).
Dollar/US yields up -> FII outflows, rupee weaker (helps IT/pharma exporters).
RBI cut/dovish -> banks, NBFCs, realty, autos up. Tariffs on India -> exporters down.

Return JSON: {{"items": [ ... ]}}

ITEMS:
{items}"""


def _llm_call(prompt: str) -> str:
    key = config.LLM_API_KEY
    if config.LLM_PROVIDER == "gemini":
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.LLM_MODEL}:generateContent"
        body = {"contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}
        headers = {"Content-Type": "application/json", "x-goog-api-key": key}
    else:  # OpenAI-compatible: OpenAI, Groq, Ollama (/v1), OpenRouter, ...
        url = config.LLM_BASE_URL.rstrip("/") + "/chat/completions"
        body = {"model": config.LLM_MODEL, "temperature": 0, "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": prompt}]}
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key or 'none'}"}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=90) as r:
        data = json.loads(r.read())
    if config.LLM_PROVIDER == "gemini":
        return data["candidates"][0]["content"]["parts"][0]["text"]
    return data["choices"][0]["message"]["content"]


def _clamp(v, lo, hi, default=0) -> int:
    try:
        return max(lo, min(hi, int(round(float(v)))))
    except (TypeError, ValueError):
        return default


def _sector_list(v) -> list[str]:
    allowed = {s.lower(): s for s in config.SECTORS}
    out = []
    for s in v or []:
        m = allowed.get(str(s).lower()) or next((a for k, a in allowed.items() if str(s).lower() in k), None)
        if m and m not in out:
            out.append(m)
    return out


def normalize_scores(raw: list[dict], valid_ids: dict[int, str]) -> dict[str, dict]:
    """Validate LLM output; map numeric ids back to item ids (no fuzzy headline matching)."""
    out = {}
    for r in raw:
        idx = _clamp(r.get("id"), -1, 10**6, -1)
        if idx not in valid_ids:
            continue
        cat = r.get("category") if r.get("category") in CATEGORIES else "Other"
        out[valid_ids[idx]] = {
            "category": cat, "importance": _clamp(r.get("importance"), 1, 10, 3),
            "nifty": _clamp(r.get("nifty"), -2, 2), "banknifty": _clamp(r.get("banknifty"), -2, 2),
            "inr": _clamp(r.get("inr"), -2, 2),
            "sectors_pos": _sector_list(r.get("sectors_pos")), "sectors_neg": _sector_list(r.get("sectors_neg")),
            "stocks": [str(s).upper()[:20] for s in (r.get("stocks") or [])][:8],
            "takeaway": _clean(str(r.get("takeaway") or ""), 200),
        }
    return out


def score_llm(items: list[Item]) -> dict[str, dict]:
    lines, ids = [], {}
    for n, it in enumerate(items, 1):
        ids[n] = it.id
        lines.append(f"[{n}] ({it.source}) {it.title} :: {it.summary[:160]}")
    prompt = PROMPT.format(now=datetime.now(IST).strftime("%Y-%m-%d %H:%M"), categories=CATEGORIES,
                           sectors=config.SECTORS, items="\n".join(lines))
    text = _llm_call(prompt)
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M)
    data = json.loads(text)
    raw = data.get("items", data) if isinstance(data, dict) else data
    return normalize_scores(raw if isinstance(raw, list) else [], ids)


# --------------------------------------------------------------------------- #
# Scoring: keyword rules (no key needed)
# --------------------------------------------------------------------------- #
UP = r"(rise[sn]?|rising|jump(s|ed)?|surge[sd]?|soar(s|ed)?|spike[sd]?|climb(s|ed)?|higher|gain(s|ed)?|rall(y|ies|ied)|hits? (record|high))"
DOWN = r"(fall(s|en)?|falling|fell|drop(s|ped)?|slump(s|ed)?|plunge[sd]?|tumble[sd]?|slide[sd]?|slid|lower|decline[sd]?|ease[sd]?|cool(s|ed)?|crash(es|ed)?|sink(s)?|sank)"


def near(subject: str, direction: str, gap: int = 2) -> str:
    """subject followed by direction within `gap` words (keeps rules from matching far-apart words)."""
    return rf"\b{subject}\b\W+(?:[\w$%.,'-]+\W+){{0,{gap}}}?{direction}\b"


# Matched against the HEADLINE only; first match wins, so order = priority.
# (pattern, category, importance, nifty, banknifty, inr, sectors_pos, sectors_neg, takeaway)
RULES = [
    (r"\b(rbi|reserve bank)\b.*\b(cuts?|lowers?|reduces?)\b.*\b(repo|rates?)\b",
     "RBI & Monetary", 9, 1, 2, -1, ["Banks", "NBFC & Financials", "Realty", "Auto"], [], "RBI easing: positive for rate-sensitives."),
    (r"\b(rbi|reserve bank)\b.*\b(hikes?|raises?)\b.*\b(repo|rates?)\b",
     "RBI & Monetary", 9, -1, -2, 1, [], ["Banks", "NBFC & Financials", "Realty", "Auto"], "RBI tightening: negative for rate-sensitives."),
    (r"\b(rbi|reserve bank|mpc)\b", "RBI & Monetary", 4, 0, 0, 0, [], [], "RBI action: check liquidity / rate signal."),
    (r"\b(retail inflation|cpi inflation|wpi)\b", "India Data", 7, 0, 0, 0, [], [], "Inflation print: shapes RBI rate path."),
    (r"\b(gdp|iip|industrial output|gst collections?|pmi)\b", "India Data", 5, 0, 0, 0, [], [], "Economic data release."),
    (r"\b(fed|fomc|powell)\b.*\b(cuts?|dovish)\b", "Fed & US Macro", 8, 1, 1, 1, ["IT", "Metals", "Realty"], [], "Fed easing: supportive for EM flows."),
    (r"\b(fed|fomc|powell)\b.*\b(hikes?|hawkish|higher for longer)\b", "Fed & US Macro", 8, -1, -1, -1, [], ["Realty", "Metals"], "Hawkish Fed: FII outflow risk."),
    (r"\b(us|u\.s\.)\b.*\b(cpi|nonfarm|payrolls|jobs report)\b", "Fed & US Macro", 7, 0, 0, 0, [], [], "US data: moves dollar and yields."),
    (r"\binventor(y|ies)\b", "Crude & Energy", 3, 0, 0, 0, [], [], "Inventory data: minor for India."),
    (near("(crude|oil|brent|wti)", DOWN), "Crude & Energy", 6, 1, 1, 1,
     ["OMCs", "Paints", "Aviation", "Chemicals"], ["Oil & Gas Upstream"], "Crude down: positive for India."),
    (near("(crude|oil|brent|wti)", UP), "Crude & Energy", 6, -1, -1, -1,
     ["Oil & Gas Upstream"], ["OMCs", "Paints", "Aviation", "Chemicals"], "Crude up: negative for India's import bill."),
    (r"\bopec\b", "Crude & Energy", 5, 0, 0, 0, [], [], "OPEC news: watch crude."),
    (near("rupee", r"(record low|weakens?|depreciates?|" + DOWN[1:]), "Currency", 6, -1, -1, -2,
     ["IT", "Pharma", "Textiles & Exporters"], ["OMCs", "Aviation"], "Weak rupee: exporters gain, importers hurt."),
    (near("rupee", r"(strengthens?|appreciates?|" + UP[1:]), "Currency", 5, 1, 1, 2, ["OMCs"], ["IT"], "Stronger rupee: FII-supportive."),
    (near("gold", UP), "Metals", 4, 0, 0, 0, ["Gold Financiers & Jewellers"], [], "Gold up: gold financiers benefit."),
    (near("(dollar|dxy)", DOWN), "Currency", 5, 1, 1, 1, [], ["IT"], "Weaker dollar: supportive for EM flows."),
    (near("(dollar|dxy)", UP), "Currency", 5, -1, -1, -1, ["IT"], [], "Dollar strength: EM outflow risk."),
    (r"\btariffs?\b", "Trade & Tariffs", 6, 0, 0, 0, [], ["Textiles & Exporters"], "Tariff news: check India exposure."),
    (r"\b(missile|airstrike|invasion|military strike|declares war|ceasefire collapse)\b", "Geopolitics", 7, -1, -1, -1,
     ["Defence", "Gold Financiers & Jewellers", "Oil & Gas Upstream"], ["Aviation", "OMCs"], "Geopolitical risk: risk-off."),
    (r"\b(budget|gst council|pli scheme|disinvestment|fiscal deficit|ltcg|stt)\b", "Govt Policy & Budget", 6, 0, 0, 0, [], [], "Policy news: check sector impact."),
    (r"\b(fii|fpi)s?\b.*\b(sell|selling|outflows?|pull out)\b", "Global Markets", 6, -1, -1, -1, [], [], "Foreign selling pressure."),
    (r"\b(fii|fpi)s?\b.*\b(buy|buying|inflows?)\b", "Global Markets", 6, 1, 1, 1, [], [], "Foreign buying support."),
]


def score_rules(items: list[Item]) -> dict[str, dict]:
    out = {}
    for it in items:
        text = it.title.lower()
        for pat, cat, imp, n, bn, inr, pos, neg, take in RULES:
            if re.search(pat, text):
                out[it.id] = {"category": cat, "importance": imp, "nifty": n, "banknifty": bn, "inr": inr,
                              "sectors_pos": pos, "sectors_neg": neg, "stocks": [], "takeaway": take}
                break
    return out


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #
def connect() -> sqlite3.Connection:
    con = nse.connect()
    con.executescript(SCHEMA)
    con.row_factory = sqlite3.Row
    return con


def run() -> dict:
    con = connect()
    now = datetime.now(timezone.utc)
    items, health = fetch_all()
    first_run = con.execute("SELECT COUNT(*) FROM news").fetchone()[0] == 0
    max_age = timedelta(hours=config.NEWS_FIRST_RUN_HOURS if first_run else config.NEWS_MAX_AGE_HOURS)

    known = {r[0] for r in con.execute("SELECT id FROM news")}
    pending_ids = {r[0] for r in con.execute("SELECT id FROM news WHERE status='pending'")}
    new, seen_now = [], set()
    for it in items:
        if it.id in seen_now or (it.id in known and it.id not in pending_ids):
            continue
        seen_now.add(it.id)
        pub = it.published or now
        too_old = now - pub > max_age
        if it.id not in known:
            con.execute("INSERT INTO news (id, source, region, title, url, summary, published_utc, first_seen_utc, status)"
                        " VALUES (?,?,?,?,?,?,?,?,?)",
                        (it.id, it.source, it.region, it.title, it.url, it.summary,
                         pub.astimezone(timezone.utc).isoformat(timespec="seconds"),
                         now.isoformat(timespec="seconds"), "skipped" if too_old else "pending"))
        if not too_old:
            new.append(it)
    con.commit()

    new.sort(key=lambda i: i.published or now, reverse=True)
    batch, overflow = new[:config.NEWS_MAX_PER_LLM_CALL], new[config.NEWS_MAX_PER_LLM_CALL:]
    con.executemany("UPDATE news SET status='skipped' WHERE id=?", [(i.id,) for i in overflow])

    scored_by, llm_error, scores = None, None, {}
    if batch:
        if config.LLM_API_KEY or config.LLM_BASE_URL.startswith("http://localhost"):
            try:
                scores, scored_by = score_llm(batch), f"llm:{config.LLM_MODEL}"
            except Exception as e:
                llm_error = f"{type(e).__name__}: {e}"[:200]
                log.error("LLM scoring failed, using rules: %s", llm_error)
        if scored_by is None:
            scores, scored_by = score_rules(batch), "rules"
        for it in batch:
            s = scores.get(it.id)
            if s:
                con.execute("UPDATE news SET status='scored', scored_by=?, category=?, importance=?, nifty=?, "
                            "banknifty=?, inr=?, sectors_pos=?, sectors_neg=?, stocks=?, takeaway=? WHERE id=?",
                            (scored_by, s["category"], s["importance"], s["nifty"], s["banknifty"], s["inr"],
                             json.dumps(s["sectors_pos"]), json.dumps(s["sectors_neg"]), json.dumps(s["stocks"]),
                             s["takeaway"], it.id))
            else:
                con.execute("UPDATE news SET status='irrelevant', scored_by=? WHERE id=?", (scored_by, it.id))
    con.commit()

    to_alert = [dict(r) for r in con.execute(
        "SELECT * FROM news WHERE status='scored' AND alerted=0 AND importance>=? ORDER BY importance DESC",
        (config.NEWS_ALERT_IMPORTANCE,))]
    con.executemany("UPDATE news SET alerted=1 WHERE id=?", [(r["id"],) for r in to_alert])
    con.commit()
    summary = {"fetched": len(items), "new": len(new), "scored": len(scores), "scored_by": scored_by,
               "llm_error": llm_error, "feeds": health, "alerts": to_alert}
    con.close()
    return summary


# --------------------------------------------------------------------------- #
# Aggregation: macro score & sector view
# --------------------------------------------------------------------------- #
def recent(hours: int = 18, min_importance: int = 1) -> list[dict]:
    con = connect()
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM news WHERE status='scored' AND published_utc>=? AND importance>=? "
        "ORDER BY importance DESC, published_utc DESC", (since, min_importance))]
    con.close()
    for r in rows:
        for k in ("sectors_pos", "sectors_neg", "stocks"):
            r[k] = json.loads(r[k] or "[]")
    return rows


def story_weights(news: list[dict]) -> list[float]:
    """Several outlets reporting the same theme shouldn't count several times: the strongest
    item per (category, direction) counts fully, repeats count a quarter."""
    seen, out = set(), []
    for r in sorted(range(len(news)), key=lambda i: -news[i]["importance"]):
        key = (news[r]["category"], (news[r]["nifty"] > 0) - (news[r]["nifty"] < 0))
        out.append((r, 1.0 if key not in seen else 0.25))
        seen.add(key)
    return [w for _, w in sorted(out)]


def macro_score(news: list[dict], prices: dict | None = None, flows: dict | None = None) -> dict:
    """0-10 risk appetite for Indian equities (5 = neutral). News + overnight global prices."""
    news_pts = sum(r["nifty"] * r["importance"] / 10 * w for r, w in zip(news, story_weights(news)))
    news_pts = max(-3.0, min(3.0, news_pts / 2))

    px_pts, notes = 0.0, []
    p = {r["key"]: r for r in (prices or {}).get("prices", []) if "price" in r and not r.get("stale")}

    def chg(k):
        return p.get(k, {}).get("chg_1d_pct")

    for k, w, label in (("SPX", 0.6, "S&P"), ("NASDAQ", 0.3, "Nasdaq"), ("BRENT", -0.25, "Brent"),
                        ("DXY", -1.0, "DXY")):
        c = chg(k)
        if c is not None and not p[k].get("verify"):
            px_pts += w * c
    if "US10Y" in p:
        px_pts -= p["US10Y"]["chg_1d_abs"] * 100 / 10 * 0.5   # -0.5 per +10bp
    if "VIX" in p and p["VIX"]["price"] >= 25:
        px_pts -= 1.5
        notes.append("US VIX elevated")
    px_pts = max(-2.0, min(2.0, px_pts))

    # Previous session's FII cash flow: +/-0.5 at +/-Rs 5,000 cr.
    flow_pts = 0.0
    if flows and "FII" in flows:
        flow_pts = max(-0.5, min(0.5, flows["FII"]["net"] / 10000))
    px_pts += flow_pts

    score = round(max(0.0, min(10.0, 5 + news_pts + px_pts)), 1)
    label = ("Risk-off" if score <= 3.5 else "Cautious" if score <= 4.5 else "Neutral" if score < 5.5
             else "Positive" if score < 6.5 else "Risk-on")
    return {"score": score, "label": label, "news_component": round(news_pts, 2),
            "prices_component": round(px_pts - flow_pts, 2), "flows_component": round(flow_pts, 2),
            "notes": notes}


def sector_view(news: list[dict]) -> list[tuple[str, float]]:
    net_ = {}
    for r, k in zip(news, story_weights(news)):
        w = r["importance"] / 10 * k
        for s in r["sectors_pos"]:
            net_[s] = net_.get(s, 0) + w
        for s in r["sectors_neg"]:
            net_[s] = net_.get(s, 0) - w
    return sorted(((s, round(v, 1)) for s, v in net_.items() if abs(v) >= 0.4), key=lambda x: -x[1])


def format_alert(r: dict) -> str:
    arrow = lambda v: "🟢" if v > 0 else ("🔴" if v < 0 else "⚪")  # noqa: E731
    pos = ", ".join(json.loads(r["sectors_pos"]) if isinstance(r["sectors_pos"], str) else r["sectors_pos"])
    neg = ", ".join(json.loads(r["sectors_neg"]) if isinstance(r["sectors_neg"], str) else r["sectors_neg"])
    lines = [f"<b>🚨 [{r['importance']}/10] {html.escape(r['title'])}</b>",
             f"{r['category']} · {r['source']}",
             f"Nifty {arrow(r['nifty'])} · Bank Nifty {arrow(r['banknifty'])} · INR {arrow(r['inr'])}"]
    if pos:
        lines.append(f"▲ {pos}")
    if neg:
        lines.append(f"▼ {neg}")
    if r.get("takeaway"):
        lines.append(f"<i>{html.escape(r['takeaway'])}</i>")
    if r.get("url"):
        lines.append(r["url"])
    return "\n".join(lines)


def format_brief(news: list[dict], score: dict, top: int = 6) -> str:
    lines = [f"<b>🧭 Macro mood for India: {score['label']} ({score['score']}/10)</b>",
             f"<i>news {score['news_component']:+.1f} · global prices {score['prices_component']:+.1f}"
             f" · FII flows {score.get('flows_component', 0):+.1f}</i>"]
    if news:
        lines.append("\n<b>📰 Top macro news</b>")
        for r in news[:top]:
            d = "🟢" if r["nifty"] > 0 else ("🔴" if r["nifty"] < 0 else "⚪")
            lines.append(f"{d} [{r['importance']}] {html.escape(r['title'])} <i>({r['source']})</i>")
    sv = sector_view(news)
    if sv:
        lines.append("\n<b>🏭 Sector tilt</b>")
        pos = [f"{s} (+{v})" for s, v in sv if v > 0]
        neg = [f"{s} ({v})" for s, v in sv if v < 0]
        if pos:
            lines.append("▲ " + ", ".join(pos[:6]))
        if neg:
            lines.append("▼ " + ", ".join(neg[:6]))
    return "\n".join(lines)
