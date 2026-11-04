"""NSE daily data: download, cache and store the 'sec_bhavdata_full' file.

One file per trading day holds OHLC, volume, turnover, trades AND delivery qty / %.
URL: https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv
"""
from __future__ import annotations

import io
import logging
import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from . import config, net

log = logging.getLogger("market_edge.nse")

BHAV_URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv"
NIFTY500_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"

COLS = {
    "SYMBOL": "symbol", "SERIES": "series", "DATE1": "date", "PREV_CLOSE": "prev_close",
    "OPEN_PRICE": "open", "HIGH_PRICE": "high", "LOW_PRICE": "low", "LAST_PRICE": "last",
    "CLOSE_PRICE": "close", "AVG_PRICE": "avg_price", "TTL_TRD_QNTY": "volume",
    "TURNOVER_LACS": "turnover_lacs", "NO_OF_TRADES": "trades", "DELIV_QTY": "deliv_qty",
    "DELIV_PER": "deliv_pct",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily (
    symbol TEXT NOT NULL, series TEXT NOT NULL, date TEXT NOT NULL,
    prev_close REAL, open REAL, high REAL, low REAL, last REAL, close REAL, avg_price REAL,
    volume INTEGER, turnover_lacs REAL, trades INTEGER, deliv_qty INTEGER, deliv_pct REAL,
    PRIMARY KEY (symbol, series, date)
);
CREATE INDEX IF NOT EXISTS ix_daily_date ON daily(date);
CREATE TABLE IF NOT EXISTS companies (
    symbol TEXT PRIMARY KEY, name TEXT, industry TEXT, isin TEXT, in_nifty500 INTEGER, is_equity INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS fetch_log (
    file_date TEXT PRIMARY KEY, status TEXT, trade_date TEXT, fetched_at TEXT
);
"""


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.DB_PATH)
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(companies)")}
    if "is_equity" not in cols:  # databases created before this column existed
        con.execute("ALTER TABLE companies ADD COLUMN is_equity INTEGER DEFAULT 0")
    return con


def parse_bhav(raw: bytes) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(raw), skipinitialspace=True, dtype=str)
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns=COLS)[list(COLS.values())]
    for c in df.columns:
        df[c] = df[c].str.strip()
    df["date"] = pd.to_datetime(df["date"], format="%d-%b-%Y").dt.strftime("%Y-%m-%d")
    num = [c for c in df.columns if c not in ("symbol", "series", "date")]
    df[num] = df[num].apply(pd.to_numeric, errors="coerce")  # '-' -> NaN (no delivery data)
    return df


def fetch_day(d: date, use_cache: bool = True) -> pd.DataFrame | None:
    """Return the parsed file published for date d, or None if there is none (holiday/weekend)."""
    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    cache = config.RAW_DIR / f"sec_bhavdata_full_{d:%Y%m%d}.csv"
    if use_cache and cache.exists():
        raw = cache.read_bytes()
    else:
        try:
            raw = net.get(BHAV_URL.format(d=d))
        except net.HttpError as e:
            if e.status == 404:
                return None
            raise
        cache.write_bytes(raw)
    return parse_bhav(raw)


def store(con: sqlite3.Connection, df: pd.DataFrame) -> int:
    rows = df.where(pd.notna(df), None).itertuples(index=False, name=None)
    cur = con.executemany(
        f"INSERT OR REPLACE INTO daily ({','.join(COLS.values())}) VALUES ({','.join('?' * len(COLS))})",
        rows,
    )
    con.commit()
    return cur.rowcount


def update(days_back: int = 7, end: date | None = None, force: bool = False) -> list[str]:
    """Download any missing trading days in the window. Safe to re-run (idempotent).

    Weekends are skipped. Note: NSE sometimes serves the previous session's file for a
    non-trading day, so rows are keyed by the DATE1 column inside the file, not the URL date.
    """
    con = connect()
    end = end or datetime.now(ZoneInfo("Asia/Kolkata")).date()
    done = {r[0] for r in con.execute("SELECT file_date FROM fetch_log WHERE status IN ('ok','none')")}
    new_dates: list[str] = []
    d = end - timedelta(days=days_back)
    while d <= end:
        key = d.isoformat()
        is_recent = (end - d).days <= 1  # today's/yesterday's file may not be published yet
        if d.weekday() < 5 and (force or key not in done or is_recent):
            df = fetch_day(d, use_cache=not is_recent)
            if df is None or df.empty:
                status, trade_date = "none", None
            else:
                trade_date = df["date"].iloc[0]
                store(con, df)
                status = "ok"
                if trade_date not in new_dates:
                    new_dates.append(trade_date)
            if not (is_recent and status == "none"):
                con.execute("INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,?)",
                            (key, status, trade_date, datetime.now().isoformat(timespec="seconds")))
                con.commit()
            log.info("%s -> %s %s", key, status, trade_date or "")
        d += timedelta(days=1)
    con.close()
    return new_dates


EQUITY_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"


def update_companies() -> int:
    """Load NSE's list of listed equities (excludes ETFs/funds) and Nifty 500 industries."""
    eq = pd.read_csv(io.BytesIO(net.get(EQUITY_LIST_URL)), dtype=str)
    eq.columns = [c.strip() for c in eq.columns]
    con = connect()
    con.execute("UPDATE companies SET is_equity = 0")
    con.executemany(
        "INSERT INTO companies (symbol, name, isin, in_nifty500, is_equity) VALUES (?,?,?,0,1) "
        "ON CONFLICT(symbol) DO UPDATE SET is_equity=1, isin=excluded.isin",
        eq[["SYMBOL", "NAME OF COMPANY", "ISIN NUMBER"]].apply(lambda c: c.str.strip()).itertuples(index=False, name=None))
    con.commit()
    con.close()
    raw = net.get(NIFTY500_URL)
    df = pd.read_csv(io.BytesIO(raw))
    con = connect()
    con.execute("UPDATE companies SET in_nifty500 = 0")
    con.executemany(
        "INSERT INTO companies (symbol, name, industry, isin, in_nifty500) VALUES (?,?,?,?,1) "
        "ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, industry=excluded.industry, "
        "isin=excluded.isin, in_nifty500=1",
        df[["Symbol", "Company Name", "Industry", "ISIN Code"]].itertuples(index=False, name=None),
    )
    con.commit()
    con.close()
    return len(df)
