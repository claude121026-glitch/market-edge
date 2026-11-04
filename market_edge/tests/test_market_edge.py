"""Offline tests (no network).   Run from /home/user:  python -m pytest -q market_edge/tests"""
from pathlib import Path

import pandas as pd

from market_edge import evening_scan, global_prices, nse

FIX = Path(__file__).parent / "fixtures"


def test_parse_bhav_real_header_with_spaces():
    df = nse.parse_bhav((FIX / "bhav_sample.csv").read_bytes())
    assert list(df.columns)[:3] == ["symbol", "series", "date"]
    assert df["date"].iloc[0] == "2026-11-04" and df["close"].dtype.kind == "f"


def _history(symbol, n=30, vol=1000, deliv_pct=30.0, last=None):
    rows = []
    for i in range(n):
        c = 100 + i * 0.1
        rows.append(dict(symbol=symbol, series="EQ", date=f"2026-01-{i + 1:02d}" if i < 31 else None,
                         prev_close=c - 0.1, open=c, high=c + 1, low=c - 1, close=c, volume=vol,
                         turnover_lacs=2000.0, deliv_qty=vol * deliv_pct / 100, deliv_pct=deliv_pct))
    if last:
        rows[-1].update(last)
    return rows


def test_volume_spike_with_low_delivery_is_not_accumulation():
    last = dict(volume=10000, deliv_qty=1000, deliv_pct=10.0, close=110, high=111, low=103, prev_close=102.9)
    df = pd.DataFrame(_history("CHURN", last=last))
    r = evening_scan.compute_signals(df).iloc[0]
    assert r["VOL_SPIKE"] and not r["DELIV_SPIKE"] and r["setup"].startswith("Volume breakout")
    assert r["trigger"] == 111 and r["target"] > r["trigger"] > r["stop"]


def test_high_delivery_spike_is_accumulation():
    last = dict(volume=5000, deliv_qty=4000, deliv_pct=80.0, close=104, high=104.5, low=102, prev_close=102.8)
    df = pd.DataFrame(_history("ACCUM", last=last))
    r = evening_scan.compute_signals(df).iloc[0]
    assert r["DELIV_SPIKE"] and r["HIGH_DELIV"] and r["setup"].startswith("Accumulation")


def test_illiquid_stocks_filtered():
    df = pd.DataFrame(_history("TINY", last=dict(volume=10000)))
    df["turnover_lacs"] = 10.0  # Rs 0.1 crore/day
    assert evening_scan.compute_signals(df).empty


def _chart(closes, price):
    return {"chart": {"result": [{"meta": {"regularMarketPrice": price, "currency": "USD"},
                                  "indicators": {"quote": [{"close": closes}]}}]}}


def test_parse_chart_changes():
    d = global_prices.parse_chart(_chart([100, None, 101, 102, 103, 104, 105, 110], 110))
    assert round(d["chg_1d_pct"], 2) == round((110 / 105 - 1) * 100, 2)
    assert round(d["chg_1m_pct"], 1) == 10.0


def test_india_read_rules():
    p = {"BRENT": {"chg_1d_pct": 3.0}, "DXY": {"chg_1d_pct": 0.8}, "VIX": {"price": 30}}
    txt = " ".join(global_prices.india_read(p))
    assert "Crude +3.0%" in txt and "FII outflow" in txt and "risk-off" in txt
    p["BRENT"]["verify"] = True
    assert "confirm on another source" in " ".join(global_prices.india_read(p))
