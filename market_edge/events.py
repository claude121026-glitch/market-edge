"""Calendar of scheduled market-moving events.

Built from three sources:
1. Recurring rules (India CPI, India GDP, GST data, US jobs report, NSE monthly expiry).
2. Known fixed dates (FOMC 2026, published by the Fed).
3. Your own data/events.json for anything else (RBI MPC dates, US CPI, company AGMs...):
   [{"date": "2026-12-04", "time_ist": "10:00", "name": "RBI MPC decision", "importance": 9}]
Dates marked "~" are rule-based approximations: confirm close to the day.
"""
from __future__ import annotations

import calendar
import json
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from . import config

IST = ZoneInfo("Asia/Kolkata")
NY = ZoneInfo("America/New_York")

# FOMC 2026 statement days (2:00 PM New York). Source: federalreserve.gov FOMC calendar.
FOMC_2026 = ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
             "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09"]
RBI_MPC_MONTHS = (2, 4, 6, 8, 10, 12)  # bi-monthly; exact dates published by RBI each year


def _ny_to_ist(d: date, hh: int, mm: int) -> datetime:
    return datetime.combine(d, time(hh, mm), NY).astimezone(IST)


def _first_weekday(y: int, m: int, wd: int) -> date:
    d = date(y, m, 1)
    return d + timedelta(days=(wd - d.weekday()) % 7)


def _last_weekday(y: int, m: int, wd: int) -> date:
    d = date(y, m, calendar.monthrange(y, m)[1])
    return d - timedelta(days=(d.weekday() - wd) % 7)


def _last_business_day(y: int, m: int) -> date:
    d = date(y, m, calendar.monthrange(y, m)[1])
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _ev(dt: datetime | date, name: str, importance: int, approx: bool = False, note: str = "") -> dict:
    if isinstance(dt, datetime):
        d, t = dt.date(), dt.strftime("%H:%M")
    else:
        d, t = dt, None
    return {"date": d.isoformat(), "time_ist": t, "name": name, "importance": importance,
            "approx": approx, "note": note}


def generate(start: date, days: int) -> list[dict]:
    end = start + timedelta(days=days)
    evs: list[dict] = []
    months, (y, m) = [], (start.year, start.month)
    while (y, m) <= (end.year, end.month):          # every month in the window, not just the ends
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    for y, m in months:
        evs.append(_ev(datetime.combine(date(y, m, 12), time(16, 0), IST), "India CPI inflation", 8, approx=True))
        evs.append(_ev(date(y, m, 1), "India GST collections / auto sales", 4, approx=True))
        evs.append(_ev(_ny_to_ist(_first_weekday(y, m, 4), 8, 30), "US jobs report (NFP)", 8))
        evs.append(_ev(_last_weekday(y, m, 1), "NSE monthly F&O expiry (Nifty)", 6,
                       note="expiry-day volatility; verify expiry weekday on nseindia.com"))
        if m in (2, 5, 8, 11):
            evs.append(_ev(datetime.combine(_last_business_day(y, m), time(16, 0), IST),
                           "India GDP (quarterly)", 8, approx=True))
        if m in RBI_MPC_MONTHS:
            evs.append(_ev(date(y, m, 1), "RBI MPC decision this month", 9, approx=True,
                           note="exact date: rbi.org.in, add it to data/events.json"))
    for s in FOMC_2026:
        evs.append(_ev(_ny_to_ist(date.fromisoformat(s), 14, 0), "US Fed (FOMC) decision", 9))
    try:
        for e in json.loads(config.EVENTS_JSON.read_text()):
            evs.append({"approx": False, "note": "", "time_ist": None, "importance": 5, **e})
    except (OSError, ValueError):
        pass
    # User-entered RBI dates replace the monthly placeholder.
    user_rbi = {e["date"][:7] for e in evs if "rbi" in e["name"].lower() and not e["approx"]}
    evs = [e for e in evs if not (e["approx"] and "RBI MPC" in e["name"] and e["date"][:7] in user_rbi)]
    return sorted((e for e in evs if start.isoformat() <= e["date"] <= end.isoformat()),
                  key=lambda e: (e["date"], e["time_ist"] or ""))


def upcoming(days: int = 7, today: date | None = None) -> list[dict]:
    today = today or datetime.now(IST).date()
    out = generate(today, days)
    for e in out:
        delta = (date.fromisoformat(e["date"]) - today).days
        e["when"] = "TODAY" if delta == 0 else "Tomorrow" if delta == 1 else date.fromisoformat(e["date"]).strftime("%a %d %b")
    return out


def format_upcoming(evs: list[dict]) -> str:
    if not evs:
        return ""
    lines = ["<b>📅 Coming up</b>"]
    for e in evs:
        star = "🔥" if e["importance"] >= 8 else "•"
        t = f" {e['time_ist']} IST" if e["time_ist"] else ""
        approx = " (~approx)" if e["approx"] else ""
        lines.append(f"{star} <b>{e['when']}</b>{t}: {e['name']}{approx}")
    return "\n".join(lines)
