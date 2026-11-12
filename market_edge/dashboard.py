"""Daily Market Edge dashboard.   Run from the folder that contains market_edge/:

    streamlit run market_edge/dashboard.py

Reads the local database and JSON files; buttons in the sidebar refresh data on demand.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---- Cloud mode (Streamlit Community Cloud): read-only copy of the data published by GitHub Actions ----
import os, shutil, tarfile, tempfile, time, urllib.request  # noqa: E401,E402


def _secret(name: str):
    if os.getenv(name):
        return os.getenv(name)
    try:
        return st.secrets.get(name)
    except Exception:
        return None


DATA_URL = _secret("ME_DATA_URL")
CLOUD_ROOT = Path(tempfile.gettempdir()) / "market_edge_cloud"
if DATA_URL:
    os.environ["ME_DATA_DIR"] = str(CLOUD_ROOT / "data")   # must be set before market_edge.config loads

from market_edge import (config, corporate, events, global_prices, intraday, macro_news,  # noqa: E402
                         nse, paper, watchlist)


@st.cache_data(ttl=600, show_spinner="Loading the latest data from GitHub…")
def refresh_cloud_data(url: str) -> str:
    """Download data.tar.gz, unpack next to the live copy, then swap it in."""
    CLOUD_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = CLOUD_ROOT / "download.tar.gz"
    for attempt in range(3):            # the asset is briefly missing while a job re-uploads it
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "market-edge-dashboard"})
            with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f)
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(10)
    new = CLOUD_ROOT / "new"
    shutil.rmtree(new, ignore_errors=True)
    with tarfile.open(tmp) as tar:
        try:
            tar.extractall(new, filter="data")      # refuses unsafe paths (Python 3.12+)
        except TypeError:
            tar.extractall(new)
    live, old = CLOUD_ROOT / "data", CLOUD_ROOT / f"old-{time.time_ns()}"
    if live.exists():
        live.rename(old)
    (new / "data").rename(live)
    shutil.rmtree(old, ignore_errors=True)
    shutil.rmtree(new, ignore_errors=True)
    return datetime.now().strftime("%H:%M")


if DATA_URL:
    try:
        LAST_SYNC = refresh_cloud_data(DATA_URL)
    except Exception as e:
        st.error(f"Couldn't download the data from GitHub ({e}). Check ME_DATA_URL in the app's secrets, "
                 "and that the bootstrap workflow has run.")
        st.stop()

# Validated reference palette (dataviz skill): categorical order, diverging poles, status.
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
POS, NEG, MID = "#2a78d6", "#e34948", "#9a9993"          # diverging: gain / loss / neutral
GOOD, CRITICAL = "#0ca30c", "#d03b3b"                    # status: target / stop (always labelled)
MUTED = "#8a8984"

st.set_page_config(page_title="Daily Market Edge", page_icon="📈", layout="wide")


# --------------------------------------------------------------------------- #
# Data access (cached briefly; buttons clear the cache)
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=60)
def q(sql: str, params: tuple = ()) -> pd.DataFrame:
    con = intraday.connect()        # ensures every table/column exists
    try:
        return pd.read_sql_query(sql, con, params=params)
    finally:
        con.close()


@st.cache_data(ttl=60)
def read_json(path: str) -> dict | None:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def latest_report() -> dict | None:
    files = sorted(config.REPORT_DIR.glob("evening_*.json"))
    return read_json(str(files[-1])) if files else None


def chart(c: alt.Chart, height: int = 260):
    st.altair_chart(c.properties(height=height).configure_view(strokeWidth=0)
                    .configure_axis(gridColor="#e8e7e3", domainColor=MUTED, tickColor=MUTED,
                                    labelColor="#52514e", titleColor="#52514e"),
                    use_container_width=True)


def diverging_bars(df: pd.DataFrame, cat: str, val: str, title: str, fmt: str = "+.2f"):
    """Horizontal diverging bars, sorted high->low (gain = blue, loss = red); value labels sit just
    outside each bar end (right of positive bars, left of negative ones)."""
    d = df.sort_values(val, ascending=False).reset_index(drop=True)
    d["sign"] = d[val].apply(lambda v: "positive" if v > 0 else "negative" if v < 0 else "zero")
    order = d[cat].tolist()
    span = max(abs(d[val].min()), abs(d[val].max())) or 1
    base = alt.Chart(d).encode(y=alt.Y(f"{cat}:N", sort=order, title=None,
                                       axis=alt.Axis(labelLimit=220, ticks=False, domain=False)))
    bars = base.mark_bar(cornerRadiusEnd=4, height={"band": 0.7}).encode(
        x=alt.X(f"{val}:Q", title=title, scale=alt.Scale(domain=[-span * 1.25, span * 1.25])),
        color=alt.Color("sign:N", scale=alt.Scale(domain=["positive", "negative", "zero"], range=[POS, NEG, MID]),
                        legend=None),
        tooltip=[alt.Tooltip(f"{cat}:N"), alt.Tooltip(f"{val}:Q", format=fmt)])
    pos = base.transform_filter(f"datum['{val}'] >= 0").mark_text(align="left", dx=4, color="#52514e").encode(
        x=f"{val}:Q", text=alt.Text(f"{val}:Q", format=fmt))
    neg = base.transform_filter(f"datum['{val}'] < 0").mark_text(align="right", dx=-4, color="#52514e").encode(
        x=f"{val}:Q", text=alt.Text(f"{val}:Q", format=fmt))
    zero = alt.Chart(pd.DataFrame({"x": [0]})).mark_rule(color=MUTED).encode(x="x:Q")
    chart(bars + pos + neg + zero, height=max(140, 34 * len(d)))


# --------------------------------------------------------------------------- #
# Sidebar: refresh actions
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("📈 Daily Market Edge")
    st.caption("Personal screener · paper-trade only")
    if DATA_URL:
        st.caption(f"☁️ Cloud view · data from GitHub, refreshed every 10 min (last {LAST_SYNC})")
        if st.button("🔄 Reload data now", use_container_width=True):
            refresh_cloud_data.clear()
            st.cache_data.clear()
            st.rerun()
    else:
        if st.button("🌍 Refresh global prices", use_container_width=True):
            with st.spinner("Fetching prices (≈1 req/s)…"):
                global_prices.run()
            st.cache_data.clear()
        if st.button("📰 Fetch macro news", use_container_width=True):
            with st.spinner("Reading feeds…"):
                r = macro_news.run()
            st.success(f"{r['new']} new · {r['scored']} relevant ({r['scored_by']})")
            st.cache_data.clear()
        if st.button("🏢 Update announcements / deals / FII", use_container_width=True):
            with st.spinner("Fetching NSE corporate data…"):
                corporate.run()
            st.cache_data.clear()
        if st.button("📊 Run evening scan", use_container_width=True):
            with st.spinner("Updating NSE data and scanning…"):
                nse.update(days_back=7)
                from market_edge import evening_scan
                rep = evening_scan.run()
                evening_scan.attach_catalysts(rep, corporate.catalysts_by_symbol(rep["scan_date"]))
                (config.REPORT_DIR / f"evening_{rep['scan_date']}.json").write_text(json.dumps(rep, indent=2, default=str))
                paper.evaluate_pending()
                paper.record_from_report(rep)
            st.cache_data.clear()
    st.divider()
    last = q("SELECT MAX(date) AS d FROM daily")["d"].iloc[0]
    st.caption(f"NSE data up to **{last}**")

tabs = st.tabs(["🌅 Today", "🎯 Watchlist & Live", "📊 Scanner", "📰 News & Events", "🏢 Corporate",
                "📒 Performance", "🔎 Stock"])

# --------------------------------------------------------------------------- #
# Today
# --------------------------------------------------------------------------- #
with tabs[0]:
    prices = read_json(str(config.GLOBAL_JSON)) or {}
    news = macro_news.recent(hours=18, min_importance=4)
    flows = corporate.fii_dii_summary()
    mood = macro_news.macro_score(news, prices, flows)

    c1, c2, c3 = st.columns([1.2, 1, 1])
    c1.metric("Macro mood for India", f"{mood['score']}/10", mood["label"], delta_color="off")
    c1.caption(f"news {mood['news_component']:+.1f} · global prices {mood['prices_component']:+.1f} · "
               f"FII flows {mood.get('flows_component', 0):+.1f}")
    if flows:
        k = flows["sessions"]
        for col, cat in ((c2, "FII"), (c3, "DII")):
            period = f"{flows[cat]['net_period']:+,.0f} over {k} sessions" if k > 1 else None
            col.metric(f"{cat} cash (₹ cr)", f"{flows[cat]['net']:+,.0f}", period)
        c2.caption(f"Session {flows['date']}")

    st.subheader("What it means for India")
    for line in prices.get("india_read", ["Refresh global prices to see overnight cues."]):
        st.write(line)
    if prices.get("alerts"):
        st.warning("**Alerts:** " + " · ".join(prices["alerts"]))

    st.subheader("Global markets")
    if prices.get("prices"):
        st.caption(f"As of {prices['generated_at_ist']} IST · ⚠️ = verify (large futures move) · (stale) = last good value")
        rows = [r for r in prices["prices"] if "price" in r]
        for group in dict.fromkeys(r["group"] for r in rows):
            g = [r for r in rows if r["group"] == group]
            st.markdown(f"**{group}**")
            cols = st.columns(len(g))
            for col, r in zip(cols, g):
                delta = f"{r['chg_1d_abs'] * 100:+.0f} bp" if r["kind"] == "bp" else f"{r['chg_1d_pct']:+.2f}%"
                flag = " ⚠️" if r.get("verify") else " (stale)" if r.get("stale") else ""
                col.metric(f"{r['name']}{flag}", f"{r['price']:,.2f}", delta,
                           delta_color="inverse" if r["key"] in ("VIX", "INDIAVIX", "US10Y", "BRENT", "WTI", "DXY", "USDINR") else "normal")
        if prices.get("missing"):
            st.caption("Not available this run: " + ", ".join(prices["missing"]))
    else:
        st.info("No global prices yet: use **Refresh global prices** in the sidebar.")

    st.subheader("Coming up (7 days)")
    evs = events.upcoming(days=7)
    if evs:
        st.dataframe(pd.DataFrame([{"When": e["when"], "Time (IST)": e["time_ist"] or "",
                                    "Event": e["name"] + (" (~approx)" if e["approx"] else ""),
                                    "Importance": e["importance"]} for e in evs]),
                     hide_index=True, use_container_width=True)

# --------------------------------------------------------------------------- #
# Watchlist & live
# --------------------------------------------------------------------------- #
with tabs[1]:
    rep = latest_report()
    if not rep:
        st.info("Run the evening scan first.")
    else:
        top = watchlist.build(rep, mood, macro_news.sector_view(news), record=False)
        st.caption(f"From the {rep['scan_date']} close · ranked by signals, catalysts, macro mood, sector news and "
                   "each setup's track record. Backtest: this plan had **no proven edge after costs** — paper-trade only.")
        trades = q("SELECT * FROM paper_trades WHERE source='live' AND signal_date=?", (rep["scan_date"],))
        # Only the TRADE session (after the signal date) counts: never judge picks on the signal day's path.
        bars_today = q("SELECT symbol, date, ts, open, high, low, close FROM intraday_bars WHERE date = "
                       "(SELECT MIN(date) FROM intraday_bars WHERE date > ?)", (rep["scan_date"],))
        rows = []
        for i, p in enumerate(top, 1):
            b = bars_today[bars_today["symbol"] == p["symbol"]]
            live, last = "—", None
            if len(b):
                path = [intraday.Bar(datetime.fromisoformat(r.ts), r.open, r.high, r.low, r.close) for r in b.itertuples()]
                f = intraday.simulate_path(p["direction"], p["trigger"], p["stop"], p["target"], path)
                last = path[-1].close
                live = {"no_entry": "waiting", "close": "open" if f.entry else "waiting", "win": "✅ target",
                        "loss": "🛑 stop"}[f.outcome] if f.entry or f.outcome == "no_entry" else "waiting"
            e = p.get("history") or {}
            rows.append({"#": i, "Side": "🟢 LONG" if p["direction"] == "bullish" else "🔴 SHORT", "Stock": p["symbol"],
                         "Setup": p["setup"], "Trigger": p["trigger"], "Stop": p["stop"], "Target": p["target"],
                         "Risk %": p["risk_pct"], "Qty": p.get("qty"), "Capital ₹": p.get("capital"),
                         "Last": last, "Live": live,
                         "Hist %": e.get("intraday_pct"), "Why": ", ".join(p["reasons"])})
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True,
                     column_config={"Trigger": st.column_config.NumberColumn(format="%.2f"),
                                    "Stop": st.column_config.NumberColumn(format="%.2f"),
                                    "Target": st.column_config.NumberColumn(format="%.2f"),
                                    "Risk %": st.column_config.NumberColumn(format="%.1f"),
                                    "Capital ₹": st.column_config.NumberColumn(format="localized"),
                                    "Hist %": st.column_config.NumberColumn(
                                        format="%+.2f", help="Setup's average next-day move in the predicted direction (backtest)")})

        st.subheader("Intraday path")
        pick = st.selectbox("Stock", [p["symbol"] for p in top])
        p = next(x for x in top if x["symbol"] == pick)
        b = q("SELECT ts, close FROM intraday_bars WHERE symbol=? AND date=(SELECT MIN(date) FROM intraday_bars "
              "WHERE symbol=? AND date>?) ORDER BY ts", (pick, pick, rep["scan_date"]))
        if b.empty:
            st.info(f"No intraday data yet for the session after {rep['scan_date']} — start "
                    "`python -m market_edge.run live` during market hours.")
        else:
            b["time"] = pd.to_datetime(b["ts"].str[:19])
            line = alt.Chart(b).mark_line(color=BLUE, strokeWidth=2).encode(
                x=alt.X("time:T", title=None, axis=alt.Axis(format="%H:%M")),
                y=alt.Y("close:Q", title="Price (₹)", scale=alt.Scale(zero=False)),
                tooltip=[alt.Tooltip("time:T", format="%H:%M"), alt.Tooltip("close:Q", format=",.2f")])
            lv = pd.DataFrame({"level": ["Trigger", "Stop", "Target"], "y": [p["trigger"], p["stop"], p["target"]]})
            rules = alt.Chart(lv).mark_rule(strokeDash=[4, 3], strokeWidth=1.5).encode(
                y="y:Q", color=alt.Color("level:N", scale=alt.Scale(domain=["Trigger", "Stop", "Target"],
                                                                     range=[MUTED, CRITICAL, GOOD]),
                                         legend=alt.Legend(orient="top", title=None)),
                tooltip=["level:N", alt.Tooltip("y:Q", format=",.2f")])
            chart(line + rules, height=300)

# --------------------------------------------------------------------------- #
# Scanner
# --------------------------------------------------------------------------- #
with tabs[2]:
    rep = latest_report()
    if rep:
        b = rep["breadth"]
        c1, c2, c3 = st.columns(3)
        c1.metric("Advances", f"{b['advances']:,}")
        c2.metric("Declines", f"{b['declines']:,}")
        c3.metric("Stocks flagged", rep["flagged"])
        for side, label in (("bullish", "🟢 Bullish"), ("bearish", "🔴 Bearish")):
            st.subheader(label)
            df = pd.DataFrame(rep[side])
            if df.empty:
                continue
            df["signals"] = df["signals"].apply(lambda s: ", ".join(x.replace("_", " ") for x in s))
            df["catalysts"] = df.get("catalysts", pd.Series([[]] * len(df))).apply(lambda c: ", ".join(c or []))
            st.dataframe(df[["symbol", "name", "setup", "score", "close", "chg_pct", "vol_x", "deliv_pct", "signals",
                             "catalysts", "trigger", "stop", "target", "risk_pct"]],
                         hide_index=True, use_container_width=True,
                         column_config={"chg_pct": st.column_config.NumberColumn("Chg %", format="%+.1f"),
                                        "vol_x": st.column_config.NumberColumn("Vol ×", format="%.1f"),
                                        "deliv_pct": st.column_config.NumberColumn("Deliv %", format="%.0f"),
                                        "risk_pct": st.column_config.NumberColumn("Risk %", format="%.1f")})
    else:
        st.info("Run the evening scan first.")

# --------------------------------------------------------------------------- #
# News & events
# --------------------------------------------------------------------------- #
with tabs[3]:
    hours = st.slider("Look back (hours)", 6, 72, 24, step=6)
    nw = macro_news.recent(hours=hours, min_importance=1)
    sv = macro_news.sector_view(nw)
    left, right = st.columns([1.4, 1])
    with left:
        st.subheader("Macro news")
        if nw:
            st.dataframe(pd.DataFrame([{"Imp": r["importance"], "Nifty": r["nifty"], "INR": r["inr"],
                                        "Headline": r["title"], "Category": r["category"], "Source": r["source"],
                                        "Takeaway": r["takeaway"], "Link": r["url"]} for r in nw]),
                         hide_index=True, use_container_width=True,
                         column_config={"Link": st.column_config.LinkColumn(display_text="open")})
        else:
            st.info("No scored news in this window — use **Fetch macro news**.")
    with right:
        st.subheader("Sector tilt from news")
        if sv:
            diverging_bars(pd.DataFrame(sv, columns=["Sector", "Tilt"]), "Sector", "Tilt",
                           "Net news impact (importance-weighted)", fmt="+.1f")
            st.caption("Blue = helped, red = hurt. Repeated coverage of one story is down-weighted.")
        else:
            st.caption("No sector impact in this window.")

# --------------------------------------------------------------------------- #
# Corporate
# --------------------------------------------------------------------------- #
with tabs[4]:
    days = q("SELECT DISTINCT substr(dt,1,10) AS d FROM announcements ORDER BY d DESC LIMIT 15")["d"].tolist()
    day = st.selectbox("Session", days) if days else None
    if day:
        anns = corporate.announcements_for(day, min_importance=4, liquid_only=st.toggle("Liquid stocks only", True))
        st.subheader("Announcements")
        st.dataframe(pd.DataFrame([{"Bias": {1: "🟢", -1: "🔴", 0: "⚪"}[a["bias"]], "Stock": a["symbol"],
                                    "Event": a["label"], "₹ cr": a["amount_cr"], "Imp": a["importance"],
                                    "Text": a["text"], "PDF": a["url"]} for a in anns]),
                     hide_index=True, use_container_width=True,
                     column_config={"PDF": st.column_config.LinkColumn(display_text="open")})
        st.subheader("Bulk / block deals (net per client)")
        dl = corporate.deal_summary(day)
        if dl:
            dd = pd.DataFrame(dl)
            st.dataframe(dd[["symbol", "client", "net_cr", "avg_price", "pct_of_avg_turnover", "institutional", "kinds"]],
                         hide_index=True, use_container_width=True,
                         column_config={"net_cr": st.column_config.NumberColumn("Net ₹ cr", format="%+.2f"),
                                        "pct_of_avg_turnover": st.column_config.NumberColumn("% of avg daily value", format="%.0f")})
        else:
            st.caption("No deals stored for this session (NSE publishes only the latest session).")
    fd = q("SELECT date, category, net_cr FROM fii_dii ORDER BY date")
    if len(fd):
        st.subheader("FII / DII net cash flow (₹ cr)")
        bars = alt.Chart(fd).mark_bar(cornerRadiusEnd=4).encode(
            x=alt.X("date:N", title=None), xOffset="category:N",
            y=alt.Y("net_cr:Q", title="Net ₹ cr"),
            color=alt.Color("category:N", scale=alt.Scale(domain=["FII", "DII"], range=[BLUE, ORANGE]),
                            legend=alt.Legend(orient="top", title=None)),
            tooltip=["date:N", "category:N", alt.Tooltip("net_cr:Q", format="+,.0f")])
        chart(bars, height=240)
        if fd["date"].nunique() < 5:
            st.caption("History builds up as you run `evening` daily.")

# --------------------------------------------------------------------------- #
# Performance
# --------------------------------------------------------------------------- #
with tabs[5]:
    st.caption("Every trade is scored pessimistically and optimistically from daily bars; 'exact' uses the real "
               "intraday path (live tracker or `resolve`). Costs and slippage are deducted.")
    has_live = q("SELECT COUNT(*) AS n FROM paper_trades WHERE source='live' AND status='evaluated'")["n"].iloc[0] > 0
    src = st.radio("Trades", ["live", "backtest", "baseline"], horizontal=True, index=0 if has_live else 1,
                   format_func={"live": "Our trades (watchlist)", "backtest": "Backtest (1 yr)",
                                "baseline": "Random-stock baseline"}.get)
    df = paper.load(src)
    s = paper.stats(df)
    if "pess" in s:
        c = st.columns(4)
        c[0].metric("Signals / entered", f"{s['signals']:,} / {s['entered']:,}", f"{s['fill_rate_pct']}% filled", delta_color="off")
        c[1].metric("Win rate", f"{s['pess']['win_rate_pct']}–{s['opt']['win_rate_pct']}%")
        c[2].metric("Avg per trade (R)", f"{s['pess']['avg_R']:+.2f} to {s['opt']['avg_R']:+.2f}")
        c[3].metric("Total (₹ lakh)", f"{s['pess']['total_rs'] / 1e5:+.1f} to {s['opt']['total_rs'] / 1e5:+.1f}")
        ex = intraday.exact_stats(src)
        if ex.get("entered"):
            st.info(f"**Exact (intraday path)** on {ex['entered']} trades: {ex['avg_R']:+.2f}R per trade, "
                    f"₹{ex['total_rs']:+,.0f} total, win {ex['win_rate_pct']}% — daily-bar range for the same trades was "
                    f"{ex['pess_avg_R_same_trades']:+.2f}R to {ex['opt_avg_R_same_trades']:+.2f}R.")
        ev = df[df["status"] == "evaluated"].copy()
        curves = []
        for m, name in (("net_pess", "Pessimistic"), ("net_opt", "Optimistic")):
            c_ = ev.groupby("trade_date")[m].sum().sort_index().cumsum().reset_index()
            c_.columns = ["date", "cum"]
            c_["series"] = name
            curves.append(c_)
        cdf = pd.concat(curves)
        cdf["date"] = pd.to_datetime(cdf["date"])
        st.subheader("Cumulative P&L (₹, after costs)")
        line = alt.Chart(cdf).mark_line(strokeWidth=2).encode(
            x=alt.X("date:T", title=None), y=alt.Y("cum:Q", title="Cumulative ₹"),
            color=alt.Color("series:N", scale=alt.Scale(domain=["Pessimistic", "Optimistic"], range=[ORANGE, BLUE]),
                            legend=alt.Legend(orient="top", title=None)),
            tooltip=[alt.Tooltip("date:T"), "series:N", alt.Tooltip("cum:Q", format="+,.0f")])
        zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color=MUTED).encode(y="y:Q")
        chart(line + zero, height=280)
    else:
        st.info("No evaluated trades yet for this source." + (" Run `backtest`." if src != "live" else
                " Live trades are scored the evening after each signal."))

    edge = (read_json(str(paper.EDGE_JSON)) or {}).get("setups")
    if edge:
        st.subheader("Setup track record — next-day open→close in the predicted direction (before costs)")
        e = pd.DataFrame([{"Setup": k, "Next-day %": v["intraday_pct"], "Hit %": v["hit_pct"],
                           "5-day %": v["five_day_pct"], "Quarters +": f"{v['quarters_positive']}/{v['quarters']}",
                           "Cases": v["n"]} for k, v in edge.items()])
        diverging_bars(e, "Setup", "Next-day %", "Average next-day move (%)")
        st.dataframe(e.sort_values("Next-day %", ascending=False), hide_index=True, use_container_width=True)
        st.caption("In-sample, one year. Costs ≈ 0.15–0.2% per round trip. Treat as hypotheses for the live log.")

# --------------------------------------------------------------------------- #
# Stock lookup
# --------------------------------------------------------------------------- #
with tabs[6]:
    syms = q("SELECT DISTINCT symbol FROM daily WHERE series='EQ' AND date=(SELECT MAX(date) FROM daily) ORDER BY symbol")["symbol"]
    default = syms.tolist().index("RELIANCE") if "RELIANCE" in syms.values else 0
    sym = st.selectbox("Symbol", syms, index=default)
    d = q("SELECT date, open, high, low, close, volume, deliv_pct, turnover_lacs FROM daily WHERE symbol=? AND series='EQ' "
          "ORDER BY date", (sym,))
    if len(d):
        d["date"] = pd.to_datetime(d["date"])
        last = d.iloc[-1]
        c = st.columns(4)
        c[0].metric("Close", f"₹{last['close']:,.2f}", f"{(last['close'] / d.iloc[-2]['close'] - 1) * 100:+.2f}%" if len(d) > 1 else None)
        c[1].metric("52-week range", f"{d['low'].min():,.0f} – {d['high'].max():,.0f}")
        c[2].metric("Delivery %", f"{last['deliv_pct']:.0f}%" if pd.notna(last["deliv_pct"]) else "—")
        c[3].metric("Avg daily value (20d)", f"₹{d['turnover_lacs'].tail(20).mean() / 100:,.0f} cr")
        hover = alt.selection_point(fields=["date"], nearest=True, on="pointerover", empty=False)
        base = alt.Chart(d).encode(x=alt.X("date:T", title=None))
        price = base.mark_line(color=BLUE, strokeWidth=2).encode(y=alt.Y("close:Q", title="Close (₹)", scale=alt.Scale(zero=False)))
        pts = base.mark_point(size=60, color=BLUE, filled=True).encode(
            y="close:Q", opacity=alt.condition(hover, alt.value(1), alt.value(0)),
            tooltip=[alt.Tooltip("date:T"), alt.Tooltip("close:Q", format=",.2f"), alt.Tooltip("volume:Q", format=",.0f"),
                     alt.Tooltip("deliv_pct:Q", format=".0f", title="delivery %")]).add_params(hover)
        rule = base.mark_rule(color=MUTED).encode(opacity=alt.condition(hover, alt.value(0.6), alt.value(0))).transform_filter(hover)
        chart(price + pts + rule, height=280)
        st.caption("Volume (shares) — separate chart, same dates")
        chart(base.mark_bar(color=BLUE, opacity=0.55).encode(
            y=alt.Y("volume:Q", title=None, axis=alt.Axis(format="~s")),
            tooltip=[alt.Tooltip("date:T"), alt.Tooltip("volume:Q", format=",.0f")]), height=110)
        hist = q("SELECT signal_date, direction, setup, outcome_pess, outcome_opt, outcome_exact, net_pess, net_opt "
                 "FROM paper_trades WHERE symbol=? AND source IN ('live','backtest') ORDER BY signal_date DESC", (sym,))
        if len(hist):
            st.subheader("Past signals for this stock")
            st.dataframe(hist, hide_index=True, use_container_width=True)
        an = q("SELECT substr(dt,1,10) AS date, label, amount_cr, text FROM announcements WHERE symbol=? ORDER BY dt DESC", (sym,))
        if len(an):
            st.subheader("Announcements")
            st.dataframe(an, hide_index=True, use_container_width=True)
