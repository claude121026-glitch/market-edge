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
