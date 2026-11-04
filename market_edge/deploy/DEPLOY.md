# Put Market Edge online

About 30–45 minutes. When you're done:
- jobs run on their own every weekday (IST)
- WhatsApp alerts arrive on your phone
- the dashboard opens from anywhere at `https://<your-ip-with-dashes>.sslip.io`, protected by a password

---

## 1. Get a server (Mumbai region)

Pick one. The setup script is the same for all of them; you need **Ubuntu 24.04** and **at least 1 GB RAM** (2 GB+ is comfortable).

| Option | Cost | Notes |
|---|---|---|
| **Oracle Cloud Always Free** | ₹0 | Card needed for sign-up checks. Choose **India West (Mumbai)** as home region (can't change later). Shape **VM.Standard.A1.Flex, 1 OCPU, 6 GB**. If it says "out of capacity", try again later. |
| AWS Lightsail (Mumbai) | about $5–7/month | Simplest paid option |
| DigitalOcean (Bangalore) | about $6/month | Simple too |

When creating the server:
1. Image: **Ubuntu 24.04**.
2. Download the **SSH key** (a `.key` or `.pem` file) and keep it safe.
3. Note the server's **public IP address**.
4. Allow web traffic: add inbound rules for **TCP 80 and 443** from anywhere.
   - Oracle: Networking → your VCN → Security List → Add Ingress Rules (source `0.0.0.0/0`, ports 80 and 443).
   - Lightsail: Networking tab → add HTTPS (443) and HTTP (80).
   - DigitalOcean: open by default.

## 2. Upload the project

On your computer, in the folder with the two zip files (Windows: PowerShell; Mac: Terminal):

```bash
scp -i path/to/your-key.key market_edge_code.zip market_edge_data.zip ubuntu@YOUR_IP:~
```

(Lightsail/Oracle user is `ubuntu`; DigitalOcean is `root`. On Mac/Linux you may first need `chmod 600 path/to/your-key.key`.)

## 3. Install

```bash
ssh -i path/to/your-key.key ubuntu@YOUR_IP
```
Then on the server:
```bash
sudo apt-get update && sudo apt-get install -y unzip
unzip -o market_edge_code.zip && unzip -o market_edge_data.zip
sudo bash market_edge/deploy/setup.sh
```
It asks you to **choose a dashboard password**, then installs everything (5–10 minutes) and prints your dashboard link.

## 4. Open the dashboard

Go to the link it printed, e.g. `https://129-154-12-34.sslip.io`, and log in as **admin** with your password.
The first visit can take a minute while the free HTTPS certificate is issued.

## 5. Turn on WhatsApp alerts (CallMeBot, free)

1. Open https://www.callmebot.com/blog/free-api-whatsapp-messages/ and follow its steps: save CallMeBot's WhatsApp
   number from that page in your contacts and send it the activation message shown there.
2. You get a reply with your **API key**.
3. On the server:
   ```bash
   sudo nano /etc/market_edge.env
   ```
   Fill in (with country code):
   ```
   WHATSAPP_PHONE=+919876543210
   WHATSAPP_APIKEY=123456
   ```
   Save with Ctrl+O, Enter, Ctrl+X.
4. Test it (sends the global prices board to WhatsApp):
   ```bash
   sudo systemctl start market-edge@prices
   ```

Messages pass through CallMeBot's servers. If you'd rather not, fill in the Telegram lines instead; both can be on.

Optional: add `GEMINI_API_KEY=` (free from aistudio.google.com) for better news scoring.

## 6. What runs when (IST)

| Time | Job | You get |
|---|---|---|
| 8:45 AM Mon–Fri | `morning` | Global markets, macro mood, top news, events, today's watchlist |
| 9:05 AM–3:35 PM Mon–Fri | `live` | Trigger / target / stop alerts, day summary at 3:20 PM |
| every 30 min, 9:15 AM–3:45 PM Mon–Fri | `corp` | Red-flag alerts (auditor quits, insolvency, defaults) |
| every 20 min, 7 AM–10:40 PM daily | `news` | Alerts only for big macro news (importance 8+) |
| 6:35 PM Mon–Fri | `evening` | Scan, watchlist candidates, FII/DII, announcements, bulk deals |
| Saturday 10 AM | `backtest` | Weekly refresh of the setup track record |

On NSE holidays the live tracker stops itself at 10:00 with a message.

## Everyday commands (on the server)

| What | Command |
|---|---|
| See schedules and next run times | `systemctl list-timers 'market-edge*'` |
| Today's logs | `journalctl -u 'market-edge@*' --since today` |
| Run a job now | `sudo systemctl start market-edge@evening` (or morning, news, corp, prices, paper) |
| Dashboard status | `systemctl status market-edge-dashboard` |
| Change settings | `sudo nano /etc/market_edge.env` |
| Change dashboard password | `sudo rm /etc/caddy/market_edge_hash && sudo bash market_edge/deploy/setup.sh` |
| Update the code | upload the new `market_edge_code.zip`, `unzip -o` it, re-run `sudo bash market_edge/deploy/setup.sh` (your data and settings are kept) |
| Back up the data to your computer | `scp -i key ubuntu@YOUR_IP:/opt/market_edge_app/market_edge/data/market_edge.db .` |

## Security notes
- The dashboard is only reachable through HTTPS with your password; Streamlit itself listens on the server only.
- Your keys live in `/etc/market_edge.env`, readable only by the administrator.
- Ubuntu installs security updates automatically by default. Run `sudo apt-get upgrade` now and then.
