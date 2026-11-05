"""Cards render real PNGs from realistic and empty data, and Telegram sending falls back to text."""
import struct

import pytest

from market_edge import cards, config, notify

PNG = b"\x89PNG\r\n\x1a\n"


def _size(png):
    return struct.unpack(">II", png[16:24])


def _price(key, group, name, v, **kw):
    return {"key": key, "group": group, "name": name, "price": 100.0, "chg_1d_pct": v, "chg_1d_abs": v, "kind": "pct", **kw}


PRICES = {"generated_at_ist": "2026-11-05 09:00", "alerts": ["S&P 500 +1.8% to 7,703.43", "🔴 Brent -7.8%"],
          "india_read": ["🔴 Crude +3%: negative for India (import bill, INR). Watch OMCs, paints, aviation, tyres.", "⚪ calm"],
          "prices": [_price("NIFTY", "India", "Nifty 50", 1.2), _price("SPX", "US", "S&P 500", -0.4, stale=True),
                     _price("US10Y", "Bonds", "US 10Y yield %", -0.05, kind="bp", chg_1d_abs=-0.05),
                     _price("BRENT", "Energy", "Brent crude", -8.0, verify=True), {"key": "X", "group": "Asia", "name": "No data"}]}


def test_prices_card_renders():
    png = cards.prices_card(PRICES)
    assert png.startswith(PNG) and _size(png)[0] == 1274 and 1000 < _size(png)[1] < 4000


def test_all_cards_render_with_data_and_empty():
    news = [{"title": "RBI cuts repo rate by 25 bps; banks and realty rally on the surprise move, analysts expect more easing",
             "importance": 9, "nifty": 1, "category": "RBI & Monetary", "source": "RBI"}]
    mood = {"score": 7.2, "label": "Risk-on", "news_component": 1.0, "prices_component": 1.2, "flows_component": 0.5}
    flows = {"date": "2026-11-04", "sessions": 5, "FII": {"net": 1200.0, "net_period": 3000.0, "streak": 3},
             "DII": {"net": -400.0, "net_period": 100.0, "streak": -2}}
    evs = [{"when": "TODAY", "time_ist": "19:00", "name": "US jobs report (NFP)", "importance": 8, "approx": False}]
    for imgs in (cards.brief_cards(news, mood, flows, [("Banks", 1.2), ("IT", -0.6)], evs, "Thu 5 Nov 2026"),
                 cards.brief_cards([], {**mood, "label": "Neutral", "score": 5.0}, None, [], [], "")):
        assert len(imgs) == 2 and all(i.startswith(PNG) for i in imgs)
    pick = {"symbol": "ABC", "setup": "Volume breakdown · 52W low", "direction": "bearish", "trigger": 100.0, "stop": 102.0,
            "target": 96.0, "risk_pct": 2.0, "wide_stop": True, "reasons": ["results day"],
            "history": {"n": 900, "intraday_pct": 0.5, "quarters_positive": 4, "quarters": 4}}
    assert cards.watchlist_card([pick], "2026-11-04").startswith(PNG)
    assert cards.watchlist_card([], "2026-11-04").startswith(PNG)
    row = {"symbol": "XYZ", "setup": "Accumulation", "close": 100.0, "chg_pct": 2.0, "vol_x": 3.1, "deliv_pct": 60.0,
           "trigger": 101.0, "stop": 99.0, "target": 105.0, "wide_stop": False, "catalysts": ["Results filed"]}
    rep = {"scan_date": "2026-11-04", "flagged": 5, "universe": 2000, "breadth": {"advances": 1200, "declines": 700},
           "bullish": [row], "bearish": [{**row, "chg_pct": -2.0}]}
    assert all(i.startswith(PNG) for i in cards.scan_cards(rep))
    assert all(i.startswith(PNG) for i in cards.scan_cards({**rep, "bullish": [], "bearish": []}))
    deal = {"symbol": "ABC", "client": "BIG FUND", "net_cr": 12.5, "avg_price": 100.0, "pct_of_avg_turnover": float("nan"),
            "institutional": True}
    assert cards.corporate_card(flows, [{"symbol": "ABC", "label": "Order win", "bias": 1, "amount_cr": 1250.0}],
                                ["AAA", "BBB"], [deal], "2026-11-04").startswith(PNG)
    assert cards.corporate_card(None, [], [], [], "").startswith(PNG)


def test_emoji_stripped_and_dot_colour():
    assert cards.strip_emoji("🔴 Crude  +3% ⚠️ now") == "Crude +3% now"
    assert cards.lead_color("🟢 ok") == cards.C["up"] and cards.lead_color("plain") == cards.C["muted"]


def test_telegram_sends_images_then_falls_back_to_text(monkeypatch):
    for k, v in (("TELEGRAM_BOT_TOKEN", "t"), ("TELEGRAM_CHAT_ID", "1"), ("WHATSAPP_PHONE", None), ("WHATSAPP_APIKEY", None)):
        monkeypatch.setattr(config, k, v)
    monkeypatch.setattr(notify.time, "sleep", lambda s: None)
    photos, texts = [], []
    monkeypatch.setattr(notify, "_tg_photo", lambda png, cap=None: photos.append((png, cap)) or (True, ""))
    monkeypatch.setattr(notify, "_post_telegram", lambda t: texts.append(t) or True)
    assert notify.send("<b>Title &amp; more</b>\nbody", images=[b"a", b"b"]) is True
    assert [p[1] for p in photos] == ["Title & more", None] and texts == []

    photos.clear()
    results = iter([(True, ""), (False, "Bad Request: wrong file")])
    monkeypatch.setattr(notify, "_tg_photo", lambda png, cap=None: photos.append(png) or next(results))
    assert notify.send("hello", images=[b"a", b"b"]) is True
    assert texts == ["hello"]                                    # an image failed: text version sent


def test_whatsapp_gets_text_only_and_no_channel_saves_cards(monkeypatch, tmp_path, capsys):
    got = []
    for k, v in (("TELEGRAM_BOT_TOKEN", None), ("TELEGRAM_CHAT_ID", None), ("WHATSAPP_PHONE", "+91"), ("WHATSAPP_APIKEY", "k")):
        monkeypatch.setattr(config, k, v)
    monkeypatch.setattr(notify, "_get_whatsapp", lambda t: got.append(t) or True)
    assert notify.send("txt", images=[b"x"]) is True and got == ["txt"]
    monkeypatch.setattr(config, "WHATSAPP_PHONE", None)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "REPORT_DIR", tmp_path / "reports")
    notify.send("plain", images=[b"\x89PNG"])
    assert "plain" in capsys.readouterr().out and len(list((tmp_path / "reports" / "cards").glob("*.png"))) == 1


def test_multipart_body_contains_file_and_fields():
    body, ctype = notify._multipart({"chat_id": "5", "caption": "hi"}, "photo", "card.png", b"PNGDATA")
    boundary = ctype.split("boundary=")[1].encode()
    assert body.count(b"--" + boundary) == 4 and b'name="chat_id"\r\n\r\n5' in body and b"PNGDATA" in body
