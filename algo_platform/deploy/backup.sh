#!/usr/bin/env bash
# Consistent copy of the live SQLite database (safe while the app is running). Keeps 30 days.
set -euo pipefail
DIR=/opt/algo_platform/data
mkdir -p "$DIR/backups"
sqlite3 "$DIR/algo.db" ".backup '$DIR/backups/algo-$(date +%F).db'"
find "$DIR/backups" -name 'algo-*.db' -mtime +30 -delete
