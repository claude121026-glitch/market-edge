"""Small-account mode: e.g. Rs 10,000 of capital, 5x intraday leverage, ONE position at a time.

Each day the top-ranked candidates are watched; the first one that triggers is the day's only trade.
Two plans:
  scan   the scanner's own levels (entry = break of signal-day high/low, stop = pivot, target = 2R),
         sized so `risk_pct` of capital is lost if the stop is hit (position capped at capital x leverage)
  fixed  entry as above, stop and target a fixed % away, full position (capital x leverage)

Two ways to test it:
  daily      one year of daily bars. Which candidate triggers first is unknown, so candidates are taken
             in rank order; the stop/target order inside the day is unknown, so results are a range
             (pessimistic .. optimistic), as in paper.py.
  intraday   real 5-minute paths (Yahoo keeps ~60 days): the first candidate to trigger in time is taken
             and the exit is exact.
"""
from __future__ import annotations

import json
import logging
import math
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime
from datetime import time as dtime

import pandas as pd

from . import config, intraday, net, paper

log = logging.getLogger("market_edge.small")


@dataclass
class Params:
    capital: float = 10_000
    leverage: float = 5
    plan: str = "scan"            # "scan" or "fixed"
    risk_pct: float = 1.0         # scan plan: % of capital at risk per trade
    stop_pct: float = 0.4         # fixed plan: stop distance from entry, % of price
    target_pct: float = 0.5       # fixed plan: target distance from entry, % of price
    candidates: int = 3           # top-ranked picks watched each day
    side: str = "both"            # "both", "long" or "short"
    skip_breakouts: bool = False  # skip the "Volume breakout" setup (it faded in the backtest)
    last_entry: str | None = None  # "HH:MM": no new entry after this time
    label: str = field(default="", compare=False)

    @property
    def max_position(self) -> float:
        return self.capital * self.leverage

    def name(self) -> str:
        if self.label:
            return self.label
        if self.plan == "scan":
            return f"Scanner levels, {self.risk_pct:g}% risk"
        extra = [f"top {self.candidates}"]
        if self.side != "both":
            extra.append(f"{self.side}s only")
        if self.skip_breakouts:
            extra.append("no volume breakouts")
        if self.last_entry:
            extra.append(f"entries until {self.last_entry}")
        return f"Fixed: target +{self.target_pct:g}% / stop -{self.stop_pct:g}% · " + " · ".join(extra)


def filter_cands(cands: list[dict], p: Params) -> list[dict]:
    out = [c for c in cands if p.side == "both" or (c["direction"] == "bullish") == (p.side == "long")]
    if p.skip_breakouts:
        out = [c for c in out if not str(c.get("setup", "")).startswith("Volume breakout")]
    return out


def levels(c: dict, p: Params) -> tuple[float, float, float]:
    if p.plan == "scan":
        return c["trigger"], c["stop"], c["target"]
    e, s = c["trigger"], 1 if c["direction"] == "bullish" else -1
    return e, e * (1 - s * p.stop_pct / 100), e * (1 + s * p.target_pct / 100)


def qty_for(entry: float, stop: float, p: Params) -> int:
    if entry <= 0 or abs(entry - stop) <= 0:
        return 0
    q = p.max_position / entry
    if p.plan == "scan":
        q = min(q, p.capital * p.risk_pct / 100 / abs(entry - stop))
    return int(math.floor(q))


# --------------------------------------------------------------------------- #
# Day simulations
# --------------------------------------------------------------------------- #
def fixed_from_entry(direction: str, entry: float, p: Params) -> tuple[float, float]:
    """Fixed plan: stop and target are measured from the actual fill, not from the trigger."""
    sgn = 1 if direction == "bullish" else -1
    return entry * (1 - sgn * p.stop_pct / 100), entry * (1 + sgn * p.target_pct / 100)


def _too_far(direction: str, trigger: float, entry: float, p: Params) -> bool:
    """Fixed plan: skip a fill that gapped more than one stop-width past the trigger (chasing)."""
    if p.plan != "fixed":
        return False
    move = (entry / trigger - 1) * 100 * (1 if direction == "bullish" else -1)
    return move > p.stop_pct


def day_daily(cands: list[dict], bars: dict, p: Params) -> dict | None:
    """Daily bars: first candidate (rank order) whose next-day bar triggers. Returns pess/opt results."""
    for c in filter_cands(cands, p)[:p.candidates]:
        bar = bars.get(c["symbol"])
        if not bar:
            continue
        e, s, t = levels(c, p)
        if p.plan == "fixed":
            bull = c["direction"] == "bullish"
            if (bull and bar["high"] < e) or (not bull and bar["low"] > e):
                continue
            fill = max(e, bar["open"]) if bull else min(e, bar["open"])
            if _too_far(c["direction"], e, fill, p):
                continue
            s, t = fixed_from_entry(c["direction"], fill, p)
            e = fill
        fp = paper.simulate(c["direction"], e, s, t, bar["open"], bar["high"], bar["low"], bar["close"], "pess")
        if fp.outcome == "no_entry":
            continue
        fo = paper.simulate(c["direction"], e, s, t, bar["open"], bar["high"], bar["low"], bar["close"], "opt")
        q = qty_for(fp.entry, s, p)
        if q == 0:
            continue
        net_p, cost = paper.pnl(c["direction"], fp.entry, fp.exit, q)
        net_o, _ = paper.pnl(c["direction"], fo.entry, fo.exit, q)
        return {"symbol": c["symbol"], "direction": c["direction"], "qty": q, "position": round(q * fp.entry, 2),
                "net_pess": round(net_p, 2), "net_opt": round(net_o, 2), "costs": round(cost, 2),
                "outcome_pess": fp.outcome, "outcome_opt": fo.outcome}
    return None


def day_intraday(cands: list[dict], paths: dict, p: Params) -> dict | None:
    """Real 5-minute paths: the candidate that triggers FIRST is the day's single position."""
    best = None
    cutoff = dtime(*map(int, p.last_entry.split(":"))) if p.last_entry else None
    for rank, c in enumerate(filter_cands(cands, p)[:p.candidates]):
        bars = paths.get(c["symbol"])
        if not bars:
            continue
        e, s, t = levels(c, p)
        f = intraday.simulate_path(c["direction"], e, s, t, bars)
        if p.plan == "fixed" and f.entry_ts is not None:
            if _too_far(c["direction"], e, f.entry, p):
                continue
            s, t = fixed_from_entry(c["direction"], f.entry, p)
            from_entry = [b for b in bars if b.ts >= f.entry_ts]
            f = intraday.simulate_path(c["direction"], f.entry, s, t, from_entry)
        if f.outcome == "no_entry" or f.entry_ts is None:
            continue
        if cutoff and f.entry_ts.time() > cutoff:
            continue
        key = (f.entry_ts, rank)
        if best is None or key < best[0]:
            best = (key, c, f, s)
    if best is None:
        return None
    _, c, f, s = best
    t = fixed_from_entry(c["direction"], f.entry, p)[1] if p.plan == "fixed" else c["target"]
    q = qty_for(f.entry, s, p)
    if q == 0:
        return None
    net_, cost = paper.pnl(c["direction"], f.entry, f.exit, q)
    return {"symbol": c["symbol"], "direction": c["direction"], "qty": q, "position": round(q * f.entry, 2),
            "entry_time": f.entry_ts.strftime("%H:%M"), "exit_time": f.exit_ts.strftime("%H:%M") if f.exit_ts else None,
            "entry_ts": f.entry_ts, "exit_ts": f.exit_ts, "entry": round(f.entry, 2), "exit": round(f.exit, 2),
            "stop": round(s, 2), "target": round(t, 2) if p.plan == "fixed" else round(c["target"], 2),
            "outcome": f.outcome, "ambiguous": f.ambiguous, "net": round(net_, 2), "costs": round(cost, 2)}


# --------------------------------------------------------------------------- #
# Summaries
# --------------------------------------------------------------------------- #
def summarize(results: list[float], sessions: int, p: Params, costs: list[float] | None = None) -> dict:
    """results: Rs P&L per traded day (fixed capital, no compounding); sessions: days in the test."""
    s = pd.Series(results, dtype=float)
    if s.empty:
        return {"sessions": sessions, "trade_days": 0}
    pct = s / p.capital * 100
    equity = s.cumsum()
    dd = (equity.cummax().clip(lower=0) - equity).max()
    comp = float((1 + pct / 100).prod() * p.capital)
    return {
        "sessions": sessions, "trade_days": int(len(s)), "no_trade_days": int(sessions - len(s)),
        "win_days_pct": round(float((s > 0).mean() * 100), 1),
        "avg_day_pct": round(float(pct.mean()), 2),
        "avg_per_session_pct": round(float(s.sum() / p.capital * 100 / max(1, sessions)), 3),
        "goal_days_pct": round(float((pct >= 2).mean() * 100), 1),
        "loss_days_2pct_pct": round(float((pct <= -2).mean() * 100), 1),
        "best_day_pct": round(float(pct.max()), 2), "worst_day_pct": round(float(pct.min()), 2),
        "total_rs": round(float(s.sum()), 0), "total_pct": round(float(s.sum() / p.capital * 100), 1),
        "max_drawdown_rs": round(float(dd), 0), "max_drawdown_pct": round(float(dd / p.capital * 100), 1),
        "compounded_end_rs": round(comp, 0),
        "avg_costs_rs": round(float(pd.Series(costs).mean()), 0) if costs else None,
    }


# --------------------------------------------------------------------------- #
# Studies
# --------------------------------------------------------------------------- #
def _candidates_by_day(con, source: str = "backtest") -> dict[str, list[dict]]:
    df = pd.read_sql_query("SELECT signal_date, symbol, direction, rank, score, setup, trigger, stop, target "
                           "FROM paper_trades WHERE source=?", con, params=(source,))
    out = {}
    for d, g in df.groupby("signal_date"):
        out[d] = g.sort_values(["score", "rank"], ascending=[False, True]).to_dict("records")
    return out


def daily_study(params: list[Params]) -> dict:
    con = paper.connect()
    cands = _candidates_by_day(con)
    px = pd.read_sql_query("SELECT symbol, date, open, high, low, close FROM daily WHERE series='EQ'", con)
    con.close()
    dates = sorted(px["date"].unique())
    nxt = dict(zip(dates[:-1], dates[1:]))
    by_day = {d: g.set_index("symbol")[["open", "high", "low", "close"]].to_dict("index") for d, g in px.groupby("date")}
    out = {"from": min(cands), "to": max(cands), "runs": []}
    for p in params:
        pess, opt, costs, sessions = [], [], [], 0
        for d in sorted(cands):
            td = nxt.get(d)
            if not td:
                continue
            sessions += 1
            r = day_daily(cands[d], by_day.get(td, {}), p)
            if r:
                pess.append(r["net_pess"]); opt.append(r["net_opt"]); costs.append(r["costs"])
        out["runs"].append({"params": asdict(p), "name": p.name(),
                            "pessimistic": summarize(pess, sessions, p, costs), "optimistic": summarize(opt, sessions, p, costs)})
    return out


def _fetch_60d(symbol: str) -> dict[str, list]:
    sym = urllib.parse.quote(symbol, safe="")
    raw = None
    for n in (1, 2):
        try:
            raw = net.get(f"https://query{n}.finance.yahoo.com/v8/finance/chart/{sym}.NS?interval=5m&range=60d",
                          timeout=30, retries=2, backoff=(5, 15))
            break
        except net.HttpError:
            continue
    if raw is None:
        return {}
    by_day: dict[str, list] = {}
    for b in intraday.parse_yahoo(json.loads(raw), None):
        by_day.setdefault(b.ts.date().isoformat(), []).append(b)
    return by_day


def intraday_study(params: list[Params], days: int = 55, fetch_fn=None) -> dict:
    """Exact study on real 5-minute paths for the last `days` signal days that Yahoo still covers."""
    fetch_fn = fetch_fn or _fetch_60d
    con = paper.connect()
    cands = _candidates_by_day(con)
    dates = [r[0] for r in con.execute("SELECT DISTINCT date FROM daily ORDER BY date")]
    con.close()
    nxt = dict(zip(dates[:-1], dates[1:]))
    signal_days = [d for d in sorted(cands) if d in nxt][-days:]
    k = max(p.candidates for p in params)
    symbols = sorted({c["symbol"] for d in signal_days for c in cands[d][:k]})
    cache, failed = {}, []
    for i, s in enumerate(symbols, 1):
        try:
            cache[s] = fetch_fn(s)
        except Exception as e:
            failed.append(s)
            log.warning("5-minute history for %s failed: %s", s, e)
        if i % 25 == 0:
            log.info("fetched %d/%d symbols", i, len(symbols))
    covered = [d for d in signal_days if any(cache.get(c["symbol"], {}).get(nxt[d]) for c in cands[d][:k])]
    out = {"from": covered[0] if covered else None, "to": covered[-1] if covered else None, "signal_days": len(covered),
           "symbols": len(symbols), "fetch_failed": len(failed), "runs": []}
    for p in params:
        res, costs, trades, amb = [], [], [], 0
        for d in covered:
            paths = {c["symbol"]: cache.get(c["symbol"], {}).get(nxt[d], []) for c in cands[d][:p.candidates]}
            r = day_intraday(cands[d], paths, p)
            if r:
                res.append(r["net"]); costs.append(r["costs"]); amb += int(r["ambiguous"])
                trades.append({"date": nxt[d], **r})
        out["runs"].append({"params": asdict(p), "name": p.name(), "exact": summarize(res, len(covered), p, costs),
                            "ambiguous_trades": amb, "last_trades": trades[-10:]})
    return out


FULL = 100.0   # risk_pct high enough that the position is always capped at capital x leverage
DEFAULT_DAILY = [
    Params(plan="scan", risk_pct=FULL, label="Scanner levels (pivot stop, 2R target), full ₹50k position"),
    Params(plan="fixed", target_pct=2.0, stop_pct=1.0), Params(plan="fixed", target_pct=2.5, stop_pct=1.25),
    Params(plan="fixed", target_pct=3.0, stop_pct=1.5), Params(plan="fixed", target_pct=2.0, stop_pct=2.0)]
DEFAULT_INTRADAY = DEFAULT_DAILY


def format_report(daily: dict | None, intra: dict | None, capital: float = 10_000) -> str:
    def row(name, s):
        if not s or not s.get("trade_days"):
            return f"• {name}: no trades"
        return (f"• {name}: {s['trade_days']}/{s['sessions']} days traded · win days {s['win_days_pct']}% · "
                f"avg {s['avg_day_pct']:+.2f}% per trade day · ≥2% days {s['goal_days_pct']}% · "
                f"total {s['total_pct']:+.1f}% (₹{s['total_rs']:+,.0f}) · max drawdown {s['max_drawdown_pct']}% · "
                f"worst day {s['worst_day_pct']:+.1f}% · costs ~₹{s['avg_costs_rs']}/trade")
    lines = [f"<b>💼 Small-account study · ₹{capital:,.0f} capital · ₹{capital * 5:,.0f} position (5×) · one trade a day</b>"]
    if daily:
        lines.append(f"\n<b>1 year, daily bars ({daily['from']} → {daily['to']}), pessimistic / optimistic</b>")
        for r in daily["runs"]:
            lines += [row(r["name"] + " (pess)", r["pessimistic"]), row(r["name"] + " (opt)", r["optimistic"])]
    if intra:
        lines.append(f"\n<b>Real 5-minute prices, {intra['signal_days']} days ({intra['from']} → {intra['to']}), exact</b>")
        for r in intra["runs"]:
            lines.append(row(r["name"], r["exact"]))
    return "\n".join(lines)


def run_study(intraday_days: int = 55, with_intraday: bool = True) -> dict:
    daily = daily_study(DEFAULT_DAILY)
    intra = intraday_study(DEFAULT_INTRADAY, intraday_days) if with_intraday else None
    rep = {"generated_at": datetime.now().isoformat(timespec="seconds"), "daily": daily, "intraday": intra}
    config.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (config.REPORT_DIR / "small_account_study.json").write_text(json.dumps(rep, indent=2, default=str))
    return rep


# --------------------------------------------------------------------------- #
# Live paper account (runs alongside the normal watchlist tracking)
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS small_trades (
    date TEXT PRIMARY KEY, signal_date TEXT, symbol TEXT, direction TEXT, qty INTEGER, entry REAL, exit REAL,
    stop REAL, target REAL, outcome TEXT, net REAL, position REAL, entry_time TEXT, exit_time TEXT,
    ambiguous INTEGER, params TEXT
);
"""


def live_params() -> Params | None:
    """The live paper account. Defaults: Rs 10,000, 5x, target +3% / stop -1.5% from the fill, watch the
    top 3 of the morning watchlist, first trigger only. Override with repository variables; ME_SMALL_ACCOUNT=0 turns it off."""
    import os
    if os.getenv("ME_SMALL_ACCOUNT", "1") == "0":
        return None
    f = lambda k, d: float(os.getenv(k) or d)  # noqa: E731
    return Params(capital=f("ME_SMALL_CAPITAL", 10_000), leverage=f("ME_SMALL_LEVERAGE", 5), plan="fixed",
                  target_pct=f("ME_SMALL_TARGET_PCT", 3.0), stop_pct=f("ME_SMALL_STOP_PCT", 1.5),
                  candidates=int(f("ME_SMALL_CANDIDATES", 3)))


def _con():
    con = intraday.connect()
    con.executescript(SCHEMA)
    return con


def candidates_for(con, signal_date: str) -> list[dict]:
    """Our watchlist for a signal day in morning-rank order (score order if the morning never ranked it)."""
    rows = con.execute("SELECT symbol, direction, trigger, stop, target, morning_rank, score FROM paper_trades "
                       "WHERE source='live' AND signal_date=? ORDER BY morning_rank IS NULL, morning_rank, score DESC",
                       (signal_date,)).fetchall()
    return [dict(zip(("symbol", "direction", "trigger", "stop", "target", "morning_rank", "score"), r)) for r in rows]


def record_recent(days: int = 10, p: Params | None = None, fetch_fn=None) -> list[dict]:
    """Evening: work out the paper account's single trade for each recent trading day from the intraday
    paths (the live tracker's bars; Yahoo 5-minute bars if one is missing) and store it once."""
    p = p or live_params()
    if p is None:
        return []
    con = _con()
    since = (datetime.now(intraday.IST).date() - pd.Timedelta(days=days)).isoformat()
    done = {r[0] for r in con.execute("SELECT date FROM small_trades")}
    out = []
    for (sd,) in con.execute("SELECT DISTINCT signal_date FROM paper_trades WHERE source='live' AND signal_date>=? "
                             "ORDER BY signal_date", (since,)).fetchall():
        td = con.execute("SELECT MIN(date) FROM daily WHERE date > ?", (sd,)).fetchone()[0]
        if not td or td in done:
            continue
        cands = candidates_for(con, sd)[:p.candidates]
        paths = {}
        for c in cands:
            bars = intraday.load_bars(con, c["symbol"], td)
            if not bars and fetch_fn:
                try:
                    bars = fetch_fn(c["symbol"], pd.Timestamp(td).date())
                except Exception as e:
                    log.warning("bars for %s %s: %s", c["symbol"], td, e)
            paths[c["symbol"]] = bars
        if not any(paths.values()):
            continue                                    # no intraday data for that day: try again later
        r = day_intraday(cands, paths, p)
        row = {"date": td, "signal_date": sd, "params": json.dumps(asdict(p))}
        if r:
            row.update({k: r[k] for k in ("symbol", "direction", "qty", "entry", "exit", "stop", "target", "outcome",
                                          "net", "position", "entry_time", "exit_time")}, ambiguous=int(r["ambiguous"]))
        else:
            row.update(outcome="none", net=0.0)
        cols = ",".join(row)
        con.execute(f"INSERT OR REPLACE INTO small_trades ({cols}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))
        out.append(row)
    con.commit()
    con.close()
    return out


def live_summary(p: Params | None = None) -> dict | None:
    p = p or live_params()
    if p is None:
        return None
    con = _con()
    df = pd.read_sql_query("SELECT * FROM small_trades ORDER BY date", con)
    con.close()
    if df.empty:
        return {"days": 0}
    traded = df[df["outcome"] != "none"]
    s = summarize(traded["net"].tolist(), sessions=len(df), p=p)
    s.update(days=len(df), account_rs=round(p.capital + float(df["net"].sum()), 0),
             targets=int((traded["outcome"] == "win").sum()), stops=int((traded["outcome"] == "loss").sum()),
             closes=int((traded["outcome"] == "close").sum()), first=df["date"].iloc[0], last=df["date"].iloc[-1])
    return s


def format_summary(s: dict | None, p: Params | None = None) -> str:
    p = p or live_params()
    if not s or not p:
        return ""
    if not s.get("days"):
        return f"<b>💼 ₹{p.capital:,.0f} paper account</b>: no trading days recorded yet."
    return (f"<b>💼 ₹{p.capital:,.0f} paper account</b> ({s['first']} → {s['last']})\n"
            f"Account now ₹{s['account_rs']:,.0f} ({s.get('total_pct', 0):+.1f}%) · {s.get('trade_days', 0)} trades in {s['days']} days\n"
            f"Targets {s['targets']} · stops {s['stops']} · closed at 3:20 {s['closes']} · "
            f"max drawdown {s.get('max_drawdown_pct', 0)}% · worst day {s.get('worst_day_pct', 0):+.1f}%")


def format_plan(top: list[dict], p: Params | None = None) -> str:
    """Morning: the paper account's rules and today's candidates."""
    p = p or live_params()
    if not p or not top:
        return ""
    lines = [f"<b>💼 ₹{p.capital:,.0f} account plan for today</b> (5× → ₹{p.max_position:,.0f} position)",
             f"Watch #1–{p.candidates}. Take the <b>first</b> to trigger, one trade only. Target +{p.target_pct:g}%, "
             f"stop −{p.stop_pct:g}% from your fill; skip it if it opens more than {p.stop_pct:g}% past the entry. "
             f"Square off by 3:20 PM."]
    for i, c in enumerate(top[:p.candidates], 1):
        long_ = c["direction"] == "bullish"
        e = c["trigger"]
        s, t = fixed_from_entry(c["direction"], e, p)
        q = qty_for(e, s, p)
        lines.append(f"{i}. {'🟢 BUY' if long_ else '🔴 SHORT'} <b>{c['symbol']}</b> {'above' if long_ else 'below'} {e:,.2f} · "
                     f"qty {q:,} (₹{q * e:,.0f}) · target ≈{t:,.2f} (+₹{abs(t - e) * q:,.0f}) · stop ≈{s:,.2f} (−₹{abs(e - s) * q:,.0f})")
    lines.append("<i>55-day test: +73% total but a 52% drawdown along the way, best of 5 settings. Paper-trade only.</i>")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Bar caches for the parameter search (hourly bars ~2 years back, 5-minute ~60 days)
# --------------------------------------------------------------------------- #
def build_cache(path: str, interval: str, rng: str, symbols: list[str]) -> dict:
    import sqlite3
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS bars (symbol TEXT, date TEXT, ts TEXT, open REAL, high REAL, low REAL, "
                "close REAL, PRIMARY KEY (symbol, ts))")
    ok = failed = 0
    for i, sym in enumerate(symbols, 1):
        q = urllib.parse.quote(sym, safe="")
        raw = None
        for n in (1, 2):
            try:
                raw = net.get(f"https://query{n}.finance.yahoo.com/v8/finance/chart/{q}.NS?interval={interval}&range={rng}",
                              timeout=30, retries=2, backoff=(5, 15))
                break
            except Exception:
                continue
        if raw is None:
            failed += 1
            continue
        try:
            bars = intraday.parse_yahoo(json.loads(raw), None)
        except Exception:
            failed += 1
            continue
        con.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?)",
                        [(sym, b.ts.date().isoformat(), b.ts.isoformat(), b.open, b.high, b.low, b.close) for b in bars])
        ok += 1
        if i % 50 == 0:
            con.commit()
            log.info("%s cache: %d/%d symbols", interval, i, len(symbols))
    con.commit()
    con.close()
    return {"interval": interval, "symbols": len(symbols), "ok": ok, "failed": failed}


def cache_symbols(k: int = 5, last_days: int | None = None) -> list[str]:
    con = paper.connect()
    cands = _candidates_by_day(con)
    con.close()
    days = sorted(cands)[-last_days:] if last_days else sorted(cands)
    return sorted({c["symbol"] for d in days for c in cands[d][:k]})


def load_cache(path: str) -> dict[str, dict[str, list]]:
    import sqlite3
    out: dict[str, dict[str, list]] = {}
    for sym, d, ts, o, h, l, c in sqlite3.connect(path).execute("SELECT * FROM bars ORDER BY symbol, ts"):
        out.setdefault(sym, {}).setdefault(d, []).append(intraday.Bar(datetime.fromisoformat(ts), o, h, l, c))
    return out


def run_on_cache(cache: dict, p: Params, days: list[str] | None = None, pool_size: int = 5) -> dict:
    """Exact-as-the-bars-allow simulation of one setting over the cached days."""
    con = paper.connect()
    cands = _candidates_by_day(con)
    dates = [r[0] for r in con.execute("SELECT DISTINCT date FROM daily ORDER BY date")]
    con.close()
    nxt = dict(zip(dates[:-1], dates[1:]))
    days = days or [d for d in sorted(cands) if d in nxt]
    res, costs, amb, trades = [], [], 0, []
    covered = 0
    for d in days:
        td = nxt.get(d)
        if not td:
            continue
        pool = cands[d][:pool_size]               # only the candidates the cache covers
        paths = {c["symbol"]: cache.get(c["symbol"], {}).get(td, []) for c in pool}
        if not any(paths.values()):
            continue
        covered += 1
        r = day_intraday(pool, paths, p)
        if r:
            res.append(r["net"]); costs.append(r["costs"]); amb += int(r["ambiguous"])
            trades.append({"date": td, "symbol": r["symbol"], "direction": r["direction"], "outcome": r["outcome"],
                           "net": r["net"], "entry_time": r["entry_time"]})
    out = summarize(res, covered, p, costs)
    out["ambiguous"] = amb
    out["outcomes"] = pd.Series([t["outcome"] for t in trades]).value_counts().to_dict() if trades else {}
    return {"summary": out, "trades": trades}


def grid() -> list[Params]:
    out = []
    for tgt in (1.5, 2.0, 2.5, 3.0):
        for stp in (0.75, 1.0, 1.5, 2.0):
            if stp > tgt:
                continue
            for k in (1, 3, 5):
                for side in ("both", "long", "short"):
                    for skip in (False, True):
                        for cut in (None, "11:15"):
                            out.append(Params(plan="fixed", target_pct=tgt, stop_pct=stp, candidates=k, side=side,
                                              skip_breakouts=skip, last_entry=cut))
    return out


def score(s: dict) -> float:
    """Return relative to the worst drawdown (at least 10% so tiny samples don't look perfect)."""
    if not s.get("trade_days"):
        return float("-inf")
    return s["total_pct"] / max(10.0, s["max_drawdown_pct"])
