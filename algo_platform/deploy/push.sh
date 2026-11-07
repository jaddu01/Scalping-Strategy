#!/usr/bin/env bash
# Upload the code from this Mac to the server, and restart the service there if it is installed.
#
#   deploy/push.sh ubuntu@SERVER_IP                first upload, or a code update
#   deploy/push.sh ubuntu@SERVER_IP --with-data    also copy this Mac's database and .env (accounts,
#                                                  users, strategies, history; first time only)
#   add --force to restart during market hours (normally refused, so a live trade is never disturbed)
set -euo pipefail

TARGET="${1:-}"
shift || true
WITH_DATA=false
FORCE=false
for a in "$@"; do
  case "$a" in
    --with-data) WITH_DATA=true ;;
    --force) FORCE=true ;;
    *) echo "unknown option $a" >&2; exit 2 ;;
  esac
done
if [[ -z "$TARGET" ]]; then
  echo "usage: deploy/push.sh user@server [--with-data] [--force]" >&2
  exit 2
fi
cd "$(dirname "$0")/.."
APP=/opt/algo_platform
SSH=(ssh -o ServerAliveInterval=15 "$TARGET")

echo "==> uploading code to $TARGET:$APP"
"${SSH[@]}" "sudo mkdir -p $APP"
rsync -az --delete --rsync-path="sudo rsync" \
  --include .env.example \
  --exclude /.venv --exclude /data --exclude /logs --exclude /.env --exclude '/.env.*' \
  --exclude __pycache__ --exclude .pytest_cache --exclude .ruff_cache --exclude .code-review-graph \
  --exclude .DS_Store \
  ./ "$TARGET:$APP/"

if $WITH_DATA; then
  if "${SSH[@]}" "sudo test -f $APP/data/algo.db"; then
    echo "The server already has a database; refusing to overwrite it." >&2
    exit 1
  fi
  echo "==> copying this Mac's database (consistent snapshot) and .env"
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  sqlite3 data/algo.db ".backup '$TMP/algo.db'"
  "${SSH[@]}" "sudo mkdir -p $APP/data"
  rsync -az --rsync-path="sudo rsync" "$TMP/algo.db" "$TARGET:$APP/data/algo.db"
  rsync -az --rsync-path="sudo rsync" .env "$TARGET:$APP/.env.from-mac"
  "${SSH[@]}" "sudo chmod 600 $APP/.env.from-mac"
  echo "    IMPORTANT: stop the platform on this Mac now. Two copies logged into the same Angel"
  echo "    account would fight over the session and could place orders twice."
fi

if "${SSH[@]}" "systemctl list-unit-files algo.service >/dev/null 2>&1 && systemctl is-enabled --quiet algo"; then
  NOW=$("${SSH[@]}" "TZ=Asia/Kolkata date +%H%M; TZ=Asia/Kolkata date +%u" | tr '\n' ' ')
  HHMM=${NOW%% *}; DOW=${NOW##* }; DOW=${DOW// /}
  if ! $FORCE && [[ $DOW -le 5 && 10#$HHMM -ge 900 && 10#$HHMM -le 1535 ]]; then
    echo "Code uploaded, but NOT restarted: it is market hours on the server ($HHMM IST)."
    echo "Restart after 15:35 (re-run this script), or add --force."
    exit 0
  fi
  echo "==> installing packages and restarting the service"
  "${SSH[@]}" "sudo chown -R algo:algo $APP && sudo -u algo $APP/.venv/bin/pip install -q -r $APP/deploy/requirements.lock.txt \
    && sudo systemctl restart algo && sleep 3 && systemctl --no-pager --lines=5 status algo | head -8"
else
  echo "==> code uploaded. Now run the one-time setup on the server:"
  echo "    ssh $TARGET 'sudo bash $APP/deploy/setup_server.sh YOUR.DOMAIN.COM'"
fi
