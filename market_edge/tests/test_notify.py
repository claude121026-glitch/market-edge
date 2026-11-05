"""Offline tests for notifications (no network)."""
from market_edge import config, notify


def test_whatsapp_formatting():
    t = notify.to_whatsapp("<b>📊 Scan</b> R&amp;D <i>note</i> <b></b>x")
    assert t == "*📊 Scan* R&D _note_ x"


def test_chunks_respect_limit():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    parts = list(notify._chunks(text, 500))
    assert all(len(p) <= 500 for p in parts) and "".join(parts) == text


def test_send_routes_to_whatsapp(monkeypatch):
    calls = []
    monkeypatch.setattr(config, "WHATSAPP_PHONE", "+910000000000")
    monkeypatch.setattr(config, "WHATSAPP_APIKEY", "k")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", None)
    monkeypatch.setattr(notify, "_get_whatsapp", lambda text: calls.append(text) or True)
    assert notify.send("<b>hi</b>") is True and calls == ["<b>hi</b>"]


def test_send_prints_without_channels(monkeypatch, capsys):
    for k in ("WHATSAPP_PHONE", "WHATSAPP_APIKEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.setattr(config, k, None)
    assert notify.send("plain") is False and "plain" in capsys.readouterr().out


def test_telegram_html_escaping():
    assert notify.to_telegram_html("<b>S&P 500</b> yield < 4% R&amp;D <i>x</i>") == \
        "<b>S&amp;P 500</b> yield &lt; 4% R&amp;D <i>x</i>"


def test_telegram_falls_back_to_plain_text_and_reports_reason(monkeypatch):
    sent = []
    def fake(text, mode):
        sent.append((text, mode))
        return (False, "Bad Request: can't parse entities: x") if mode == "HTML" else (True, "")
    monkeypatch.setattr(notify, "_tg_request", fake)
    assert notify._post_telegram("<b>S&P</b> ok") is True
    assert sent[0][1] == "HTML" and sent[1] == ("S&P ok", None)

    monkeypatch.setattr(notify, "_tg_request", lambda text, mode: (False, "Bad Request: chat not found"))
    assert notify._post_telegram("hello") is False


def _fake_api(me_id=555, updates=()):
    def api(method):
        if method == "getMe":
            return {"ok": True, "result": {"id": me_id, "username": "my_bot"}}
        return {"ok": True, "result": [{"message": {"chat": {"id": c}}} for c in updates]}
    return api


def test_diagnose_telegram_cases(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "555:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", " 987654321\n")
    ok = "\n".join(notify.diagnose_telegram(_fake_api(updates=[987654321])))
    assert "@my_bot" in ok and "OK: TELEGRAM_CHAT_ID matches" in ok and "stray spaces" in ok
    assert "987654321" not in ok and "555:abc" not in ok                       # never prints secrets
    assert "MISMATCH" in "\n".join(notify.diagnose_telegram(_fake_api(updates=[111222333])))
    assert "NO messages" in "\n".join(notify.diagnose_telegram(_fake_api(updates=[])))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    assert "BOT's own id" in "\n".join(notify.diagnose_telegram(_fake_api()))


def test_secrets_are_trimmed(monkeypatch):
    import importlib
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "  123 \n")
    assert importlib.reload(config).TELEGRAM_CHAT_ID == "123"


def _payload(**chg):
    rows = [{"key": k, "name": k, "price": 100.0, "chg_1d_pct": v, "chg_1d_abs": v, "stale": False}
            for k, v in chg.items()]
    return {"prices": rows}


def test_price_alerts_are_not_repeated(tmp_path, monkeypatch):
    from market_edge import global_prices as gp
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    day = "2026-11-05"
    assert len(gp.new_alerts(_payload(BRENT=3.5), day)) == 1          # first time over the 3% threshold
    assert gp.new_alerts(_payload(BRENT=3.6), day) == []              # 30 min later: same alert, stay quiet
    assert len(gp.new_alerts(_payload(BRENT=6.4), day)) == 1          # doubled: alert again
    assert len(gp.new_alerts(_payload(BRENT=3.5), "2026-11-06")) == 1  # new day resets
    assert gp.new_alerts(_payload(GOLD=0.5), day) == []               # below its threshold
    stale = _payload(BRENT=9.0); stale["prices"][0]["stale"] = True
    assert gp.new_alerts(stale, "2026-11-07") == []                    # stale values never alert
