"""Offline tests for step 4 (announcements, deals, FII/DII)."""
import sqlite3

import pytest

from market_edge import config, corporate as c, macro_news


@pytest.mark.parametrize("typ,text,expected", [
    ("Resignation", "Resignation of Mr. X as Global Chief Executive Officer and Whole-time Director", "Top management exit"),
    ("Resignation", "Resignation of Mr. Y as Non- Executive Director", None),
    ("Resignation of Statutory Auditor", "auditor resigned", "Auditor resigned"),
    ("Amalgamation/Merger", "merger of A Pvt Ltd with B Pvt Ltd wholly owned subsidiaries of the Company", None),
    ("Amalgamation/Merger", "Amalgamation of XYZ Ltd with the Company", "Merger / demerger"),
    ("Credit Rating", "CRISIL has reaffirmed the rating", None),
    ("Credit Rating", "ICRA has upgraded the long-term rating", "Rating upgrade"),
    ("Press Release", "Company bags order worth Rs 1,250 crore from NHAI", "Order win"),
    ("General Updates", "Company has defaulted on payment of interest on NCDs", "Default / fraud disclosure"),
    ("Outcome of Board Meeting", "approved the financial results for the quarter", "Results filed"),
    ("Outcome of Board Meeting", "appointment of company secretary", None),
])
def test_classify(typ, text, expected):
    got = c.classify(typ, text)
    assert (got[0] if got else None) == expected


def test_amount_extraction():
    assert c.amount_cr("orders worth Rs 1,250 crore and Rs 50 cr") == 1250
    assert c.amount_cr("INR 500 million contract") == 50
    assert c.amount_cr("₹75 lakh") == 0.75
    assert c.amount_cr("no amount") is None


BULK = b"""Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price,Remarks
04-NOV-2026,ABC,Abc Ltd,BIG MUTUAL FUND,BUY,1000000,200.00,-
04-NOV-2026,ABC,Abc Ltd,FAST HFT LLP,BUY,500000,201.00,-
04-NOV-2026,ABC,Abc Ltd,FAST HFT LLP,SELL,500000,201.50,-
"""


def test_deals_net_out_churn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    con = c.connect()
    con.executemany("INSERT OR IGNORE INTO deals VALUES (?,?,?,?,?,?,?,?)",
                    c.parse_deals(BULK, "bulk").itertuples(index=False, name=None))
    con.commit()
    out = c.deal_summary("2026-11-04")
    assert [d["client"] for d in out] == ["BIG MUTUAL FUND"]       # HFT buy+sell netted away
    assert out[0]["net_cr"] == 20.0 and out[0]["institutional"]


def test_fii_dii_summary_and_score(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    con = c.connect()
    con.executemany("INSERT INTO fii_dii VALUES (?,?,?,?,?)", [
        ("2026-11-03", "FII", 0, 0, 1000.0), ("2026-11-04", "FII", 0, 0, 7284.03),
        ("2026-11-03", "DII", 0, 0, 500.0), ("2026-11-04", "DII", 0, 0, -3712.23)])
    con.commit()
    s = c.fii_dii_summary()
    assert s["FII"]["streak"] == 2 and round(s["FII"]["net_period"]) == 8284 and s["DII"]["streak"] == -1
    assert "2-day buying streak" in c.format_fii_dii(s)
    assert macro_news.macro_score([], None, s)["flows_component"] == 0.5


def test_no_records_placeholder_is_empty():
    raw = b"Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price\nNO RECORDS,,,,,,\n"
    assert c.parse_deals(raw, "block").empty
