"""Tiny polite HTTP helper: one shared pacing gap, retries with backoff on 429/5xx."""
import time
import urllib.error
import urllib.request

from . import config

_last_call = 0.0


class HttpError(Exception):
    def __init__(self, status, url):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status


def get(url: str, timeout: float = 30, retries: int = 3, backoff=(5, 15, 30)) -> bytes:
    global _last_call
    for attempt in range(retries + 1):
        wait = config.REQUEST_GAP_SEC - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(backoff[min(attempt, len(backoff) - 1)])
                continue
            raise HttpError(e.code, url) from None
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries:
                time.sleep(backoff[min(attempt, len(backoff) - 1)])
                continue
            raise
    raise RuntimeError("unreachable")
