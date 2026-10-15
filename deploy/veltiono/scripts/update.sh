#!/usr/bin/env bash
# Usage: update.sh vX.Y.Z — backup, pull, switch, wait for healthy; roll back on any failure.
set -eEuo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"
load_env

NEW="${1:-}"
[[ "$NEW" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "usage: update.sh vX.Y.Z ('latest' is refused)" >&2; exit 2; }
OLD="$QUICKLY_IMAGE_TAG"

set_tag() {  # .env for the next runs, the exported var for this one (it would override .env)
  sed -i "s/^QUICKLY_IMAGE_TAG=.*/QUICKLY_IMAGE_TAG=$1/" "$DEPLOY_DIR/.env"
  export QUICKLY_IMAGE_TAG="$1"
}

IMAGE="${QUICKLY_IMAGE:-ghcr.io/travel2space/quickly}"
WAIT="${UPDATE_HEALTH_TIMEOUT:-120}"
# Nothing has changed yet: a failed backup (it alerts by itself) just cancels the update.
bash "$HERE/backup.sh" || { tg_send "обновление до $NEW отменено: бэкап не удался, остаёмся на $OLD"; exit 1; }
BACKUP="$(cat "$STATE_DIR/last-backup-folder")"
# An image already present locally needs no pull (this also lets the local test build one).
if ! docker image inspect "$IMAGE:$NEW" >/dev/null 2>&1 && ! docker pull -q "$IMAGE:$NEW" >/dev/null 2>&1; then
  tg_send "обновление до $NEW отменено: образ не найден, остаёмся на $OLD"; exit 1
fi

rollback() {  # any failure after this point (ERR trap, or the new version not turning healthy)
  trap - ERR
  set +e
  set_tag "$OLD"
  if compose up -d app && wait_healthy "$WAIT"; then state="прежняя версия здорова"; else state="прежняя версия тоже НЕ здорова — нужен человек"; fi
  tg_send "обновление до $NEW не удалось, образ откатан на $OLD ($state). Бэкап перед обновлением: quickly/$BACKUP. Откат образа НЕ отменяет миграции БД новой версии: если приложение или данные ведут себя странно, восстановите этот бэкап (bash scripts/restore-prod.sh $BACKUP)."
  exit 1
}
trap rollback ERR

set_tag "$NEW"
compose up -d app
wait_healthy "$WAIT" || rollback
trap - ERR
tg_send "обновлено: $OLD → $NEW (бэкап перед обновлением: quickly/$BACKUP)"; log "updated $OLD -> $NEW"
