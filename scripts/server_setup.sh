#!/usr/bin/env bash
# One-time setup of JGAPicksBot on a fresh Ubuntu 22.04/24.04 server.
# Run as root AFTER copying the project (and optionally kalshi.pem) to the server:
#     bash /home/bot/jga.pick/scripts/server_setup.sh
#
# Installs Python + dependencies, runs the bot as a systemd service that starts on
# boot and restarts after crashes, enables a firewall (SSH only - the bot needs no
# open ports), adds 1 GB swap, and turns on AUTO_START (paper mode only).
set -euo pipefail
APP_USER=bot
APP_DIR=/home/$APP_USER/jga.pick
SERVICE=jgapicks

[ "$(id -u)" = 0 ] || { echo "Please run as root: sudo bash $0"; exit 1; }
[ -f "$APP_DIR/app/main.py" ] || { echo "Project not found in $APP_DIR - copy it first (see README 'Deploy')."; exit 1; }
[ -f "$APP_DIR/.env" ] || { echo "$APP_DIR/.env missing - copy your .env from the Chromebook first."; exit 1; }

echo "==> System packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y git curl ca-certificates sqlite3 ufw

echo "==> App user"
id "$APP_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash "$APP_USER"

echo "==> Kalshi key"
mkdir -p /home/$APP_USER/.kalshi
for k in /root/kalshi.pem /root/.kalshi/kalshi.pem; do
  if [ -f "$k" ]; then mv "$k" /home/$APP_USER/.kalshi/kalshi.pem; fi
done
if [ -f /home/$APP_USER/.kalshi/kalshi.pem ]; then
  chmod 600 /home/$APP_USER/.kalshi/kalshi.pem
  sed -i "s|^KALSHI_PRIVATE_KEY_PATH=.*|KALSHI_PRIVATE_KEY_PATH=/home/$APP_USER/.kalshi/kalshi.pem|" "$APP_DIR/.env"
fi
if grep -q '^AUTO_START=' "$APP_DIR/.env"; then
  sed -i 's/^AUTO_START=.*/AUTO_START=true/' "$APP_DIR/.env"
else
  echo "AUTO_START=true" >> "$APP_DIR/.env"
fi
chmod 600 "$APP_DIR/.env"
chown -R $APP_USER:$APP_USER /home/$APP_USER

echo "==> Python 3.12 + dependencies (uv)"
sudo -u $APP_USER bash -c 'command -v ~/.local/bin/uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh'
sudo -u $APP_USER bash -c "cd $APP_DIR && rm -rf .venv && ~/.local/bin/uv venv --python 3.12 .venv \
  && ~/.local/bin/uv pip install --python .venv/bin/python -r requirements-dev.txt"

echo "==> Checking configuration"
sudo -u $APP_USER bash -c "cd $APP_DIR && .venv/bin/python -m app.main --check-config"

echo "==> Swap (helps a 1 GB server during replays)"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "==> Firewall (SSH only)"
ufw allow OpenSSH >/dev/null && ufw --force enable >/dev/null

echo "==> systemd service"
cat > /etc/systemd/system/$SERVICE.service <<UNIT
[Unit]
Description=JGAPicksBot (Kalshi paper-trading bot)
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python -m app.main
Restart=always
RestartSec=15
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now $SERVICE
sleep 8
systemctl --no-pager --lines=15 status $SERVICE || true

cat <<DONE

✅ JGAPicksBot is installed and running as a service.
   It starts on boot, restarts after crashes, and starts the scanner automatically (paper mode).

   Logs (live):        journalctl -u $SERVICE -f        (Ctrl+C to stop watching)
   Restart:            systemctl restart $SERVICE
   Stop / start:       systemctl stop $SERVICE  /  systemctl start $SERVICE
   Update the code:    cd $APP_DIR && sudo -u $APP_USER git pull && systemctl restart $SERVICE
   Reports:            cd $APP_DIR && sudo -u $APP_USER .venv/bin/python scripts/diagnose.py

   In Telegram: /menu  (the bot is already started)
DONE
