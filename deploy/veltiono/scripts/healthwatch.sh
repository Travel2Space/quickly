#!/usr/bin/env bash
# Every 5 min: /health through the proxy; alert after 2 failures in a row, once per incident,
# and once on recovery. Also: disk > 85 %, TLS certificate < 14 days (each at most once a day).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"
load_env

FAILS="$STATE_DIR/health-fails"; DOWN="$STATE_DIR/health-down"

once_a_day() {  # once_a_day <key> <message>
  local stamp="$STATE_DIR/alert-$1" today; today="$(date -u +%Y-%m-%d)"
  [ "$(cat "$stamp" 2>/dev/null || true)" = "$today" ] && return 0
  echo "$today" > "$stamp"; tg_send "$2"
}

if health_ok "$HEALTH_URL"; then
  echo 0 > "$FAILS"
  if [ -f "$DOWN" ]; then rm -f "$DOWN"; tg_send "✅ снова отвечает: $HEALTH_URL"; fi
else
  n=$(( $(cat "$FAILS" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$FAILS"
  if [ "$n" -ge 2 ] && [ ! -f "$DOWN" ]; then
    touch "$DOWN"; tg_send "🔴 не отвечает: $HEALTH_URL (2 проверки подряд)"
  fi
fi

use="$(df -P / | awk 'NR==2{gsub("%","",$5); print $5}')"
[ "${use:-0}" -le "${DISK_ALERT_PCT:-85}" ] || once_a_day disk "диск заполнен на ${use}%"

case "$HEALTH_URL" in
  https://*)
    host="$(printf '%s' "$HEALTH_URL" | sed -E 's#https://([^/:]+).*#\1#')"
    if ! echo | openssl s_client -servername "$host" -connect "$host:443" 2>/dev/null \
        | openssl x509 -noout -checkend $((14 * 86400)) >/dev/null 2>&1; then
      once_a_day cert "TLS-сертификат $host истекает меньше чем через 14 дней (или не читается)"
    fi ;;
esac
