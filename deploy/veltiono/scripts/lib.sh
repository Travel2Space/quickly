#!/usr/bin/env bash
# Shared helpers for the Quickly deploy scripts. Source it; do not run it.
# Data moves through stdin/stdout (no bind mounts), so the same scripts work on the server
# and from a container that talks to the Docker socket (deploy/veltiono/test/run.sh).
set -euo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-/opt/quickly}"
STATE_DIR="${STATE_DIR:-$DEPLOY_DIR/state}"
mkdir -p "$STATE_DIR"

load_env() {
  [ -f "$DEPLOY_DIR/.env" ] || { echo "missing $DEPLOY_DIR/.env" >&2; exit 2; }
  cd "$DEPLOY_DIR"   # cron starts in /; COMPOSE_FILE paths are relative to this directory
  set -a
  # shellcheck disable=SC1091
  . "$DEPLOY_DIR/.env"
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
