#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 24.04 server for the algo platform.
#
# Run on the server, after deploy/push.sh has uploaded the code to /opt/algo_platform:
#     sudo bash /opt/algo_platform/deploy/setup_server.sh algo.example.com
#
# What it does: IST timezone + time sync, firewall (SSH/80/443 only), automatic security updates,
# a dedicated 'algo' system user, Python venv with pinned packages, .env (re-using the Mac's master
# key when its data was uploaded), a systemd service that restarts on failure, Caddy for HTTPS with
# an automatic certificate, and a daily database backup. Safe to run again.
set -euo pipefail

DOMAIN="${1:-}"
if [[ -z "$DOMAIN" ]]; then
  echo "usage: sudo bash deploy/setup_server.sh YOUR.DOMAIN.COM" >&2
  exit 2
fi
if [[ $EUID -ne 0 ]]; then
  echo "run with sudo" >&2
  exit 1
fi

APP_DIR=/opt/algo_platform
APP_USER=algo
cd "$APP_DIR"

echo "==> system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip sqlite3 ufw unattended-upgrades chrony caddy rsync
timedatectl set-timezone Asia/Kolkata
systemctl enable --now chrony >/dev/null
dpkg-reconfigure -f noninteractive unattended-upgrades >/dev/null

echo "==> firewall: SSH, HTTP, HTTPS only"
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

echo "==> app user and folders"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR/data/backups" "$APP_DIR/logs"

echo "==> python environment"
[[ -x "$APP_DIR/.venv/bin/python" ]] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/deploy/requirements.lock.txt"

echo "==> .env"
set_env() {  # set_env KEY VALUE  (replace or append)
  if grep -q "^$1=" "$APP_DIR/.env"; then
    sed -i "s|^$1=.*|$1=$2|" "$APP_DIR/.env"
  else
    echo "$1=$2" >> "$APP_DIR/.env"
  fi
}
if [[ ! -f "$APP_DIR/.env" ]]; then
  if [[ -f "$APP_DIR/.env.from-mac" ]]; then
    mv "$APP_DIR/.env.from-mac" "$APP_DIR/.env"   # same master key, so the uploaded credentials decrypt
    echo "    using the .env uploaded from the Mac (same master key)"
  else
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    set_env ALGO_MASTER_KEY "$("$APP_DIR/.venv/bin/python" "$APP_DIR/run.py" genkey)"
    echo "    generated a NEW master key: back up $APP_DIR/.env somewhere safe"
  fi
fi
set_env ALGO_HOST 127.0.0.1
set_env ALGO_PORT 8000
set_env ALGO_DATA_DIR "$APP_DIR/data"
set_env ALGO_DATABASE_URL ""
set_env ALGO_TRUST_PROXY_HEADERS true
set_env ALGO_COOKIE_SECURE true
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

echo "==> database migrations"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" "$APP_DIR/run.py" migrate

echo "==> systemd service"
install -m 644 "$APP_DIR/deploy/algo.service" /etc/systemd/system/algo.service
systemctl daemon-reload
systemctl enable algo >/dev/null
systemctl restart algo

echo "==> HTTPS (Caddy) for $DOMAIN"
sed "s|{{DOMAIN}}|$DOMAIN|g" "$APP_DIR/deploy/Caddyfile.template" > /etc/caddy/Caddyfile
systemctl enable caddy >/dev/null
systemctl reload caddy || systemctl restart caddy

echo "==> daily database backup (16:05 IST, keeps 30 days)"
install -m 755 "$APP_DIR/deploy/backup.sh" /usr/local/bin/algo-backup
echo "5 16 * * * $APP_USER /usr/local/bin/algo-backup" > /etc/cron.d/algo-backup

sleep 3
echo
systemctl --no-pager --lines=0 status algo | head -3
USERS=$(sqlite3 "$APP_DIR/data/algo.db" "select count(*) from users" 2>/dev/null || echo 0)
echo
echo "Done. Open https://$DOMAIN"
if [[ "$USERS" == "0" ]]; then
  echo "No users yet. Create the admin from this shell (do NOT use the web /setup page on a public server):"
  echo "    sudo -u $APP_USER $APP_DIR/.venv/bin/python $APP_DIR/run.py create-admin admin"
fi
echo "Server IP for Angel One static-IP registration: $(curl -s -4 https://ifconfig.me || hostname -I)"
