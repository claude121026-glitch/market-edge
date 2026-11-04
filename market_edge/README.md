# Daily Market Edge (personal, free)

Steps 1–2 of the plan: **evening NSE scan** and **global prices board**, with optional Telegram alerts.

## Setup (once)
```bash
pip install pandas pytest
python -m market_edge.run update --days 380     # ~5 min: 1 year of NSE daily files (~100 MB raw, ~140 MB DB)
```
Run all commands from the folder that **contains** `market_edge/`.

## Daily use
| When (IST) | Command | What you get |
|---|---|---|
| ~6:30 PM | `python -m market_edge.run evening` | Downloads today's NSE file, scans ~2,000 stocks, prints/sends the watchlist |
| ~8:45 AM | `python -m market_edge.run morning` | Global prices, macro mood score, top news, sector tilt, upcoming events, last evening's watchlist |
| every 15–30 min | `python -m market_edge.run news` | New macro headlines scored; instant alert for big ones |
| any time | `python -m market_edge.run prices` | Global prices board only |

Reports are also saved as JSON in `data/reports/` and `data/global_prices.json`.

## Telegram alerts (optional, free)
1. In Telegram, message **@BotFather** → `/newbot` → copy the token.
2. Send any message to your new bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`.
3. Set environment variables:
```bash
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
```
Without these, messages are printed to the terminal.

## Scheduling
Linux/Mac (`crontab -e`, times in IST; set `CRON_TZ=Asia/Kolkata` or convert):
```
35 18 * * 1-5  cd /path/to && python -m market_edge.run evening
45 8  * * 1-5  cd /path/to && python -m market_edge.run morning
```
Windows: Task Scheduler with the same commands.

## Macro news tracker (step 3)
`python -m market_edge.run news`: schedule every 15–30 min (e.g. 7 AM–11 PM IST).

- Reads 10 publisher RSS feeds: RBI, ET Economy, ET Markets, Mint, Business Standard, BusinessLine,
  US Fed, investingLive, OilPrice, BBC Business. Feeds are configured in `config.py` (`NEWS_FEEDS`).
- Only **new** headlines are scored. Each gets: category, importance 1–10, impact on Nifty / Bank Nifty / INR,
  sectors helped or hurt, and a one-line takeaway.
- Items with importance ≥ 8 are pushed instantly to Telegram.
- The morning brief shows the **macro mood score** (0–10), combining overnight news with global price moves.
  It also shows the top news, a sector tilt, and the next 7 days of events.

**Scoring engine**
- **Free with no setup:** keyword rules on headlines. They're conservative and catch the big themes
  (RBI, crude, rupee, dollar, Fed, tariffs, war, budget). They're crude compared with an LLM.
- **Better: an LLM, one batched call per run.** For a free Gemini key, go to aistudio.google.com, then:
  ```bash
  export GEMINI_API_KEY=...
  ```
  Or any OpenAI-compatible service:
  ```bash
  export LLM_PROVIDER=openai LLM_API_KEY=... LLM_MODEL=... LLM_BASE_URL=...
  ```
  This covers OpenAI, Groq (free tier) and local Ollama (`LLM_BASE_URL=http://localhost:11434/v1`).
  If the LLM call fails, it falls back to the rules automatically.

**Event calendar** (`events.py`)
- Built in: India CPI (~12th), India GDP, GST data, US jobs report, NSE monthly expiry, FOMC 2026 dates.
  US times are converted to IST with daylight saving handled.
- RBI MPC months show a placeholder until you add the exact date to `data/events.json`:
  ```json
  [{"date": "2026-12-04", "time_ist": "10:00", "name": "RBI MPC decision", "importance": 9}]
  ```
  Add US CPI dates, results dates or anything else the same way.

## Corporate data (step 4)
These run automatically inside `evening`. `python -m market_edge.run corp` can also be scheduled
every 30–60 min in market hours for instant red-flag alerts.

- **Announcements:** about 1,400 a day from NSE.
  - Routine filings are dropped (investor calls, newspaper ads, board meetings without decisions).
  - Internal restructurings are dropped (subsidiary mergers and transfers).
  - What's kept is classified: order win, buyback, open offer, delisting, rating upgrade/downgrade, top-management exit
    (CEO/CFO/MD/chairman only), merger, acquisition, capacity addition, bonus/split, strike,
    auditor resignation, insolvency, default/fraud. Rupee amounts are extracted when mentioned.
  - Results filings are summarised on one line.
  - Only liquid stocks are shown. Red flags (importance ≥ 8) on liquid stocks are sent as instant alerts.
- **Bulk and block deals:** net position per client.
  - The same client buying and selling the same day (market makers, arbitrage) nets out.
  - Each deal shows its size versus the stock's average daily value. 🏛 marks institution-like names.
- **FII/DII cash flows:** the day's net figure, period total and buying/selling streak. They also add up to
  ±0.5 to the macro mood score.
- **Watchlist tags:** evening-scan picks with a same-day catalyst get a 📌 tag (e.g. `📌 Results filed`,
  `📌 Bulk BUY ₹20 cr`), so you can see *why* a stock moved.

NSE only publishes deals and FII/DII for the latest session, so history builds up from the day you start
running `evening` daily.

## Watchlist, paper log and backtest (step 5)
| Command | What it does |
|---|---|
| `evening` (daily) | Scores yesterday's paper trades against today's prices, then records today's picks as pending trades |
| `morning` (daily) | Re-ranks picks into a **top-10 watchlist** using catalysts, macro mood, sector tilt and the setup's track record |
| `backtest` | Replays the scan over the stored history, with a random-stock baseline, and refreshes the setup track record |
| `paper` | Live paper results (all picks and the morning top 10) next to the backtest |

**Trade plan simulated:** enter on a break of the signal day's high (long) or low (short) the next session.
The stop is the pivot, the target 2R, and the position is squared off at the close. Sizing uses ₹1,000 risk per trade, capped at ₹2 lakh exposure.
Indian intraday costs (brokerage, STT, exchange, SEBI, stamp, GST) and 0.05% slippage each way are deducted.
Rates are in `config.py`; check them against your broker.

**Daily bars can't show what happened first within the day**, so every trade is scored twice. In the pessimistic
version, a stop touch after entry is a loss. In the optimistic version, a target touch is a win and a stop counts only if the close confirms it.
The truth lies in between. Step 6 (live intraday tracking) removes this ambiguity for live trades.

**Backtest result (1 year, ~3,200 entered trades): no proven edge for this plan after costs.**
Per trade it ranged from −0.31R to +0.03R, against −0.56R to +0.06R for random stocks under the same rules.

**Setup track record**, measured exactly as next-day open→close in the predicted direction, before ~0.15–0.2% costs:
- *Volume breakdown* (falling on heavy volume): +0.57%, 61% hit rate, positive in 4/4 quarters.
- *Volume breakout* (rising on heavy volume): −0.24%, faded in 4/4 quarters. Buying these breakouts the next day lost.

These are in-sample findings from one year. Treat them as hypotheses for the live paper log to confirm, not as a strategy.
Intraday short selling in the cash segment must be squared off the same day.

## Dashboard (step 7)
```bash
pip install streamlit altair
streamlit run market_edge/dashboard.py
```
Opens at http://localhost:8501. It reads the local database and JSON files, and sidebar buttons refresh prices,
news, corporate data or run the evening scan.

| Tab | Shows |
|---|---|
| 🌅 Today | Macro mood score, FII/DII, "what it means for India", alerts, global prices by region, next 7 days of events |
| 🎯 Watchlist & Live | Top-10 picks with trigger/stop/target, live status from the intraday tracker, intraday chart with levels |
| 📊 Scanner | Full bullish/bearish scan with signals and catalysts |
| 📰 News & Events | Scored macro news (with links) and sector tilt chart |
| 🏢 Corporate | Filtered announcements, net bulk/block deals, FII/DII history |
| 📒 Performance | Live paper log / backtest / random baseline: win rate, R, P&L curves, exact intraday results, setup track record |
| 🔎 Stock | Any stock: price and volume charts, delivery %, past signals and their outcomes, announcements |

## Live intraday tracking (step 6)
```bash
python -m market_edge.run live
```
Start it around 9:05 AM IST, after `morning`. It runs until 15:35.

- **Polling:** checks each watchlist stock every 60 s (`--poll`), using NSE's intraday chart and falling back to Yahoo 5-minute bars.
- **Alerts:** ⚡ trigger hit (entry) · ✅ target hit · 🛑 stop hit · ⏭ gapped past target.
- **Square-off at 15:20:** a day summary scores every pick **exactly** from the real intraday path, with costs deducted.
- **Path storage:** the price path is saved in `intraday_bars`, so outcomes can be re-checked later.

```bash
python -m market_edge.run resolve --source live
```
This scores past trades exactly with Yahoo 5-minute bars, which go back about 60 days. Use it on days the live tracker
wasn't running. `--source backtest` does the same for recent backtest trades and shows where the truth lies between the
pessimistic and optimistic daily-bar estimates. `paper` shows the exact results next to the estimates.

**Remaining uncertainty:** when the stop and the target both fall inside one 5-minute bar, or a stop touch falls inside
the entry bar, the order is unknown. Those trades are scored as losses and counted as "ambiguous".

**Data notes**
- NSE's intraday chart only covers the current day and was empty when tested at night. Check that the first live
  run shows NSE as the source; otherwise it falls back to Yahoo.
- Yahoo 5-minute highs and lows can differ slightly from NSE's official figures, for example a missing last tick.

## Signals (evening scan)
| Signal | Rule (vs stock's own prior 20 sessions) |
|---|---|
| VOL SPIKE | volume ≥ 3× average |
| DELIV SPIKE | delivered qty ≥ 3× average **and** delivery % ≥ max(35%, its average) |
| HIGH DELIV | delivery % ≥ 60% and ≥ 1.5× its average |
| BRK / BRKDN 20D | close above prior 20-day high / below 20-day low |
| HIGH / LOW 52W | close above prior 52-week high / below 52-week low |
| BIG MOVE | ±5% or more |

Only EQ series, price ≥ ₹20, 20-day average turnover ≥ ₹10 crore. Thresholds are in `config.py`.

**Setup labels:** *Accumulation/Distribution* = real delivery-based buying/selling. *Volume breakout/breakdown* = big volume but mostly intraday churn.

**Plan line:** enter on a break of today's high (bullish) or low (bearish), stop at the pivot, target 2× risk. Risk above 3% is marked ⚠️wide; skip or size down.

## Data sources
- NSE `sec_bhavdata_full_DDMMYYYY.csv`: prices, volume, delivery for every stock, daily. Free.
- NSE Nifty 500 list: names and industries.
- Yahoo Finance chart endpoint: global prices. Free but rate-limited; the last good value is reused and marked *(stale)* if a fetch fails. Futures moves over 6% in a day are marked ⚠️verify (possible contract roll).

These are public endpoints, not official APIs; they can change. Requests are paced at 1 per second.

## Honest note
This is a screener, not a profit guarantee. Most intraday traders lose money. Track every signal on paper (step 5 adds an automatic paper-trade log) for a few weeks before risking money.

## Next steps
3. Macro news tracker (India + global, sector mapping, event calendar)
4. Corporate announcements, bulk/block deals, FII/DII
5. Morning watchlist + paper-trading log
6. Live intraday tracking of the watchlist
7. Streamlit dashboard
