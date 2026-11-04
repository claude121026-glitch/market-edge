#!/usr/bin/env bash
# Market Edge server setup for Ubuntu 22.04 / 24.04 (Oracle Cloud, AWS, DigitalOcean, any VPS).
#
#   Upload market_edge_code.zip (and optionally market_edge_data.zip) to the server, then:
#     unzip market_edge_code.zip && unzip -o market_edge_data.zip    # data zip is optional
#     sudo bash market_edge/deploy/setup.sh
#
# Safe to re-run: it updates the code and keeps your data and settings.
set -euo pipefail

APP_USER="marketedge"
APP_DIR="/opt/market_edge_app"            # contains market_edge/ and .venv/
ENV_FILE="/etc/market_edge.env"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"   # folder that contains market_edge/

[[ $EUID -eq 0 ]] || { echo "Run with sudo: sudo bash market_edge/deploy/setup.sh"; exit 1; }
[[ -f "$SRC_DIR/market_edge/run.py" ]] || { echo "Can't find market_edge/run.py next to this script."; exit 1; }

echo "==> 1/8 System packages and India time zone"
timedatectl set-timezone Asia/Kolkata
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip curl gnupg debian-keyring debian-archive-keyring apt-transport-https sqlite3

if ! command -v caddy >/dev/null; then
  echo "==> Installing Caddy (HTTPS web server) from its official repository"
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
fi

echo "==> 2/8 App user and code"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR/market_edge/data"
# Copy code; never overwrite existing server data.
tar -C "$SRC_DIR" --exclude='market_edge/data' --exclude='__pycache__' -cf - market_edge | tar -C "$APP_DIR" -xf -
if [[ -d "$SRC_DIR/market_edge/data" && ! -f "$APP_DIR/market_edge/data/market_edge.db" ]]; then
  echo "    copying the data you uploaded (first install only)"
  cp -r "$SRC_DIR/market_edge/data/." "$APP_DIR/market_edge/data/"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> 3/8 Python environment"
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/market_edge/requirements.txt"

echo "==> 4/8 Settings file ($ENV_FILE)"
if [[ ! -f "$ENV_FILE" ]]; then
  install -m 600 -o root -g root "$APP_DIR/market_edge/deploy/market_edge.env.example" "$ENV_FILE"
  echo "    created; add your WhatsApp details later with: sudo nano $ENV_FILE"
fi

# The settings file is root-only (it holds your keys): read it as root, pass the values to the app user.
run_as_app() { ( set -a; source "$ENV_FILE"; set +a
                 sudo -E -u "$APP_USER" bash -c "cd $APP_DIR && .venv/bin/python -m market_edge.run $*" ); }

echo "==> 5/8 Tests"
sudo -u "$APP_USER" bash -c "cd $APP_DIR && .venv/bin/python -m pytest -q market_edge/tests" | tail -1

if [[ ! -f "$APP_DIR/market_edge/data/market_edge.db" ]]; then
  echo "==> 6/8 First data download (~5 min for 1 year of NSE files, then a backtest)"
  run_as_app update --days 380
  run_as_app backtest >/dev/null
else
  echo "==> 6/8 Data found; downloading any missing days"
  run_as_app update --days 30 >/dev/null
fi

echo "==> 7/8 Scheduled jobs (systemd timers, IST)"
cat > /etc/systemd/system/market-edge@.service <<EOF
[Unit]
Description=Market Edge job: %i
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/python -m market_edge.run %i
TimeoutStartSec=8h
Nice=5
EOF

make_timer() {   # name  OnCalendar  persistent
  cat > "/etc/systemd/system/market-edge-$1.timer" <<EOF
[Unit]
Description=Market Edge $1 schedule

[Timer]
OnCalendar=$2
Persistent=$3
Unit=market-edge@$1.service

[Install]
WantedBy=timers.target
EOF
}
make_timer morning  "Mon..Fri 08:45 Asia/Kolkata"            false
make_timer live     "Mon..Fri 09:05 Asia/Kolkata"            false
make_timer corp     "Mon..Fri 09..15:15/30 Asia/Kolkata"     false
make_timer news     "*-*-* 07..22:00/20 Asia/Kolkata"        false
make_timer evening  "Mon..Fri 18:35 Asia/Kolkata"            true
make_timer backtest "Sat 10:00 Asia/Kolkata"                 false

cat > /etc/systemd/system/market-edge-dashboard.service <<EOF
[Unit]
Description=Market Edge dashboard (Streamlit, local only; Caddy serves it)
After=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/streamlit run market_edge/dashboard.py --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now market-edge-dashboard.service
for t in morning live corp news evening backtest; do systemctl enable --now "market-edge-$t.timer"; done

echo "==> 8/8 HTTPS + password for the dashboard"
if [[ -z "${DOMAIN:-}" ]]; then
  PUBLIC_IP="$(curl -fsS --max-time 10 https://api.ipify.org || true)"
  if [[ -z "$PUBLIC_IP" ]]; then
    echo "    Couldn't detect this server's public IP. Re-run with your IP or domain, e.g.:"
    echo "    sudo DOMAIN=1-2-3-4.sslip.io bash market_edge/deploy/setup.sh"
    exit 1
  fi
  DOMAIN="${PUBLIC_IP//./-}.sslip.io"      # free hostname that points at this IP; gets a real HTTPS cert
fi
if [[ ! -f /etc/caddy/market_edge_hash ]]; then
  if [[ -z "${DASH_PASSWORD:-}" ]]; then
    read -r -s -p "    Choose a dashboard password: " DASH_PASSWORD; echo
  fi
  caddy hash-password --plaintext "$DASH_PASSWORD" > /etc/caddy/market_edge_hash
  chmod 600 /etc/caddy/market_edge_hash; chown caddy:caddy /etc/caddy/market_edge_hash 2>/dev/null || true
fi
HASH="$(cat /etc/caddy/market_edge_hash)"
cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
    encode gzip
    basic_auth {
        admin $HASH
    }
    reverse_proxy 127.0.0.1:8501
}
EOF
# Open web ports: ufw (most VPS) and Oracle's iptables rules.
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow 80/tcp; ufw allow 443/tcp; fi
if iptables -S INPUT 2>/dev/null | grep -q REJECT; then
  for p in 443 80; do iptables -C INPUT -p tcp --dport $p -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -p tcp --dport $p -j ACCEPT; done
  command -v netfilter-persistent >/dev/null && netfilter-persistent save || true
fi
systemctl restart caddy

cat <<EOF

============================================================
 Market Edge is installed.

 Dashboard:  https://$DOMAIN      (user: admin, your password)
             The HTTPS certificate can take a minute on first visit.
             On Oracle/AWS also allow TCP 80 and 443 in the cloud
             firewall (security list / security group).

 Alerts:     sudo nano $ENV_FILE     (WhatsApp phone + API key)
             then test:  sudo systemctl start market-edge@prices

 Schedules:  systemctl list-timers 'market-edge*'
 Logs:       journalctl -u 'market-edge@*' --since today
============================================================
EOF
