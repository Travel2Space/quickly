#!/usr/bin/env bash
# DESTRUCTIVE: replace the live database with a backup.
#   Usage: restore-prod.sh [YYYY-MM-DD]   (default: newest). Type RESTORE, then paste the age
#   private key (silent, never stored). The dump streams R2 → age → pg_restore; no plaintext file.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"
load_env

PGU="${POSTGRES_USER:-quickly}"; PGD="${POSTGRES_DB:-quickly}"
DAY="${1:-$(aws_r2 s3 ls "s3://$R2_BUCKET/quickly/" </dev/null | awk '$1=="PRE"{sub("/","",$2); print $2}' | sort | tail -1)}"
[ -n "$DAY" ] || { echo "no backups found" >&2; exit 1; }

printf 'This REPLACES database %s with the backup of %s. Type RESTORE to continue: ' "$PGD" "$DAY" >&2
read -r answer
[ "$answer" = RESTORE ] || { echo "aborted" >&2; exit 1; }
KEYFILE="$(mktemp)"; trap 'rm -f "$KEYFILE"' EXIT
printf 'age private key: ' >&2
read -rs key; echo >&2
printf '%s\n' "$key" > "$KEYFILE"; chmod 600 "$KEYFILE"; unset key

compose stop app
compose exec -T db dropdb -U "$PGU" --if-exists "$PGD"
compose exec -T db createdb -U "$PGU" "$PGD"
aws_r2 s3 cp "s3://$R2_BUCKET/quickly/$DAY/dump.pgc.age" - </dev/null \
  | age -d -i "$KEYFILE" \
  | compose exec -T db pg_restore -U "$PGU" -d "$PGD" --no-owner --exit-on-error
compose start app
if wait_healthy "${UPDATE_HEALTH_TIMEOUT:-120}"; then
  log "restored $DAY"; tg_send "база восстановлена из бэкапа $DAY"
else
  tg_send "восстановление из $DAY: приложение не поднялось — нужен человек"; exit 1
fi
