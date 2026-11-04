# Market Edge on free GitHub + Streamlit (no card, no PC)

What runs where:
| Part | Service | Cost |
|---|---|---|
| Scheduled jobs (morning, live, news, corporate, evening, weekly backtest) | GitHub Actions | free (public repo) |
| Database + reports | a GitHub release named `data` in your repo | free |
| Dashboard you open from your phone | Streamlit Community Cloud | free |
| Alerts | WhatsApp (CallMeBot) and/or Telegram | free |

## 1. Add your secrets (2 min)

Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**. Add:

| Name | Value |
|---|---|
| `WHATSAPP_PHONE` | your number with country code, e.g. `+919876543210` |
| `WHATSAPP_APIKEY` | the key CallMeBot sent you |
| `GEMINI_API_KEY` | optional, from https://aistudio.google.com/apikey |

Secrets are encrypted; they never appear in the code or logs.

## 2. Build the data (once, about 10–15 min)

Repo → **Actions** tab → (if asked, click **I understand my workflows, go ahead and enable them**)
→ **market-edge jobs** → **Run workflow** → command `bootstrap` → **Run workflow**.

It downloads a year of NSE data, runs the backtest, sends the global prices board to WhatsApp as a test,
and creates the `data` release. A green tick means it worked. If it's red, open it and send the error to Claude.

## 3. Dashboard on Streamlit Community Cloud (5 min)

1. Go to https://share.streamlit.io and **Continue with GitHub**.
2. **Create app** → **Deploy a public app from GitHub**:
   - Repository: `YOUR-USERNAME/market-edge`, branch `main`
   - Main file path: `market_edge/dashboard.py`
   - App URL: pick a name, e.g. `my-market-edge`
3. **Advanced settings**: Python **3.12**, and under **Secrets** paste (with your username):
   ```toml
   ME_DATA_URL = "https://github.com/YOUR-USERNAME/market-edge/releases/download/data/data.tar.gz"
   ```
4. **Deploy**. Your dashboard: `https://my-market-edge.streamlit.app`.
5. Optional: in the app's **Settings → Sharing**, limit viewers to your own email.

## What runs when (IST)

| Time | Job |
|---|---|
| 08:30 (backup 08:55) Mon–Fri | morning brief + watchlist |
| 09:00–12:10 and 12:05–15:35 Mon–Fri | live tracker (two sessions); day summary at 15:20 |
| every 30 min 09:30–15:00 Mon–Fri | corporate red flags |
| every 30 min ~06:45–22:15 daily | macro news alerts (only big items) |
| 18:35 (backup 19:35) Mon–Fri | evening scan, FII/DII, announcements, deals |
| Saturday 10:00 | backtest refresh + keep-alive commit |

## Good to know
- **GitHub may start scheduled jobs late**, usually by a few minutes and sometimes up to 30. Backups cover the
  morning and evening jobs, and they never send twice.
- **NSE might block GitHub's servers.** The bootstrap run tells us. If it fails on NSE downloads, we'll switch options.
- **The repo and the data release are public.** They hold code, prices, news and paper trades; your keys stay in Secrets.
- **GitHub pauses schedules** in repos with no activity for 60 days; the Saturday job makes a small commit to prevent it.
- **Run any job by hand**: Actions → market-edge jobs → Run workflow → type the command (e.g. `evening`, `prices`, `paper`).
