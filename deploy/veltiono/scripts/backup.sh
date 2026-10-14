#!/usr/bin/env bash
# Daily: row-count manifest + pg_dump → age (public key) → R2 quickly/<YYYY-MM-DDTHHMMSSZ>/.
# A run owns its folder: dump first, size check, manifest.json LAST (= "this folder is complete").
# Any failure deletes the folder's objects, so a half-written backup is never mistaken for a good one.
set -eEuo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"
load_env

PGU="${POSTGRES_USER:-quickly}"; PGD="${POSTGRES_DB:-quickly}"
FOLDER="$(date -u +%Y-%m-%dT%H%M%SZ)"; KEY="quickly/$FOLDER"

fail() {
  trap - ERR
  log "backup FAILED: $1"
  aws_r2 s3 rm --recursive --only-show-errors "s3://$R2_BUCKET/$KEY/" </dev/null >/dev/null 2>&1 || log "cleanup of $KEY failed"
  tg_send "бэкап НЕ удался: $1"
  exit 1
}
trap 'fail "ошибка в строке $LINENO"' ERR

# Manifest BEFORE the dump, growing tables only: the restore drill checks restored >= manifest.
MANIFEST="$(compose exec -T db psql -U "$PGU" -d "$PGD" -At -c \
  "select json_build_object('lead',(select count(*) from lead),'campaign',(select count(*) from campaign),
   'campaign_lead',(select count(*) from campaign_lead),'email_log',(select count(*) from email_log),'dumped_at',now())")"

compose exec -T db pg_dump -U "$PGU" -d "$PGD" -Fc \
  | age -r "$BACKUP_AGE_PUBLIC_KEY" \
  | aws_r2 s3 cp - "s3://$R2_BUCKET/$KEY/dump.pgc.age" --only-show-errors

SIZE="$(aws_r2 s3api head-object --bucket "$R2_BUCKET" --key "$KEY/dump.pgc.age" --query ContentLength --output text </dev/null)"
[ "${SIZE:-0}" -ge 1024 ] || fail "дамп подозрительно мал (${SIZE:-0} байт)"

printf '%s\n' "$MANIFEST" | aws_r2 s3 cp - "s3://$R2_BUCKET/$KEY/manifest.json" --only-show-errors

trap - ERR
date -u +%Y-%m-%dT%H:%M:%SZ > "$STATE_DIR/last-backup-ok"
printf '%s\n' "$FOLDER" > "$STATE_DIR/last-backup-folder"
log "backup ok: s3://$R2_BUCKET/$KEY/ ($SIZE bytes)"

# Retention by the date in the folder name (first 10 chars). Non-fatal: the backup above is good.
prune() {
  local cutoff folder rc=0
  cutoff="$(days_ago "${RETENTION_DAYS:-30}")"
  while read -r folder; do
    if [[ "${folder:0:10}" < "$cutoff" ]]; then
      if aws_r2 s3 rm --recursive --only-show-errors "s3://$R2_BUCKET/quickly/$folder/" </dev/null; then
        log "pruned $folder"
      else
        rc=1
      fi
    fi
  done < <(aws_r2 s3 ls "s3://$R2_BUCKET/quickly/" </dev/null | awk '$1=="PRE"{sub("/","",$2); print $2}')
  return "$rc"
}
prune || log "prune failed (non-fatal)"
