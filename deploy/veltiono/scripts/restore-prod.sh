#!/usr/bin/env bash
# DESTRUCTIVE: replace the live database with a backup — but only after the backup is proven.
#   Usage: restore-prod.sh [YYYY-MM-DD|folder]   (default: newest COMPLETE backup, i.e. with
#   manifest.json; a day picks that day's newest, a folder name picks exactly it). Type RESTORE, then paste the age
#   private key (silent, never stored). Steps:
#     1. validate: stream R2 → age → pg_restore -l (wrong key / corrupt dump stops here, nothing touched)
#     2. app stopped, safety dump of the live DB → $STATE_DIR/pre-restore-<UTC>.pgc.age (public key)
#     3. restore into a NEW database <db>_restore
#     4. only then: drop <db>, rename <db>_restore → <db>, start the app
#   Any failure drops <db>_restore and starts the app on the untouched DB.
#   No plaintext dump and no key file ever touch the disk.
set -eEuo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"
load_env

PGU="${POSTGRES_USER:-quickly}"; PGD="${POSTGRES_DB:-quickly}"; NEWDB="${PGD}_restore"
DAY="$(find_backup "${1:-}")" || { echo "no complete backup (with manifest.json) found${1:+ for $1}" >&2; exit 1; }
SRC="s3://$R2_BUCKET/quickly/$DAY/dump.pgc.age"

printf 'This REPLACES database %s with the backup of %s. Type RESTORE to continue: ' "$PGD" "$DAY" >&2
read -r answer
[ "$answer" = RESTORE ] || { echo "aborted" >&2; exit 1; }
printf 'age private key: ' >&2
read -rs KEY; echo >&2

SAFETY=""; SWAP=0
dbexec() { compose exec -T db "$@"; }
on_error() {
  local line="$1"
  trap - ERR
  set +e
  dbexec dropdb -U "$PGU" --if-exists --force "$NEWDB" >/dev/null 2>&1
  compose start app >/dev/null 2>&1
  tg_send "восстановление из $DAY НЕ удалось (строка $line); приложение запущено на прежней базе${SAFETY:+; страховочная копия: $SAFETY}"
  if [ "$SWAP" = 1 ]; then
    tg_send "ВНИМАНИЕ: сбой при подмене базы, живая база могла быть удалена; восстановите из ${SAFETY:-страховочной копии}"
    echo "restore FAILED during the swap; live DB may be gone, safety dump: $SAFETY" >&2
  else
    echo "restore FAILED at line $line; live database untouched, app restarted" >&2
  fi
  exit 1
}
trap 'on_error $LINENO' ERR

# 1. Prove the backup decrypts and parses. pg_restore -l reads only the table of contents, so
#    the rest of the stream is drained: that also makes age check every chunk's authentication.
aws_r2 s3 cp "$SRC" - </dev/null \
  | age -d -i <(printf '%s\n' "$KEY") \
  | dbexec sh -c 'pg_restore -l >/dev/null; rc=$?; cat >/dev/null; exit $rc'
log "backup $DAY validated"

# 2. App stopped first, so the safety dump is the last word on the live data.
compose stop app
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"; SAFETY="$STATE_DIR/pre-restore-$STAMP.pgc.age"
dbexec pg_dump -U "$PGU" -d "$PGD" -Fc | age -r "$BACKUP_AGE_PUBLIC_KEY" > "$SAFETY"
[ "$(wc -c < "$SAFETY")" -ge 512 ] || { rm -f "$SAFETY"; SAFETY=""; false; }
log "safety dump: $SAFETY"

# 3. Restore next to the live DB.
dbexec dropdb -U "$PGU" --if-exists --force "$NEWDB"
dbexec createdb -U "$PGU" "$NEWDB"
aws_r2 s3 cp "$SRC" - </dev/null \
  | age -d -i <(printf '%s\n' "$KEY") \
  | dbexec pg_restore -U "$PGU" -d "$NEWDB" --no-owner --exit-on-error
unset KEY

# 4. Swap. From here the live DB is gone until the rename lands; the safety dump covers a failure.
SWAP=1
dbexec dropdb -U "$PGU" --force "$PGD"
dbexec psql -U "$PGU" -d postgres -v ON_ERROR_STOP=1 -c "ALTER DATABASE \"$NEWDB\" RENAME TO \"$PGD\""
trap - ERR
compose start app
if wait_healthy "${UPDATE_HEALTH_TIMEOUT:-120}"; then
  log "restored $DAY"; tg_send "база восстановлена из бэкапа $DAY; страховочная копия прежней базы: $SAFETY"
else
  tg_send "восстановление из $DAY: приложение не поднялось — нужен человек; страховочная копия: $SAFETY"; exit 1
fi
