#!/usr/bin/env bash
# Local check of the deploy kit (spec D1–D6) against MinIO (stands in for R2) and a fake
# Telegram. Needs only Docker. Scripts run in a docker-cli container that drives the host
# daemon through its socket — the same way they run on the server.
set -euo pipefail
export MSYS_NO_PATHCONV=1                                     # Git Bash on Windows
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
KIT="$ROOT/deploy/veltiono"
hostpath() { if command -v cygpath >/dev/null; then cygpath -m "$1"; else printf '%s' "$1"; fi; }
NET=qk-test; R=qk-runner
run() { docker exec -i "$R" bash -c "cd /opt/quickly && $1"; }
tg_count() { docker exec qk-faketg sh -c 'cat /tmp/tg.log 2>/dev/null | wc -l' | tr -d ' '; }
pass() { echo "PASS $1"; }
die() { echo "FAIL $1" >&2; exit 1; }

cleanup() {
  docker network disconnect quickly_default "$R" >/dev/null 2>&1 || true   # else `down` can't drop the network
  docker exec "$R" bash -c "cd /opt/quickly && docker compose down -v" >/dev/null 2>&1 || true
  docker rm -f qk-s3 qk-faketg "$R" qk-restore-drill >/dev/null 2>&1 || true
  docker rmi quickly:v0.0.2 >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$KIT/.env" "$KIT/state"
}
trap cleanup EXIT
cleanup

docker build -q -t quickly:v0.0.1 "$(hostpath "$ROOT")" >/dev/null
docker network create "$NET" >/dev/null
docker run -d --name qk-s3 --network "$NET" \
  chrislusf/seaweedfs:latest server -s3 -dir=/data >/dev/null
docker run -d --name qk-faketg --network "$NET" -v "$(hostpath "$KIT/test"):/t:ro" python:3.12-alpine python /t/fake_telegram.py >/dev/null
docker run -d --name "$R" --network "$NET" -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$(hostpath "$KIT"):/opt/quickly" docker:27-cli sleep 3600 >/dev/null
docker exec "$R" sh -c "apk add -q bash curl openssl age 2>/dev/null || apk add -q bash curl openssl age --repository=http://dl-cdn.alpinelinux.org/alpine/edge/community"

run 'age-keygen -o /tmp/age.key 2>/dev/null; pub=$(age-keygen -y /tmp/age.key)
  cat > .env <<EOF
COMPOSE_FILE=docker-compose.yml:docker-compose.nginx.yml
QUICKLY_IMAGE=quickly
QUICKLY_IMAGE_TAG=v0.0.1
QUICKLY_PORT=5059
POSTGRES_PASSWORD=ci
BASE_URL=http://localhost:5059
QUICKLY_SECRET_KEY=$(openssl rand -hex 32)
QUICKLY_ENCRYPTION_KEY=$(openssl rand -base64 32 | tr "+/" "-_")
BACKUP_AGE_PUBLIC_KEY=$pub
R2_ENDPOINT=http://qk-s3:8333
R2_REGION=us-east-1
R2_ACCOUNT_ID=unused
R2_BUCKET=test-backups
R2_ACCESS_KEY_ID=testkey
R2_SECRET_ACCESS_KEY=testsecret123
AWS_NETWORK=qk-test
RETENTION_DAYS=30
DISK_ALERT_PCT=101
UPDATE_HEALTH_TIMEOUT=60
TELEGRAM_API_URL=http://qk-faketg:8080
TELEGRAM_BOT_TOKEN=test
TELEGRAM_OPS_CHAT_ID=1
HEALTH_URL=http://app:8000/health
EOF'

# D1 — the stack starts and answers /health
run 'docker compose up -d >/dev/null 2>&1 && docker network connect quickly_default qk-runner'
run 'for i in $(seq 60); do curl -fs http://app:8000/health && exit 0; sleep 2; done; exit 1' | grep '"ok"' >/dev/null || die D1
curl -fs http://127.0.0.1:5059/health | grep '"ok"' >/dev/null && pass D1 || die "D1 loopback publish"   # from the host

# D2 — encrypted backup lands in the bucket with a manifest
run '. scripts/lib.sh; load_env; aws_r2 s3 mb "s3://$R2_BUCKET" >/dev/null'
run 'bash scripts/backup.sh' >/dev/null || die "D2 backup.sh"
run '. scripts/lib.sh; load_env; aws_r2 s3 ls --recursive "s3://$R2_BUCKET/quickly/"' | grep 'manifest.json' >/dev/null || die "D2 manifest"
run '. scripts/lib.sh; load_env; aws_r2 s3 cp "s3://$R2_BUCKET/quickly/$(date -u +%Y-%m-%d)/dump.pgc.age" - </dev/null > /tmp/dump-head.age && head -c 21 /tmp/dump-head.age' \
  | grep 'age-encryption.org/v1' >/dev/null && pass D2 || die "D2 not encrypted"

# D3 — restore drill with the private key on stdin
run 'grep AGE-SECRET-KEY /tmp/age.key | bash scripts/restore-drill.sh' | grep 'drill ok' >/dev/null && pass D3 || die D3

# D3b — streamed production restore: confirmation + key on stdin, app healthy afterwards
run '{ echo RESTORE; grep AGE-SECRET-KEY /tmp/age.key; } | bash scripts/restore-prod.sh' >/dev/null && pass D3b || die D3b

# D4 — one alert after the 2nd failed check, none on the 3rd, one on recovery
base=$(tg_count)
docker stop quickly-app-1 >/dev/null
run 'bash scripts/healthwatch.sh; bash scripts/healthwatch.sh; bash scripts/healthwatch.sh' >/dev/null
[ "$(tg_count)" -eq $((base + 1)) ] || die "D4 down alerts: $(( $(tg_count) - base ))"
docker start quickly-app-1 >/dev/null
run '. scripts/lib.sh; load_env; wait_healthy 150' || die "D4 app did not come back"
run 'bash scripts/healthwatch.sh' >/dev/null
[ "$(tg_count)" -eq $((base + 2)) ] && pass D4 || die "D4 recovery alert"

# D5 — a tag that does not exist: refused, tag unchanged, still healthy
if run 'bash scripts/update.sh v9.9.9' >/dev/null 2>&1; then die "D5 update succeeded"; fi
run 'grep -q "^QUICKLY_IMAGE_TAG=v0.0.1$" .env && . scripts/lib.sh && load_env && wait_healthy 30' && pass D5 || die D5
if run 'bash scripts/update.sh latest' >/dev/null 2>&1; then die "D5 latest accepted"; fi

# D5b — a tag that exists but never turns healthy: rolled back, alert sent
printf 'FROM busybox:1.36\nCMD ["sleep","3600"]\n' | docker build -q -t quickly:v0.0.2 - >/dev/null
base=$(tg_count)
if run 'bash scripts/update.sh v0.0.2' >/dev/null 2>&1; then die "D5b bad image accepted"; fi
run 'grep -q "^QUICKLY_IMAGE_TAG=v0.0.1$" .env && . scripts/lib.sh && load_env && wait_healthy 90' || die "D5b no rollback"
[ "$(tg_count)" -gt "$base" ] && pass D5b || die "D5b no alert"

# D6 — shellcheck
docker run --rm -v "$(hostpath "$KIT"):/k:ro" --entrypoint sh koalaman/shellcheck-alpine:stable \
  -c 'shellcheck -x -P SCRIPTDIR -S warning /k/scripts/*.sh /k/test/run.sh' && pass D6 || die D6

echo "ALL PASS"
