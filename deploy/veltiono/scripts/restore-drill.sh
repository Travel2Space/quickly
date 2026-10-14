#!/usr/bin/env bash
# Monthly, by hand: newest dump → decrypt with the PRIVATE key read from stdin (never stored)
# → throwaway Postgres → row counts must be >= the manifest taken before the dump.
#   Usage: bash restore-drill.sh            (paste AGE-SECRET-KEY-… and press Enter)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"
load_env

DRILL=qk-restore-drill
KEYFILE="$(mktemp)"; DUMP="$(mktemp)"
cleanup() { rm -f "$KEYFILE" "$DUMP"; docker rm -f "$DRILL" >/dev/null 2>&1 || true; }
trap cleanup EXIT

read -rs AGEKEY   # silent: the private key must not land in the terminal scrollback
printf '%s\n' "$AGEKEY" > "$KEYFILE"; chmod 600 "$KEYFILE"; unset AGEKEY

DAY="$(find_backup)" || { echo "no complete backup (with manifest.json) found" >&2; exit 1; }
MANIFEST="$(aws_r2 s3 cp "s3://$R2_BUCKET/quickly/$DAY/manifest.json" - </dev/null)"
aws_r2 s3 cp "s3://$R2_BUCKET/quickly/$DAY/dump.pgc.age" - </dev/null | age -d -i "$KEYFILE" > "$DUMP"

docker run -d --name "$DRILL" -e POSTGRES_PASSWORD=drill postgres:16-alpine >/dev/null
# TCP, not the socket: the image's temporary init server answers on the socket, then restarts.
for _ in $(seq 60); do docker exec "$DRILL" pg_isready -h 127.0.0.1 -U postgres >/dev/null 2>&1 && break; sleep 1; done
docker exec -i "$DRILL" pg_restore -U postgres -d postgres --no-owner --exit-on-error < "$DUMP"

status=0
for t in lead campaign campaign_lead email_log; do
  want="$(printf '%s' "$MANIFEST" | sed -n "s/.*\"$t\" *: *\([0-9]*\).*/\1/p")"
  got="$(docker exec "$DRILL" psql -U postgres -d postgres -At -c "select count(*) from $t")"
  if [ "${got:-0}" -ge "${want:-0}" ]; then log "$t: $got >= $want ok"; else log "$t: $got < $want MISMATCH"; status=1; fi
done
[ "$status" -eq 0 ] && log "drill ok ($DAY)" || { log "drill FAILED ($DAY)"; exit 1; }
