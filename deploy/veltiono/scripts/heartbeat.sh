#!/usr/bin/env bash
# Daily "alive" message. Silence in the ops chat at that hour = the whole server is down
# (cron on the server itself cannot report that).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"
load_env
last="$(cat "$STATE_DIR/last-backup-ok" 2>/dev/null || echo 'ещё не было')"
if health_ok "$HEALTH_URL"; then h="отвечает"; else h="НЕ отвечает"; fi
tg_send "жив; /health $h; последний успешный бэкап: $last"
