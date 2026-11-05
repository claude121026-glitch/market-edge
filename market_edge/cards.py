"""Dashboard-style PNG cards for Telegram. Pure matplotlib (no browser), 1080 logical px wide.

Each public function returns PNG bytes (or a list of them). Cards draw only what the data holds,
use colour plus arrows (never colour alone), and fit their text to the space available.
"""
from __future__ import annotations

import io
import re

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyBboxPatch, Polygon, Rectangle  # noqa: E402

W, PAD, DPI = 1080, 36, 118
C = dict(bg="#0e1621", panel="#16212f", panel2="#1d2b3d", line="#2a3b52", text="#eaf0f7", muted="#93a4b8",
         up="#34c17a", down="#f0555a", accent="#5aa2ff", amber="#f4b13f", grey="#6b7c90")
_EMOJI = re.compile("[\U0001F000-\U0001FFFF☀-➿️‍]")
_DOT = {"🔴": "down", "🟢": "up", "🟠": "amber", "🟡": "amber", "⚪": "grey", "⚠": "amber"}


def strip_emoji(s: str) -> str:
    return re.sub(r"\s+", " ", _EMOJI.sub("", s or "")).strip()


def lead_color(s: str) -> str:
    for ch, key in _DOT.items():
        if (s or "").lstrip().startswith(ch):
            return C[key]
    return C["muted"]


def money(v: float | None, dec: int = 2) -> str:
    return "n/a" if v is None else f"{v:,.{dec}f}"


def price(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 10000 else f"{v:,.2f}"


class Canvas:
    def __init__(self, height: float):
        self.h = int(height)
        self.fig = plt.figure(figsize=(W / 100, self.h / 100), dpi=DPI, facecolor=C["bg"])
        self.ax = self.fig.add_axes([0, 0, 1, 1])
        self.ax.set_xlim(0, W)
        self.ax.set_ylim(self.h, 0)
        self.ax.axis("off")
        self._r = self.fig.canvas.get_renderer()

    # --- measuring -------------------------------------------------------- #
    def width(self, s: str, px: float, bold: bool = False) -> float:
        t = self.ax.text(0, 0, s, fontsize=px * 0.72, fontweight="bold" if bold else "normal")
        w = t.get_window_extent(self._r).width / (DPI / 100)
        t.remove()
        return w

    def fit(self, s: str, px: float, max_w: float, bold: bool = False) -> str:
        if self.width(s, px, bold) <= max_w:
            return s
        lo, hi = 0, len(s)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.width(s[:mid].rstrip() + "…", px, bold) <= max_w:
                lo = mid
            else:
                hi = mid - 1
        return s[:lo].rstrip() + "…"

    def wrap(self, s: str, px: float, max_w: float, max_lines: int = 2, bold: bool = False) -> list[str]:
        lines, cur = [], ""
        for word in s.split():
            trial = f"{cur} {word}".strip()
            if self.width(trial, px, bold) <= max_w or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = self.fit(lines[-1] + " …", px, max_w, bold)
        return [self.fit(l, px, max_w, bold) for l in lines]

    # --- drawing ---------------------------------------------------------- #
    def box(self, x, y, w, h, fc=None, ec=None, r=14, lw=1.0):
        self.ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={r}",
                                         fc=fc or C["panel"], ec=ec or "none", lw=lw))

    def text(self, x, y, s, px=24, color=None, bold=False, ha="left", va="top"):
        self.ax.text(x, y, s, fontsize=px * 0.72, color=color or C["text"], fontweight="bold" if bold else "normal",
                     ha=ha, va=va, clip_on=False)

    def dot(self, x, y, color, r=7):
        self.ax.add_patch(plt.Circle((x, y), r, fc=color, ec="none"))

    def pill(self, x, y, label, fg, bg=None, px=20, bold=True, pad=12, h=32, ha="left"):
        w = self.width(label, px, bold) + 2 * pad
        x0 = x - w if ha == "right" else x
        self.box(x0, y, w, h, fc=bg or C["panel2"], r=h / 2)
        self.text(x0 + w / 2, y + h / 2, label, px, fg, bold, ha="center", va="center")
        return w

    def hline(self, y, x1=PAD, x2=W - PAD, color=None):
        self.ax.add_line(plt.Line2D([x1, x2], [y, y], color=color or C["line"], lw=1))

    def png(self) -> bytes:
        buf = io.BytesIO()
        self.fig.savefig(buf, format="png", dpi=DPI, facecolor=C["bg"])
        plt.close(self.fig)
        return buf.getvalue()


def _header(c: Canvas, title: str, right: str = "", sub: str = "") -> float:
    c.text(PAD, 30, title, 40, bold=True)
    if right:
        c.text(W - PAD, 44, right, 22, C["muted"], ha="right")
    if sub:
        c.text(PAD, 80, sub, 22, C["muted"])
    c.hline(116)
    return 134


def _footer(c: Canvas, y: float, note: str = "Market Edge · screening output, not investment advice") -> None:
    c.text(PAD, y, note, 18, C["grey"])


def _arrow(v: float) -> str:
    return "▲" if v > 0 else "▼" if v < 0 else "–"


def _dir_color(v: float) -> str:
    return C["up"] if v > 0 else C["down"] if v < 0 else C["muted"]


# --------------------------------------------------------------------------- #
# 1. Global markets board
# --------------------------------------------------------------------------- #
_GROUPS = [("INDIA", ["India"]), ("US", ["US"]), ("ASIA", ["Asia"]), ("EUROPE", ["Europe"]),
           ("CURRENCIES", ["Currencies"]), ("RATES & ENERGY", ["Bonds", "Energy"]),
           ("METALS & CRYPTO", ["Metals", "Crypto"])]
TILE_H, TILE_GAP, COLS = 112, 14, 4


def prices_card(p: dict) -> bytes:
    rows = [r for r in p["prices"] if "price" in r]
    groups = [(label, [r for r in rows if r["group"] in keys]) for label, keys in _GROUPS]
    groups = [(l, g) for l, g in groups if g]
    read = [x for x in p.get("india_read", []) if x]
    alerts = p.get("alerts", [])[:5]
    n_rows = sum(-(-len(g) // COLS) for _, g in groups)
    m = Canvas(10)                                         # measuring canvas: wrap text to know the real height
    wrapped = [m.wrap(strip_emoji(x), 24, W - 2 * PAD - 40, 2) for x in read]
    plt.close(m.fig)
    alert_h = (50 + len(alerts) * 34 + 14) if alerts else 0
    h = 134 + n_rows * (TILE_H + TILE_GAP) + len(groups) * 40 + 6 * len(groups)
    h += (alert_h + 24) if alerts else 0
    h += 38 + 24 + sum(30 * len(w) + 14 for w in wrapped) + 50
    c = Canvas(h)
    y = _header(c, "GLOBAL MARKETS", f"{p['generated_at_ist']} IST", "Change vs previous close · green up, red down")
    tw = (W - 2 * PAD - (COLS - 1) * TILE_GAP) / COLS
    for label, g in groups:
        c.text(PAD, y, label, 20, C["muted"], bold=True)
        y += 34
        for i, r in enumerate(g):
            col, row = i % COLS, i // COLS
            x0, y0 = PAD + col * (tw + TILE_GAP), y + row * (TILE_H + TILE_GAP)
            c.box(x0, y0, tw, TILE_H)
            kind_bp = r.get("kind") == "bp"
            v = r["chg_1d_abs"] if kind_bp else r["chg_1d_pct"]
            c.text(x0 + 16, y0 + 12, c.fit(r["name"], 20, tw - 32), 20, C["muted"])
            c.text(x0 + 16, y0 + 38, price(r["price"]), 34, bold=True)
            chg = f"{_arrow(v)} {abs(r['chg_1d_abs'] * 100):.0f} bp" if kind_bp else f"{_arrow(v)} {abs(v):.2f}%"
            c.text(x0 + 16, y0 + 80, chg, 25, _dir_color(v), bold=True)
            if r.get("stale") or r.get("verify"):
                c.text(x0 + tw - 14, y0 + 84, "stale" if r.get("stale") else "verify", 17, C["amber"], ha="right")
        y += -(-len(g) // COLS) * (TILE_H + TILE_GAP) + 6
    if alerts:
        c.box(PAD, y, W - 2 * PAD, alert_h, fc=C["panel"], ec=C["amber"], lw=1.2)
        c.text(PAD + 22, y + 14, "ALERTS", 20, C["amber"], bold=True)
        for i, a in enumerate(alerts):
            c.text(PAD + 22, y + 50 + i * 34, c.fit(strip_emoji(a), 23, W - 2 * PAD - 44), 23)
        y += alert_h + 24
    c.text(PAD, y, "WHAT IT MEANS FOR INDIA", 20, C["muted"], bold=True)
    y += 38
    for line, lines in zip(read, wrapped):
        col = lead_color(line)
        c.dot(PAD + 8, y + 14, col)
        for j, l in enumerate(lines):
            c.text(PAD + 32, y + j * 30, l, 24)
        y += 30 * len(lines) + 14
    _footer(c, c.h - 40)
    return c.png()


# --------------------------------------------------------------------------- #
# 2. Morning brief (two cards)
# --------------------------------------------------------------------------- #
_MOOD_SEGMENTS = [(0, 3.5, "#c8434a"), (3.5, 4.5, "#e28a43"), (4.5, 5.5, "#7d8b9b"), (5.5, 6.5, "#58b97f"), (6.5, 10, "#2fae6a")]


def _flow_tile(c: Canvas, x, y, w, label, d: dict | None, sessions: int) -> None:
    c.box(x, y, w, 118)
    c.text(x + 18, y + 12, label, 20, C["muted"], bold=True)
    if not d:
        c.text(x + 18, y + 48, "n/a", 34, C["muted"], bold=True)
        return
    c.text(x + 18, y + 40, f"{_arrow(d['net'])} ₹{abs(d['net']):,.0f} cr", 34, _dir_color(d["net"]), bold=True)
    sub = []
    if sessions > 1:
        sub.append(f"{sessions}d: {d['net_period']:+,.0f} cr")
    if abs(d.get("streak", 0)) >= 2:
        sub.append(f"{abs(d['streak'])}-day {'buying' if d['streak'] > 0 else 'selling'}")
    c.text(x + 18, y + 86, " · ".join(sub) or "net buying − selling, cash market", 20, C["muted"])


def brief_cards(news: list[dict], mood: dict, flows: dict | None, sectors: list, events: list, date_label: str = "") -> list[bytes]:
    top = news[:6]
    m = Canvas(10)
    tlines = [m.wrap(n["title"], 23, W - 2 * PAD - 190, 2) for n in top]
    plt.close(m.fig)
    row_hs = [max(84, 20 + 28 * len(t) + 30) for t in tlines]
    h1 = 134 + 200 + 150 + 56 + sum(r + 12 for r in row_hs) + 80
    c = Canvas(h1)
    y = _header(c, "MORNING BRIEF", date_label, "Macro mood, institutional flows, top news")
    c.text(PAD, y, "MACRO MOOD FOR INDIA", 20, C["muted"], bold=True)
    c.text(PAD, y + 32, f"{mood['score']:.1f}", 78, bold=True)
    num_w = c.width(f"{mood['score']:.1f}", 78, True)
    c.text(PAD + num_w + 12, y + 70, "/10", 28, C["muted"])
    c.pill(W - PAD, y + 50, mood["label"].upper(), "#0e1621", _mood_color(mood["score"]), px=24, h=44, ha="right")
    by, bx0, bw = y + 130, PAD, W - 2 * PAD
    for lo, hi, col in _MOOD_SEGMENTS:
        c.ax.add_patch(Rectangle((bx0 + bw * lo / 10, by), bw * (hi - lo) / 10 - 2, 18, fc=col, ec="none"))
    mx = bx0 + bw * max(0, min(10, mood["score"])) / 10
    c.ax.add_patch(Polygon([[mx - 11, by - 14], [mx + 11, by - 14], [mx, by - 1]], fc=C["text"], ec="none"))
    c.text(bx0, by + 26, "Risk-off", 18, C["muted"])
    c.text(bx0 + bw, by + 26, "Risk-on", 18, C["muted"], ha="right")
    comp = (f"News {mood['news_component']:+.1f}   ·   Global prices {mood['prices_component']:+.1f}   ·   "
            f"FII flows {mood.get('flows_component', 0):+.1f}")
    c.text(PAD, by + 56, comp, 21, C["muted"])
    y = by + 110
    half = (W - 2 * PAD - 14) / 2
    k = (flows or {}).get("sessions", 1)
    _flow_tile(c, PAD, y, half, f"FII CASH · {flows['date']}" if flows else "FII CASH", (flows or {}).get("FII"), k)
    _flow_tile(c, PAD + half + 14, y, half, "DII CASH", (flows or {}).get("DII"), k)
    y += 118 + 36
    c.text(PAD, y, "TOP NEWS", 20, C["muted"], bold=True)
    y += 40
    for n, lines, rh in zip(top, tlines, row_hs):
        c.box(PAD, y, W - 2 * PAD, rh)
        c.dot(PAD + 34, y + rh / 2, C["panel2"], r=22)
        c.text(PAD + 34, y + rh / 2, str(n["importance"]), 24, bold=True, ha="center", va="center")
        for j, l in enumerate(lines):
            c.text(PAD + 76, y + 10 + j * 28, l, 23)
        c.text(PAD + 76, y + 10 + 28 * len(lines) + 1, c.fit(f"{n['category']} · {n['source']}", 17, W - 2 * PAD - 190),
               17, C["muted"])
        col = _dir_color(n["nifty"])
        c.text(W - PAD - 20, y + rh / 2, f"{_arrow(n['nifty'])} Nifty" if n["nifty"] else "– Nifty", 21, col, bold=True,
               ha="right", va="center")
        y += rh + 12
    if not top:
        c.text(PAD, y, "No major macro news in the last 18 hours.", 24, C["muted"])
    _footer(c, c.h - 40)
    first = c.png()

    sv = sectors[:10]
    ev = events[:6]
    h2 = 134 + 56 + max(1, len(sv)) * 44 + 40 + 56 + max(1, len(ev)) * 50 + 70
    c = Canvas(h2)
    y = _header(c, "SECTORS & CALENDAR", date_label, "What the news helps or hurts, and what is coming up")
    c.text(PAD, y, "SECTOR TILT FROM NEWS", 20, C["muted"], bold=True)
    y += 40
    if sv:
        span = max(abs(v) for _, v in sv) or 1
        cx, half_w = 700, 300
        for name, v in sv:
            c.text(340, y + 4, c.fit(name, 22, 300), 22, ha="right")
            bw = abs(v) / span * (half_w - 50)
            col = C["up"] if v > 0 else C["down"]
            c.box(cx if v > 0 else cx - bw, y + 4, max(bw, 4), 28, fc=col, r=6)
            c.text(cx + bw + 12 if v > 0 else cx - bw - 12, y + 6, f"{v:+.1f}", 20, C["muted"], ha="left" if v > 0 else "right")
            y += 44
        c.ax.add_line(plt.Line2D([cx, cx], [y - len(sv) * 44 + 2, y], color=C["line"], lw=1.2))
        c.text(cx + 6, y + 6, "helped ▲", 18, C["up"])
        c.text(cx - 6, y + 6, "▼ hurt", 18, C["down"], ha="right")
    else:
        c.text(PAD, y, "No sector impact from news right now.", 24, C["muted"])
        y += 44
    y += 56
    c.text(PAD, y, "COMING UP (7 DAYS)", 20, C["muted"], bold=True)
    y += 40
    for e in ev:
        hot = e["importance"] >= 8
        c.dot(PAD + 10, y + 16, C["amber"] if hot else C["grey"])
        when = f"{e['when']}{' ' + e['time_ist'] if e.get('time_ist') else ''}"
        c.text(PAD + 32, y, when, 23, C["amber"] if hot else C["muted"], bold=True)
        c.text(PAD + 330, y, c.fit(e["name"] + (" (~approx)" if e.get("approx") else ""), 23, W - PAD - 330 - PAD), 23)
        y += 50
    if not ev:
        c.text(PAD, y, "No scheduled events in the next 7 days.", 24, C["muted"])
    _footer(c, c.h - 40, "Times in IST · ~approx = rule-based date, confirm near the day")
    return [first, c.png()]


def _mood_color(score: float) -> str:
    for lo, hi, col in _MOOD_SEGMENTS:
        if lo <= score <= hi:
            return col
    return C["grey"]


# --------------------------------------------------------------------------- #
# 3. Watchlist
# --------------------------------------------------------------------------- #
def verdict(e: dict | None) -> tuple[str, str]:
    if not e or e.get("n", 0) < 100:
        return "few past cases", C["muted"]
    if e["intraday_pct"] > 0.15 and e["quarters_positive"] >= 3:
        return f"held up {e['intraday_pct']:+.2f}%", C["up"]
    if e["intraday_pct"] < -0.1:
        return f"faded {e['intraday_pct']:+.2f}%", C["down"]
    return f"no clear edge {e['intraday_pct']:+.2f}%", C["muted"]


def watchlist_card(top: list[dict], scan_date: str) -> bytes:
    row_h = 124
    c = Canvas(134 + max(1, len(top)) * (row_h + 12) + 130)
    y = _header(c, "TODAY'S WATCHLIST", f"from {scan_date} close", "Intraday plan: enter on the break, stop at the pivot, target 2R")
    for i, p in enumerate(top, 1):
        long_ = p["direction"] == "bullish"
        col = C["up"] if long_ else C["down"]
        c.box(PAD, y, W - 2 * PAD, row_h)
        c.text(PAD + 22, y + 14, str(i), 22, C["muted"], bold=True)
        c.pill(PAD + 62, y + 12, "LONG" if long_ else "SHORT", "#0e1621", col, px=19, h=30)
        c.text(PAD + 170, y + 8, p["symbol"], 31, bold=True)
        sx = PAD + 170 + c.width(p["symbol"], 31, True) + 16
        c.text(sx, y + 16, c.fit(p["setup"], 21, W - PAD - 24 - sx, False), 21, C["muted"])
        side = "above" if long_ else "below"
        plan = f"Enter {side} {p['trigger']:,.2f}   ·   Stop {p['stop']:,.2f}   ·   Target {p['target']:,.2f}   ·   Risk {p['risk_pct']:.1f}%"
        c.text(PAD + 22, y + 56, c.fit(plan, 22, W - 2 * PAD - 44), 22)
        v, vcol = verdict(p.get("history"))
        c.text(PAD + 22, y + 90, f"History: {v}", 20, vcol, bold=True)
        extra = ", ".join(p.get("reasons") or [])
        if p.get("wide_stop"):
            extra = ("wide stop, " + extra).strip(", ")
        if extra:
            c.text(W - PAD - 22, y + 92, c.fit(extra, 19, 600), 19, C["amber"] if p.get("wide_stop") else C["muted"], ha="right")
        y += row_h + 12
    if not top:
        c.text(PAD, y, "No watchlist yet: it appears after the evening scan.", 24, C["muted"])
    c.text(PAD, y + 8, "Paper-trade only. The 1-year backtest found no proven edge after costs.", 20, C["amber"])
    _footer(c, c.h - 40)
    return c.png()


# --------------------------------------------------------------------------- #
# 4. Evening scan (two cards) and corporate card
# --------------------------------------------------------------------------- #
def _scan_rows(c: Canvas, y: float, rows: list[dict], row_h: int = 112) -> float:
    for r in rows:
        long_ = r["chg_pct"] > 0
        c.box(PAD, y, W - 2 * PAD, row_h - 10)
        c.text(PAD + 20, y + 10, r["symbol"], 29, bold=True)
        sx = PAD + 20 + c.width(r["symbol"], 29, True) + 14
        c.text(sx, y + 17, c.fit(r["setup"], 20, 500 - (sx - PAD), False), 20, C["muted"])
        c.text(W - PAD - 20, y + 8, f"₹{r['close']:,.2f}", 27, bold=True, ha="right")
        c.text(W - PAD - 20, y + 44, f"{_arrow(r['chg_pct'])} {abs(r['chg_pct']):.1f}%", 22, _dir_color(r["chg_pct"]), bold=True, ha="right")
        deliv = f"   ·   Delivery {r['deliv_pct']:.0f}%" if r.get("deliv_pct") is not None else ""
        c.text(PAD + 20, y + 48, f"Volume {r['vol_x']:.1f}x{deliv}", 20, C["muted"])
        side = "above" if long_ else "below"
        wide = "  (wide)" if r.get("wide_stop") else ""
        c.text(PAD + 20, y + 74, c.fit(f"Plan: {side} {r['trigger']:,.1f} · SL {r['stop']:,.1f} · Tgt {r['target']:,.1f}{wide}", 20,
                                         600), 20, C["amber"] if r.get("wide_stop") else C["text"])
        cats = ", ".join(r.get("catalysts") or [])
        if cats:
            c.text(W - PAD - 20, y + 74, c.fit(cats, 19, 330), 19, C["accent"], ha="right")
        y += row_h
    return y


def scan_cards(rep: dict) -> list[bytes]:
    bull, bear = rep["bullish"][:8], rep["bearish"][:6]
    b = rep["breadth"]
    total = max(1, b["advances"] + b["declines"] + b.get("unchanged", 0))
    h1 = 134 + 130 + 60 + max(1, len(bull)) * 112 + 56
    c = Canvas(h1)
    y = _header(c, "EVENING SCAN", rep["scan_date"], f"{rep['flagged']} of {rep['universe']:,} stocks flagged")
    c.text(PAD, y, "MARKET BREADTH", 20, C["muted"], bold=True)
    bw = W - 2 * PAD
    aw = bw * b["advances"] / total
    c.box(PAD, y + 36, bw, 26, fc=C["down"], r=13)
    if aw > 0:
        c.box(PAD, y + 36, aw, 26, fc=C["up"], r=13)
    c.text(PAD, y + 72, f"▲ {b['advances']:,} advancing", 22, C["up"], bold=True)
    c.text(W - PAD, y + 72, f"{b['declines']:,} declining ▼", 22, C["down"], bold=True, ha="right")
    y += 130
    c.text(PAD, y, "BULLISH WATCH", 20, C["muted"], bold=True)
    y += 40
    _scan_rows(c, y, bull)
    _footer(c, c.h - 40, "Plan = break of today's high · stop at pivot · target 2R · screening output, not advice")
    first = c.png()

    h2 = 134 + 56 + max(1, len(bear)) * 112 + 56
    c = Canvas(h2)
    y = _header(c, "EVENING SCAN · BEARISH", rep["scan_date"], "Short candidates (intraday only in the cash market)")
    c.text(PAD, y, "BEARISH WATCH", 20, C["muted"], bold=True)
    y += 40
    _scan_rows(c, y, bear)
    _footer(c, c.h - 40, "Shorting in the cash market must be squared off the same day")
    return [first, c.png()]


def corporate_card(flows: dict | None, anns: list[dict], results: list[str], deals: list[dict], date_label: str = "") -> bytes:
    anns, deals = anns[:10], [d for d in deals if abs(d["net_cr"]) >= 5][:6]
    res_lines = 2 if results else 0
    h = 134 + (150 if flows else 0) + 56 + max(1, len(anns)) * 44 + (res_lines * 30 + 60 if results else 0) \
        + 56 + max(1, len(deals)) * 70 + 90
    c = Canvas(h)
    y = _header(c, "CORPORATE & FLOWS", date_label, "Filtered announcements, bulk/block deals, institutional flows")
    if flows:
        half = (W - 2 * PAD - 14) / 2
        k = flows.get("sessions", 1)
        _flow_tile(c, PAD, y, half, f"FII CASH · {flows['date']}", flows.get("FII"), k)
        _flow_tile(c, PAD + half + 14, y, half, "DII CASH", flows.get("DII"), k)
        y += 150
    c.text(PAD, y, "KEY ANNOUNCEMENTS (LIQUID STOCKS)", 20, C["muted"], bold=True)
    y += 40
    for a in anns:
        col = C["up"] if a["bias"] > 0 else C["down"] if a["bias"] < 0 else C["grey"]
        c.dot(PAD + 10, y + 16, col)
        c.text(PAD + 32, y, a["symbol"], 24, bold=True)
        amt = f"   ₹{a['amount_cr']:,.0f} cr" if a.get("amount_cr") else ""
        c.text(PAD + 250, y + 2, c.fit(a["label"] + amt, 23, W - PAD - 250 - PAD), 23, C["muted"] if a["bias"] == 0 else col)
        y += 44
    if not anns:
        c.text(PAD, y, "Nothing notable today.", 24, C["muted"])
        y += 44
    if results:
        y += 10
        c.text(PAD, y, f"RESULTS FILED ({len(results)})", 20, C["muted"], bold=True)
        y += 34
        txt = ", ".join(results[:40]) + (f" +{len(results) - 40} more" if len(results) > 40 else "")
        for l in c.wrap(txt, 20, W - 2 * PAD, 2):
            c.text(PAD, y, l, 20, C["text"])
            y += 30
        y += 14
    y += 6
    c.text(PAD, y, "BULK / BLOCK DEALS (NET, ≥ ₹5 CR)", 20, C["muted"], bold=True)
    y += 40
    for d in deals:
        buy = d["net_cr"] > 0
        col = C["up"] if buy else C["down"]
        c.box(PAD, y, W - 2 * PAD, 62)
        c.pill(PAD + 14, y + 15, "BUY" if buy else "SELL", "#0e1621", col, px=18, h=32)
        c.text(PAD + 120, y + 8, d["symbol"], 25, bold=True)
        c.text(PAD + 120, y + 38, c.fit(d["client"] + (" · institution" if d.get("institutional") else ""), 17, 520), 17, C["muted"])
        c.text(W - PAD - 18, y + 8, f"₹{abs(d['net_cr']):,.1f} cr @ {d['avg_price']:,.2f}", 23, bold=True, ha="right")
        rel = d.get("pct_of_avg_turnover")
        if rel == rel and rel is not None:
            c.text(W - PAD - 18, y + 38, f"{rel:.0f}% of avg daily value", 17, C["muted"], ha="right")
        y += 70
    if not deals:
        c.text(PAD, y, "No large deals published for this session.", 24, C["muted"])
    _footer(c, c.h - 40)
    return c.png()
