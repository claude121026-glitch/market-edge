"""Command line entry point.

    python -m market_edge.run update [--days 7]   download missing NSE daily files
    python -m market_edge.run evening [--date YYYY-MM-DD] [--no-update]
    python -m market_edge.run prices              global prices board
    python -m market_edge.run morning             full morning brief (global, macro, events, watchlist)
    python -m market_edge.run news                fetch + score macro news, alert on big items
                                                  (schedule every 15-30 min)
    python -m market_edge.run corp                announcements, bulk/block deals, FII/DII; alerts on red flags
    python -m market_edge.run refresh             quiet 30-min update of prices + news + corporate data;
                                                  messages only for NEW big moves, big news, red flags
    python -m market_edge.run backtest            replay the scan over the stored history (+ random baseline)
    python -m market_edge.run paper               live paper-trading results vs backtest
    python -m market_edge.run live [--poll 60]    track today's watchlist intraday (start ~9:05 IST)
    python -m market_edge.run resolve [--source live|backtest] [--days 55] [--limit N]
                                                  score past trades exactly with Yahoo 5-min bars
    python -m market_edge.run export-intraday OUT.db [--date YYYY-MM-DD]   (GitHub live jobs)
    python -m market_edge.run merge-intraday FILE.db [FILE.db ...]

Morning and evening run at most once per day/session, so they can safely be scheduled twice.
"""
import argparse
from datetime import date, datetime
from datetime import time as dtime
import json
import logging
from zoneinfo import ZoneInfo

from . import config, corporate, evening_scan, events, global_prices, intraday, macro_news, notify, nse, paper, watchlist


IST = ZoneInfo("Asia/Kolkata")


def _jobs_con():
    con = nse.connect()
    con.execute("CREATE TABLE IF NOT EXISTS job_runs (job TEXT, key TEXT, at TEXT, PRIMARY KEY (job, key))")
    return con


def _done(job: str, key: str | None) -> bool:
    if not key:
        return False
    con = _jobs_con()
    hit = con.execute("SELECT 1 FROM job_runs WHERE job=? AND key=?", (job, key)).fetchone() is not None
    con.close()
    return hit


def _mark(job: str, key: str) -> None:
    con = _jobs_con()
    con.execute("INSERT OR REPLACE INTO job_runs VALUES (?,?,?)", (job, key, datetime.now(IST).isoformat(timespec="seconds")))
    con.commit()
    con.close()


def _cards(make):
    """Render image cards, or None (-> text is sent) if images are off or rendering fails."""
    if not config.IMAGES:
        return None
    try:
        from . import cards
        return make(cards)
    except Exception as ex:
        logging.warning("image cards unavailable (%s: %s); sending text", type(ex).__name__, ex)
        return None


def _send_chart_analysis(top: list[dict], scan_date: str) -> None:
    """Morning: one chart per watchlist trade with the analysis behind it (a Telegram album)."""
    contexts = {}
    for p in top:
        try:
            ctx = watchlist.chart_context(p["symbol"], scan_date)
            if not ctx["bars"].empty:
                contexts[p["symbol"]] = ctx
        except Exception as ex:
            logging.warning("chart data for %s failed: %s", p["symbol"], ex)
    picks = [(i, p) for i, p in enumerate(top, 1) if p["symbol"] in contexts]
    if not picks:
        return
    text = watchlist.format_analysis(top, contexts)
    images = _cards(lambda c: [c.trade_chart(p, contexts[p["symbol"]], i, scan_date,
                                             watchlist.analysis_lines(p, contexts[p["symbol"]])) for i, p in picks])
    notify.send(text, images=images, album=True)


def _latest_date() -> str | None:
    con = nse.connect()
    d = con.execute("SELECT MAX(date) FROM daily").fetchone()[0]
    con.close()
    return d


def run_evening(date_arg: str | None = None, no_update: bool = False, force: bool = False) -> bool:
    """Evening scan + corporate data + reports. Returns False when today's report was already sent."""
    if not no_update:
        nse.update(days_back=7)
    latest = _latest_date()
    if not date_arg and not force and _done("evening", latest):
        print(f"evening: {latest} already reported (today's NSE file not out yet, or a backup run). Skipping.")
        return False
    rep = evening_scan.run(date_arg)
    day = rep["scan_date"]
    corp = corporate.run(date.fromisoformat(day))
    evening_scan.attach_catalysts(rep, corporate.catalysts_by_symbol(day))
    (config.REPORT_DIR / f"evening_{day}.json").write_text(json.dumps(rep, indent=2, default=str))
    paper.finalize_our_trades()                    # only the morning top-10 count as our trades
    evaluated = paper.evaluate_pending()           # yesterday's picks vs today's prices
    recorded = paper.record_from_report(rep)       # today's picks become tomorrow's paper trades
    try:
        exact = intraday.resolve("live", days=5)   # exact scoring from stored intraday bars
        settled = intraday.settle_ambiguous()      # ambiguous 5-min bars re-checked at 1 minute
        if settled["checked"]:
            exact = {**exact, "ambiguous_settled": settled["settled"], "still_ambiguous": settled["still_ambiguous"]}
    except Exception as ex_:
        exact = f"error: {ex_}"
    try:
        from . import small_account
        small_days = small_account.record_recent(fetch_fn=intraday.fetch_yahoo)
        if small_days:
            exact = {**exact, "small_account_days": len(small_days)} if isinstance(exact, dict) else exact
    except Exception as ex_:
        logging.error("small account record failed: %s", ex_)
    print(f"paper log: {evaluated} evaluated, {recorded} new · exact: {exact}")
    notify.send(evening_scan.format_telegram(rep), images=_cards(lambda c: c.scan_cards(rep)))
    flows, anns = corporate.fii_dii_summary(), corporate.announcements_for(day)
    results, deals = corporate.results_filed(day), corporate.deal_summary(day)
    extra = [corporate.format_fii_dii(flows), corporate.format_announcements(anns, results=results),
             corporate.format_deals(deals)]
    extra = "\n\n".join(x for x in extra if x)
    if extra:
        notify.send(extra, images=_cards(lambda c: [c.corporate_card(flows, anns, results, deals, day)]))
    errors = {k: v for k, v in corp.items() if isinstance(v, str) and v.startswith("error")}
    if errors:
        print("corporate data errors:", errors)
    if not date_arg:
        _mark("evening", day)
    return True


def run_morning(force: bool = False) -> bool:
    """Morning brief + watchlist. Returns False when today's brief was already sent."""
    today = datetime.now(IST).date().isoformat()
    if not force and _done("morning", today):
        print(f"morning: already sent for {today}. Skipping.")
        return False
    prices = global_prices.run()
    notify.send(global_prices.format_telegram(prices), images=_cards(lambda c: [c.prices_card(prices)]))
    try:
        macro_news.run()  # pick up overnight news first
    except Exception as ex:
        logging.error("news refresh failed: %s", ex)
    news = macro_news.recent(hours=18, min_importance=4)
    flows = corporate.fii_dii_summary()
    mood = macro_news.macro_score(news, prices, flows)
    sectors, evs = macro_news.sector_view(news), events.upcoming(days=7)
    brief = macro_news.format_brief(news, mood)
    if flows:
        brief += "\n\n" + corporate.format_fii_dii(flows)
    cal = events.format_upcoming(evs)
    now_ist = datetime.now(IST)
    label = f"{now_ist:%a} {now_ist.day} {now_ist:%b %Y}"
    notify.send(brief + ("\n\n" + cal if cal else ""),
                images=_cards(lambda c: c.brief_cards(news, mood, flows, sectors, evs, label)))
    reports = sorted(config.REPORT_DIR.glob("evening_*.json"))
    if reports:
        rep = json.loads(reports[-1].read_text())
        top = watchlist.build(rep, mood, sectors)
        notify.send(watchlist.format_telegram(top, rep["scan_date"]),
                    images=_cards(lambda c: [c.watchlist_card(top, rep["scan_date"])]))
        _send_chart_analysis(top, rep["scan_date"])
        from . import small_account
        plan = small_account.format_plan(top)
        if plan:
            notify.send(plan + ("\n\n" + small_account.format_summary(small_account.live_summary())
                                if small_account.live_summary() else ""))
    _mark("morning", today)
    return True


def _catch_up() -> list[str]:
    """GitHub sometimes starts or drops a scheduled run. The 30-minute refresh therefore also sends a
    morning brief or evening report that has not gone out yet (each is guarded: at most once a day)."""
    now = datetime.now(IST)
    done = []
    if now.weekday() >= 5:
        return done
    try:
        if now.time() >= dtime(8, 10) and run_morning():
            done.append("morning brief")
    except Exception as ex:
        logging.error("catch-up morning failed: %s", ex)
    try:
        if now.time() >= dtime(18, 20) and run_evening(None, False, False):
            done.append("evening report")
    except Exception as ex:
        logging.error("catch-up evening failed: %s", ex)
    return done


def main():
    ap = argparse.ArgumentParser(prog="market_edge")
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("update"); u.add_argument("--days", type=int, default=7)
    e = sub.add_parser("evening"); e.add_argument("--date"); e.add_argument("--no-update", action="store_true")
    e.add_argument("--force", action="store_true", help="re-send even if today's report was already sent")
    sub.add_parser("prices")
    mo = sub.add_parser("morning"); mo.add_argument("--force", action="store_true")
    sub.add_parser("news")
    sub.add_parser("corp")
    sub.add_parser("refresh")
    sub.add_parser("backtest")
    sub.add_parser("paper")
    sub.add_parser("telegram-check")
    sub.add_parser("settle")
    ss = sub.add_parser("small-study"); ss.add_argument("--days", type=int, default=55)
    ss.add_argument("--no-intraday", action="store_true")
    sc = sub.add_parser("small-cache"); sc.add_argument("--k", type=int, default=5)
    lv = sub.add_parser("live"); lv.add_argument("--poll", type=int, default=60)
    lv.add_argument("--until", default="15:35", help="HH:MM IST to stop (e.g. 12:10 for a morning-only run)")
    ex = sub.add_parser("export-intraday"); ex.add_argument("out"); ex.add_argument("--date")
    mg = sub.add_parser("merge-intraday"); mg.add_argument("files", nargs="+")
    rs = sub.add_parser("resolve"); rs.add_argument("--source", default="live", choices=["live", "backtest", "baseline"])
    rs.add_argument("--days", type=int, default=55); rs.add_argument("--limit", type=int)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    if a.cmd == "update":
        print("companies:", nse.update_companies())
        print("new trading dates:", nse.update(days_back=a.days))
    elif a.cmd == "evening":
        run_evening(a.date, a.no_update, a.force)
    elif a.cmd == "refresh":
        summary = []
        try:
            prices = global_prices.run()
            alerts = global_prices.new_alerts(prices)
            if alerts:
                notify.send("<b>⚠️ Market alerts</b>\n" + "\n".join(f"• {x}" for x in alerts))
            fresh = sum(1 for p in prices["prices"] if "price" in p and not p.get("stale"))
            summary.append(f"prices {fresh}/{len(prices['prices'])} fresh, {len(alerts)} alerts")
        except Exception as ex:
            summary.append(f"prices ERROR {type(ex).__name__}")
            logging.error("prices refresh failed: %s", ex)
        try:
            res = macro_news.run()
            for item in res["alerts"]:
                notify.send(macro_news.format_alert(item))
            summary.append(f"news {res['new']} new, {len(res['alerts'])} alerts ({res['scored_by']})")
        except Exception as ex:
            summary.append(f"news ERROR {type(ex).__name__}")
            logging.error("news refresh failed: %s", ex)
        try:
            res = corporate.run()
            flags = corporate.pending_alerts()
            for al in flags:
                notify.send(corporate.format_alert(al))
            summary.append(f"corporate ok, {len(flags)} red flags")
        except Exception as ex:
            summary.append(f"corporate ERROR {type(ex).__name__}")
            logging.error("corporate refresh failed: %s", ex)
        late = _catch_up()
        if late:
            summary.append("sent late: " + ", ".join(late))
        print("refresh:", " | ".join(summary))
    elif a.cmd == "small-study":
        from . import small_account
        rep = small_account.run_study(a.days, with_intraday=not a.no_intraday)
        text = small_account.format_report(rep["daily"], rep["intraday"])
        print(text.replace("<b>", "").replace("</b>", ""))
        notify.send(text)
    elif a.cmd == "small-cache":
        import os
        import subprocess
        from . import small_account
        for interval, rng, last, name in (("60m", "2y", None, "bars-60m.db"), ("5m", "60d", 60, "bars-5m.db")):
            path = f"/tmp/{name}"
            print(small_account.build_cache(path, interval, rng, small_account.cache_symbols(a.k, last)))
            if os.getenv("GITHUB_ACTIONS"):
                subprocess.run(["bash", "market_edge/deploy/github/data.sh", "push-file", path], check=False)
    elif a.cmd == "settle":
        res = intraday.settle_ambiguous()
        print("settle:", {k: v for k, v in res.items() if k != "changes"})
        for line in res["changes"]:
            print("  ", line)
    elif a.cmd == "telegram-check":
        for line in notify.diagnose_telegram():
            print(line)
    elif a.cmd == "backtest":
        print(paper.backtest())
        edge = paper.setup_edge()
        for src in ("backtest", "baseline"):
            notify.send(paper.format_stats(f"{src.title()} (1 yr, next-day intraday plan)", paper.stats(paper.load(src))))
        print("\nSetup track record (next-day open->close, direction-adjusted, before costs):")
        for k, v in sorted(edge.items(), key=lambda kv: -kv[1]["intraday_pct"]):
            print(f"  {k:18} {v['intraday_pct']:+.2f}%  hit {v['hit_pct']}%  5d {v['five_day_pct']:+.2f}%  "
                  f"{v['quarters_positive']}/{v['quarters']} qtrs  n={v['n']}")
    elif a.cmd == "paper":
        paper.finalize_our_trades()
        paper.evaluate_pending()
        notify.send(paper.format_stats("Our trades (morning top-10 watchlist)", paper.stats(paper.load("live"))))
        from . import small_account
        small_account.record_recent(fetch_fn=intraday.fetch_yahoo)
        txt = small_account.format_summary(small_account.live_summary())
        if txt:
            notify.send(txt)
        notify.send(paper.format_stats("Backtest reference", paper.stats(paper.load("backtest"))))
        for src in ("live", "backtest"):
            ex = intraday.exact_stats(src)
            if ex:
                notify.send(intraday.format_exact(f"Exact intraday results ({'our trades' if src == 'live' else src})", ex))
    elif a.cmd == "live":
        hh, mm = (int(x) for x in a.until.split(":"))
        print(intraday.run_live(poll=a.poll, until=dtime(hh, mm)))
    elif a.cmd == "export-intraday":
        day = a.date or datetime.now(IST).date().isoformat()
        print("exported bars:", intraday.export_day(a.out, day))
    elif a.cmd == "merge-intraday":
        print("merged bars:", intraday.merge_files(a.files))
    elif a.cmd == "resolve":
        print(intraday.resolve(a.source, a.days, a.limit))
        notify.send(intraday.format_exact(f"Exact intraday results ({a.source})", intraday.exact_stats(a.source)))
    elif a.cmd == "corp":
        res = corporate.run()
        for al in corporate.pending_alerts():
            notify.send(corporate.format_alert(al))
        print("corporate:", res)
    elif a.cmd == "prices":
        prices = global_prices.run()
        notify.send(global_prices.format_telegram(prices), images=_cards(lambda c: [c.prices_card(prices)]))
    elif a.cmd == "news":
        res = macro_news.run()
        for r in res["alerts"]:
            notify.send(macro_news.format_alert(r))
        print(f"news: fetched {res['fetched']} · new {res['new']} · relevant {res['scored']} "
              f"· scored by {res['scored_by']} · alerts {len(res['alerts'])}"
              + (f" · LLM error: {res['llm_error']}" if res["llm_error"] else ""))
        bad = [k for k, v in res["feeds"].items() if not v["ok"]]
        if bad:
            print("feeds failing:", ", ".join(bad))
    elif a.cmd == "morning":
        run_morning(a.force)


if __name__ == "__main__":
    main()
