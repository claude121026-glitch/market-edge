"""All settings in one place. Override any of them with environment variables."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("ME_DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "market_edge.db"
RAW_DIR = DATA_DIR / "raw"            # cached NSE daily files (safe to delete)
REPORT_DIR = DATA_DIR / "reports"

# Honest, identifiable client name. Be polite: NSE and Yahoo are free public sources.
USER_AGENT = os.getenv("ME_USER_AGENT", "market-edge-personal/0.1")
REQUEST_GAP_SEC = float(os.getenv("ME_REQUEST_GAP", "1.0"))

# ---- Evening scan -------------------------------------------------------- #
SERIES = ("EQ",)                       # intraday-tradeable cash series
MIN_PRICE = float(os.getenv("ME_MIN_PRICE", "20"))
MIN_AVG_TURNOVER_CR = float(os.getenv("ME_MIN_TURNOVER_CR", "10"))  # 20-day avg, Rs crore
VOL_SPIKE_X = 3.0                      # volume vs 20-day average
DELIV_SPIKE_X = 3.0                    # delivered qty vs 20-day average
HIGH_DELIV_PCT = 60.0
MIN_DELIV_PCT_FOR_SPIKE = 35.0      # delivery % floor for a DELIV_SPIKE
MAX_INTRADAY_RISK_PCT = 3.0        # stop distance above this is marked "wide"
BIG_MOVE_PCT = 5.0
TOP_BULLISH = 15
TOP_BEARISH = 10
HISTORY_DAYS = 380                     # calendar days to backfill (~252 sessions for 52-week)

# ---- Telegram (optional) ------------------------------------------------- #
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip() or None   # secrets often carry stray spaces/newlines
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip() or None   # secrets often carry stray spaces/newlines
# ---- WhatsApp via CallMeBot (optional, free for personal use; see notify.py) ---- #
WHATSAPP_PHONE = (os.getenv("WHATSAPP_PHONE") or "").strip() or None   # secrets often carry stray spaces/newlines      # with country code, e.g. +919876543210
WHATSAPP_APIKEY = (os.getenv("WHATSAPP_APIKEY") or "").strip() or None   # secrets often carry stray spaces/newlines

# ---- Global prices ------------------------------------------------------- #
# kind: "pct" -> alert on % change, "bp" -> alert on basis-point change (yields)
# level_alert: alert when price is at/above this level (volatility indices)
GLOBAL_TICKERS = [
    # group,        key,       name,               yahoo,       kind,  alert, level_alert
    ("India",      "NIFTY",   "Nifty 50",         "^NSEI",     "pct", 1.5, None),
    ("India",      "BANKNIFTY", "Bank Nifty",     "^NSEBANK",  "pct", 1.5, None),
    ("India",      "INDIAVIX", "India VIX",       "^INDIAVIX", "pct", 10,  20),
    ("US",         "SPX",     "S&P 500",          "^GSPC",     "pct", 1.5, None),
    ("US",         "NASDAQ",  "Nasdaq",           "^IXIC",     "pct", 2.0, None),
    ("US",         "DOW",     "Dow Jones",        "^DJI",      "pct", 1.5, None),
    ("US",         "VIX",     "US VIX",           "^VIX",      "pct", 15,  25),
    ("Asia",       "NIKKEI",  "Nikkei 225",       "^N225",     "pct", 2.0, None),
    ("Asia",       "HANGSENG", "Hang Seng",       "^HSI",      "pct", 2.0, None),
    ("Asia",       "SHANGHAI", "Shanghai Comp",   "000001.SS", "pct", 2.0, None),
    ("Europe",     "DAX",     "DAX",              "^GDAXI",    "pct", 1.5, None),
    ("Europe",     "FTSE",    "FTSE 100",         "^FTSE",     "pct", 1.5, None),
    ("Currencies", "DXY",     "Dollar Index",     "DX-Y.NYB",  "pct", 0.6, None),
    ("Currencies", "USDINR",  "USD/INR",          "INR=X",     "pct", 0.4, None),
    ("Currencies", "EURUSD",  "EUR/USD",          "EURUSD=X",  "pct", 0.7, None),
    ("Currencies", "USDJPY",  "USD/JPY",          "JPY=X",     "pct", 1.0, None),
    ("Bonds",      "US10Y",   "US 10Y yield %",   "^TNX",      "bp",  10,  None),
    ("Energy",     "BRENT",   "Brent crude",      "BZ=F",      "pct", 3.0, None),
    ("Energy",     "WTI",     "WTI crude",        "CL=F",      "pct", 3.0, None),
    ("Energy",     "NATGAS",  "Natural gas",      "NG=F",      "pct", 5.0, None),
    ("Metals",     "GOLD",    "Gold",             "GC=F",      "pct", 1.5, None),
    ("Metals",     "SILVER",  "Silver",           "SI=F",      "pct", 3.0, None),
    ("Metals",     "COPPER",  "Copper",           "HG=F",      "pct", 2.5, None),
    ("Crypto",     "BTC",     "Bitcoin",          "BTC-USD",   "pct", 5.0, None),
]
GLOBAL_JSON = DATA_DIR / "global_prices.json"

# ---- Macro news (step 3) ------------------------------------------------- #
# Publisher-provided RSS feeds (verified working). Moneycontrol/CNBC refuse automated access.
NEWS_FEEDS = [
    # name,            region,   url
    ("RBI",            "India",  "https://www.rbi.org.in/pressreleases_rss.xml"),
    ("ET Economy",     "India",  "https://economictimes.indiatimes.com/news/economy/rssfeeds/1373380680.cms"),
    ("ET Markets",     "India",  "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Mint Economy",   "India",  "https://www.livemint.com/rss/economy"),
    ("Business Std",   "India",  "https://www.business-standard.com/rss/economy-102.rss"),
    ("BusinessLine",   "India",  "https://www.thehindubusinessline.com/economy/feeder/default.rss"),
    ("US Fed",         "US",     "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("investingLive",  "Global", "https://investinglive.com/feed/"),
    ("OilPrice",       "Global", "https://oilprice.com/rss/main"),
    ("BBC Business",   "Global", "https://feeds.bbci.co.uk/news/business/rss.xml"),
]
NEWS_MAX_AGE_HOURS = 24          # ignore older items
NEWS_FIRST_RUN_HOURS = 6         # on the very first run, only score this much backlog
NEWS_MAX_PER_LLM_CALL = 60
NEWS_ALERT_IMPORTANCE = 8        # push an instant Telegram alert at/above this

# LLM (optional; without a key, keyword rules are used)
#   LLM_PROVIDER=gemini  + GEMINI_API_KEY                    (free tier at aistudio.google.com)
#   LLM_PROVIDER=openai  + LLM_API_KEY [+ LLM_BASE_URL]       (OpenAI, Groq, Ollama, any OpenAI-compatible)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini")
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-2.5-flash" if LLM_PROVIDER == "gemini" else "gpt-4o-mini")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("OPENAI_API_KEY")

SECTORS = [
    "Banks", "NBFC & Financials", "Insurance", "IT", "Pharma", "Healthcare", "Auto", "FMCG",
    "Metals", "Oil & Gas Upstream", "OMCs", "Power", "Renewables", "Capital Goods & Infra",
    "Cement", "Realty", "Telecom", "Chemicals", "Textiles & Exporters", "Aviation", "Paints",
    "Defence", "PSU", "Consumer Durables", "Gold Financiers & Jewellers", "Agri & Sugar",
    "Logistics", "Media",
]
EVENTS_JSON = DATA_DIR / "events.json"

# ---- Paper trading (step 5) ---------------------------------------------- #
RISK_PER_TRADE = float(os.getenv("ME_RISK_PER_TRADE", "1000"))        # Rs lost if stop is hit
MAX_POSITION_VALUE = float(os.getenv("ME_MAX_POSITION", "200000"))   # Rs exposure cap per trade
SLIPPAGE_PCT = 0.05            # % adverse slippage on entry and on exit
# Approximate Indian intraday equity costs (discount broker). Check your broker's current rates.
BROKERAGE_PCT, BROKERAGE_MAX = 0.03, 20.0   # per order: min(0.03%, Rs 20)
STT_SELL_PCT = 0.025           # intraday STT, sell side
EXCHANGE_PCT = 0.00297         # NSE transaction charge, both sides
SEBI_PCT = 0.0001              # Rs 10 per crore
STAMP_BUY_PCT = 0.003          # buy side
GST_PCT = 18.0                 # on brokerage + exchange + SEBI
WATCHLIST_SIZE = 10

# Send report cards to Telegram as images (set ME_IMAGES=0 for text only)
IMAGES = os.getenv("ME_IMAGES", "1") != "0"
