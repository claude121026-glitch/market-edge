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
import urllib.error
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


_TAG = re.compile(r"(</?(?:b|i)>)")
_AMP = re.compile(r"&(?!#?\w+;)")          # an & that is not already part of an entity


def to_telegram_html(text: str) -> str:
    """Our messages use only <b> and <i>. Escape everything else so Telegram's HTML parser accepts it
    (e.g. 'S&P 500', 'yield < 4%'), without double-escaping text that already holds entities."""
    out = []
    for piece in _TAG.split(text):
        if _TAG.fullmatch(piece):
            out.append(piece)
        else:
            out.append(_AMP.sub("&amp;", piece).replace("<", "&lt;").replace(">", "&gt;"))
    return "".join(out)


def _tg_request(text: str, parse_mode: str | None) -> tuple[bool, str]:
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    fields = {"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": "true"}
    if parse_mode:
        fields["parse_mode"] = parse_mode
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode()),
                                    timeout=20) as r:
            return bool(json.loads(r.read()).get("ok", False)), ""
    except urllib.error.HTTPError as e:           # Telegram explains itself in the response body
        try:
            return False, json.loads(e.read()).get("description", f"HTTP {e.code}")
        except Exception:
            return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)


def _post_telegram(text: str) -> bool:
    ok = True
    for part in _chunks(text, TELEGRAM_LIMIT):
        sent, why = _tg_request(to_telegram_html(part), "HTML")
        if not sent and "parse entities" in why:     # formatting problem: send it as plain text instead
            log.warning("Telegram rejected the formatting (%s); resending as plain text", why)
            plain = html.unescape(re.sub(r"<[^>]+>", "", part))
            sent, why = _tg_request(plain, None)
        if not sent:
            log.error("Telegram send failed: %s", why)   # e.g. 'chat not found', 'Unauthorized'
        ok &= sent
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


# --------------------------------------------------------------------------- #
# Diagnostics (safe to run in public CI logs: prints yes/no results and lengths, never secrets)
# --------------------------------------------------------------------------- #
def _tg_api(method: str) -> dict:
    url = f"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN', '').strip()}/{method}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"ok": False, "description": f"HTTP {e.code}"}
    except Exception as e:                      # never include the exception text: it can hold the URL
        return {"ok": False, "description": type(e).__name__}


def diagnose_telegram(api=_tg_api) -> list[str]:
    raw_chat = os.getenv("TELEGRAM_CHAT_ID", "")
    raw_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    out = []
    if not raw_token.strip():
        return ["TELEGRAM_BOT_TOKEN secret is empty or missing."]
    out.append(f"TELEGRAM_BOT_TOKEN: set, {len(raw_token.strip())} chars"
               + (" (had stray spaces/newline, now trimmed)" if raw_token != raw_token.strip() else ""))
    out.append(f"TELEGRAM_CHAT_ID: {'set' if raw_chat.strip() else 'EMPTY'}, {len(raw_chat.strip())} chars, "
               f"{'digits only' if raw_chat.strip().lstrip('-').isdigit() else 'NOT a plain number'}"
               + (" (had stray spaces/newline, now trimmed)" if raw_chat != raw_chat.strip() else ""))
    me = api("getMe")
    if not me.get("ok"):
        return out + [f"Token rejected by Telegram: {me.get('description')}. Check TELEGRAM_BOT_TOKEN."]
    out.append(f"Token OK: this is bot @{me['result'].get('username')}  <- must be the bot you pressed Start on")
    if raw_chat.strip() == str(me["result"]["id"]):
        return out + ["TELEGRAM_CHAT_ID is the BOT's own id (the digits at the start of the token). "
                      "Use the number after \"chat\":{\"id\": from getUpdates instead."]
    up = api("getUpdates")
    if not up.get("ok"):
        return out + [f"Couldn't read messages sent to the bot: {up.get('description')}"]
    chats = set()
    for u in up.get("result", []):
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            if key in u and "chat" in u[key]:
                chats.add(str(u[key]["chat"]["id"]))
    if not chats:
        return out + ["The bot has received NO messages in the last 24 hours. In Telegram open this bot, press Start, "
                      "send 'hi', then run this check again."]
    if raw_chat.strip() in chats:
        out.append(f"OK: TELEGRAM_CHAT_ID matches one of the {len(chats)} chat(s) that messaged this bot.")
    else:
        out.append(f"MISMATCH: TELEGRAM_CHAT_ID matches none of the {len(chats)} chat(s) that messaged this bot. "
                   f"Saved value has {len(raw_chat.strip())} chars; the chat id(s) seen have "
                   f"{sorted(len(c) for c in chats)} chars. Re-copy the number after \"chat\":{{\"id\": from getUpdates "
                   f"for THIS bot and update the secret.")
    return out
