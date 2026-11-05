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


def _latest_date() -> str | None:
    con = nse.connect()
    d = con.execute("SELECT MAX(date) FROM daily").fetchone()[0]
    con.close()
    return d


def main():
    ap = argparse.ArgumentParser(prog="market_edge")
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("update"); u.add_argument("--days", type=int, default=7)
    e = sub.add_parser("evening"); e.add_argument("--date"); e.add_argument("--no-update", action="store_true")
    sub.add_parser("prices")
    sub.add_parser("morning")
    sub.add_parser("news")
    sub.add_parser("corp")
    sub.add_parser("refresh")
    sub.add_parser("backtest")
    sub.add_parser("paper")
    sub.add_parser("telegram-check")
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
        if not a.no_update:
            nse.update(days_back=7)
        latest = _latest_date()
        if not a.date and _done("evening", latest):
            print(f"evening: {latest} already reported (today's NSE file not out yet, or a backup run). Skipping.")
            return
        rep = evening_scan.run(a.date)
        day = rep["scan_date"]
        corp = corporate.run(date.fromisoformat(day))
        evening_scan.attach_catalysts(rep, corporate.catalysts_by_symbol(day))
        (config.REPORT_DIR / f"evening_{day}.json").write_text(json.dumps(rep, indent=2, default=str))
        evaluated = paper.evaluate_pending()           # yesterday's picks vs today's prices
        recorded = paper.record_from_report(rep)       # today's picks become tomorrow's paper trades
        try:
            exact = intraday.resolve("live", days=5)   # exact scoring from stored intraday bars
        except Exception as ex_:
            exact = f"error: {ex_}"
        print(f"paper log: {evaluated} evaluated, {recorded} new · exact: {exact}")
        notify.send(evening_scan.format_telegram(rep))
        extra = [corporate.format_fii_dii(corporate.fii_dii_summary()),
                 corporate.format_announcements(corporate.announcements_for(day), results=corporate.results_filed(day)),
                 corporate.format_deals(corporate.deal_summary(day))]
        extra = "\n\n".join(x for x in extra if x)
        if extra:
            notify.send(extra)
        errors = {k: v for k, v in corp.items() if isinstance(v, str) and v.startswith("error")}
        if errors:
            print("corporate data errors:", errors)
        if not a.date:
            _mark("evening", day)
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
        print("refresh:", " | ".join(summary))
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
        paper.evaluate_pending()
        live = paper.load("live")
        notify.send(paper.format_stats("Live paper log (all picks)", paper.stats(live)))
        top = live[live["morning_rank"].notna()]
        if len(top):
            notify.send(paper.format_stats("Live paper log (morning top 10)", paper.stats(top)))
        notify.send(paper.format_stats("Backtest reference", paper.stats(paper.load("backtest"))))
        for src in ("live", "backtest"):
            ex = intraday.exact_stats(src)
            if ex:
                notify.send(intraday.format_exact(f"Exact intraday results ({src})", ex))
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
        notify.send(global_prices.format_telegram(global_prices.run()))
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
        today = datetime.now(IST).date().isoformat()
        if _done("morning", today):
            print(f"morning: already sent for {today}. Skipping.")
            return
        prices = global_prices.run()
        notify.send(global_prices.format_telegram(prices))
        try:
            macro_news.run()  # pick up overnight news first
        except Exception as ex:
            logging.error("news refresh failed: %s", ex)
        news = macro_news.recent(hours=18, min_importance=4)
        flows = corporate.fii_dii_summary()
        brief = macro_news.format_brief(news, macro_news.macro_score(news, prices, flows))
        if flows:
            brief += "\n\n" + corporate.format_fii_dii(flows)
        cal = events.format_upcoming(events.upcoming(days=7))
        notify.send(brief + ("\n\n" + cal if cal else ""))
        reports = sorted(config.REPORT_DIR.glob("evening_*.json"))
        if reports:
            rep = json.loads(reports[-1].read_text())
            mood = macro_news.macro_score(news, prices, flows)
            top = watchlist.build(rep, mood, macro_news.sector_view(news))
            notify.send(watchlist.format_telegram(top, rep["scan_date"]))
        _mark("morning", today)


if __name__ == "__main__":
    main()
