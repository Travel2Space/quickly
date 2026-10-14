#!/usr/bin/env bash
# Daily: row-count manifest + pg_dump → age (public key) → R2 quickly/YYYY-MM-DD/; prune old days.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"
load_env

fail() { log "backup FAILED: $1"; tg_send "бэкап НЕ удался: $1"; exit 1; }
trap 'fail "ошибка в строке $LINENO"' ERR

PGU="${POSTGRES_USER:-quickly}"; PGD="${POSTGRES_DB:-quickly}"
DAY="$(date -u +%Y-%m-%d)"; KEY="quickly/$DAY"

# Manifest BEFORE the dump, growing tables only: the restore drill checks restored >= manifest.
MANIFEST="$(compose exec -T db psql -U "$PGU" -d "$PGD" -At -c \
  "select json_build_object('lead',(select count(*) from lead),'campaign',(select count(*) from campaign),
   'campaign_lead',(select count(*) from campaign_lead),'email_log',(select count(*) from email_log),'dumped_at',now())")"

compose exec -T db pg_dump -U "$PGU" -d "$PGD" -Fc \
  | age -r "$BACKUP_AGE_PUBLIC_KEY" \
  | aws_r2 s3 cp - "s3://$R2_BUCKET/$KEY/dump.pgc.age" --only-show-errors
printf '%s\n' "$MANIFEST" | aws_r2 s3 cp - "s3://$R2_BUCKET/$KEY/manifest.json" --only-show-errors

SIZE="$(aws_r2 s3api head-object --bucket "$R2_BUCKET" --key "$KEY/dump.pgc.age" --query ContentLength --output text)"
[ "${SIZE:-0}" -ge 1024 ] || fail "дамп подозрительно мал (${SIZE:-0} байт)"

CUTOFF="$(days_ago "${RETENTION_DAYS:-30}")"
aws_r2 s3 ls "s3://$R2_BUCKET/quickly/" | awk '$1=="PRE"{sub("/","",$2); print $2}' | while read -r day; do
  if [[ "$day" < "$CUTOFF" ]]; then
    aws_r2 s3 rm --recursive --only-show-errors "s3://$R2_BUCKET/quickly/$day/" </dev/null  # keep the loop's stdin
    log "pruned $day"
  fi
done

date -u +%Y-%m-%dT%H:%M:%SZ > "$STATE_DIR/last-backup-ok"
log "backup ok: s3://$R2_BUCKET/$KEY/ ($SIZE bytes)"
