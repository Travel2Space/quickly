#!/usr/bin/env bash
# Shared helpers for the Quickly deploy scripts. Source it; do not run it.
# Data moves through stdin/stdout (no bind mounts), so the same scripts work on the server
# and from a container that talks to the Docker socket (deploy/veltiono/test/run.sh).
set -euo pipefail

# cron's default PATH is /usr/bin:/bin: no age/docker in /usr/local/bin. Same value as cron/quickly.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

DEPLOY_DIR="${DEPLOY_DIR:-/opt/quickly}"
STATE_DIR="${STATE_DIR:-$DEPLOY_DIR/state}"
mkdir -p "$STATE_DIR"

load_env() {
  local f
  for f in .env ops.env; do
    [ -f "$DEPLOY_DIR/$f" ] || { echo "missing $DEPLOY_DIR/$f" >&2; exit 2; }
  done
  cd "$DEPLOY_DIR"   # cron starts in /; COMPOSE_FILE paths are relative to this directory
  set -a
  # shellcheck disable=SC1091
  . "$DEPLOY_DIR/.env"
  # ops.env: backup/R2/Telegram secrets, kept out of the app container's env_file.
  # shellcheck disable=SC1091
  . "$DEPLOY_DIR/ops.env"
  set +a
}

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# Best effort: an alert must never break the script that raises it.
tg_send() {
  local api="${TELEGRAM_API_URL:-https://api.telegram.org}"
  if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_OPS_CHAT_ID:-}" ]; then
    log "telegram not configured: $1"
    return 0
  fi
  curl -fsS -m 10 -X POST "$api/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_OPS_CHAT_ID}" \
    --data-urlencode "text=[quickly] $1" >/dev/null || log "telegram send failed"
}

# COMPOSE_FILE in .env picks the proxy overlay.
compose() { docker compose --project-directory "$DEPLOY_DIR" "$@"; }

# aws-cli in a throwaway container; stdin/stdout pass through (`s3 cp - …`, `s3 cp … -`).
# `-i` forwards stdin: every call that does NOT upload from stdin gets `</dev/null`, or it eats
# the caller's input (a `while read` loop, a terminal prompt).
# Credentials go in by name only (`-e VAR`), never as visible `docker run` arguments.
aws_r2() {
  AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" \
  AWS_DEFAULT_REGION="${R2_REGION:-auto}" \
  docker run --rm -i --network "${AWS_NETWORK:-host}" \
    -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_DEFAULT_REGION \
    "${AWS_CLI_IMAGE:-amazon/aws-cli:2.22.35}" \
    --endpoint-url "${R2_ENDPOINT:-https://${R2_ACCOUNT_ID}.eu.r2.cloudflarestorage.com}" "$@"
}

# The image HEALTHCHECK (GET /health) must report healthy within $1 seconds.
wait_healthy() {
  local cid deadline
  deadline=$(( $(date +%s) + $1 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    cid="$(compose ps -q app)"
    if [ -n "$cid" ] && [ "$(docker inspect -f '{{.State.Health.Status}}' "$cid" 2>/dev/null)" = healthy ]; then
      return 0
    fi
    sleep 3
  done
  return 1
}

# UTC date N days ago, portable across GNU and busybox date.
days_ago() { date -u -d "@$(( $(date +%s) - $1 * 86400 ))" +%Y-%m-%d; }

# True when $1 answers {"ok": true}. No `curl | grep -q`: SIGPIPE under pipefail.
health_ok() {
  local body
  body="$(curl -fsS -m 10 "$1" 2>/dev/null || true)"
  case "$body" in *'"ok":true'*|*'"ok": true'*) return 0 ;; *) return 1 ;; esac
}

# Newest backup folder under quickly/ that HAS manifest.json (the completion marker backup.sh
# writes last), optionally limited to names starting with $1 (a day, YYYY-MM-DD, or a full
# folder name). Prints the folder name; exit 1 when there is none.
find_backup() {
  local want="${1:-}" f
  while read -r f; do
    case "$f" in "$want"*) ;; *) continue ;; esac
    if aws_r2 s3api head-object --bucket "$R2_BUCKET" --key "quickly/$f/manifest.json" </dev/null >/dev/null 2>&1; then
      printf '%s\n' "$f"; return 0
    fi
  done < <(aws_r2 s3 ls "s3://$R2_BUCKET/quickly/" </dev/null | awk '$1=="PRE"{sub("/","",$2); print $2}' | sort -r)
  return 1
}
