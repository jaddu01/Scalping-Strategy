#!/usr/bin/env bash
# Update the platform on a server where it runs as the Docker container "algo_platform"
# (shared server set up per docs/DEPLOY.md, section "Docker on a shared server").
#
#   deploy/push_docker.sh                 upload code, rebuild the image, restart the container
#   deploy/push_docker.sh --force         also during market hours (normally refused)
#
# Server and key default to the current deployment; override with ALGO_SERVER / ALGO_SSH_KEY.
set -euo pipefail

SERVER="${ALGO_SERVER:-ubuntu@65.0.62.220}"
KEY="${ALGO_SSH_KEY:-$HOME/Downloads/algo-trading.pem}"
FORCE=false
[[ "${1:-}" == "--force" ]] && FORCE=true
APP=/opt/algo_platform
cd "$(dirname "$0")/.."
ssh_() { ssh -i "$KEY" -o ServerAliveInterval=15 "$SERVER" "$@"; }

echo "==> uploading code to $SERVER:$APP"
rsync -az --delete --rsync-path="sudo rsync" -e "ssh -i $KEY" \
  --include .env.example \
  --exclude /.venv --exclude /data --exclude /logs --exclude /.env --exclude '/.env.*' \
  --exclude __pycache__ --exclude .pytest_cache --exclude .ruff_cache --exclude .code-review-graph \
  --exclude .DS_Store \
  ./ "$SERVER:$APP/"

echo "==> building the image (the running platform is not affected yet)"
ssh_ "cd $APP && sudo docker compose -f deploy/docker/compose.yml build -q"

HHMM=$(ssh_ "TZ=Asia/Kolkata date +%H%M"); DOW=$(ssh_ "TZ=Asia/Kolkata date +%u")
OPEN=$(ssh_ "sudo docker exec algo_platform sqlite3 /app/data/algo.db \"select count(*) from trades where status in ('OPEN','EXITING','PENDING_ENTRY')\"" || echo "?")
if ! $FORCE && [[ $DOW -le 5 && 10#$HHMM -ge 900 && 10#$HHMM -le 1535 ]]; then
  echo "Image built, but NOT restarted: market hours on the server ($HHMM IST, open trades: $OPEN)."
  echo "Re-run after 15:35, or add --force (only when no trade is open)."
  exit 0
fi
if [[ "$OPEN" != "0" ]] && ! $FORCE; then
  echo "Not restarted: $OPEN trade(s) open. Re-run when flat, or add --force." >&2
  exit 1
fi

echo "==> restarting the container"
ssh_ "cd $APP && sudo docker compose -f deploy/docker/compose.yml up -d && sleep 25 && sudo docker ps --filter name=algo_platform --format '{{.Names}} {{.Status}}'"
