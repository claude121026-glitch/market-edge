"""Send messages to WhatsApp and/or Telegram. With neither configured, messages are printed.

WhatsApp uses CallMeBot (free, personal use): https://www.callmebot.com/blog/free-api-whatsapp-messages/
  1. Get CallMeBot's current WhatsApp number from the page above and save it in your contacts.
  2. Send it the activation message shown on that page (e.g. "I allow callmebot to send me messages").
  3. You get an API key back. Set WHATSAPP_PHONE (e.g. +919876543210) and WHATSAPP_APIKEY.
Messages pass through CallMeBot's servers; use Telegram if you prefer not to.
"""
import html
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request

from . import config

log = logging.getLogger("market_edge.notify")
TELEGRAM_LIMIT = 4000
WHATSAPP_LIMIT = 1500          # keep each WhatsApp message short; long reports are split
WHATSAPP_GAP_SEC = 4           # be gentle with the free service


def _chunks(text: str, limit: int):
    buf = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:             # a single very long line
            yield line[:limit]
            line = line[limit:]
        if len(buf) + len(line) > limit:
            yield buf
            buf = ""
        buf += line
    if buf.strip():
        yield buf


def to_whatsapp(text: str) -> str:
    """Our messages are written in Telegram HTML; WhatsApp uses *bold* and _italic_."""
    t = re.sub(r"</?b>", "*", text)
    t = re.sub(r"</?i>", "_", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = t.replace("**", "")                  # empty bold from adjacent tags
    return html.unescape(t)


def _post_telegram(text: str) -> bool:
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    ok = True
    for part in _chunks(text, TELEGRAM_LIMIT):
        data = urllib.parse.urlencode({"chat_id": config.TELEGRAM_CHAT_ID, "text": part,
                                       "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
                ok &= json.loads(r.read()).get("ok", False)
        except Exception as e:
            log.error("Telegram send failed: %s", e)
            ok = False
    return ok


def _get_whatsapp(text: str) -> bool:
    ok = True
    for i, part in enumerate(_chunks(to_whatsapp(text), WHATSAPP_LIMIT)):
        if i:
            time.sleep(WHATSAPP_GAP_SEC)
        q = urllib.parse.urlencode({"phone": config.WHATSAPP_PHONE, "text": part, "apikey": config.WHATSAPP_APIKEY})
        try:
            with urllib.request.urlopen(f"https://api.callmebot.com/whatsapp.php?{q}", timeout=30) as r:
                body = r.read().decode("utf-8", "replace").lower()
                if r.status != 200 or "error" in body:
                    log.error("WhatsApp send failed: %s", body[:200])
                    ok = False
        except Exception as e:
            log.error("WhatsApp send failed: %s", e)
            ok = False
    return ok


def send(text: str) -> bool:
    """Send to every configured channel. Never raises: a failed alert must not stop a job."""
    sent = False
    if config.WHATSAPP_PHONE and config.WHATSAPP_APIKEY:
        sent |= _get_whatsapp(text)
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        sent |= _post_telegram(text)
    if not (config.WHATSAPP_PHONE and config.WHATSAPP_APIKEY) and not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        print(text)
    if os.getenv("ME_ECHO"):
        print(text)
    return sent
