#!/usr/bin/env bash
set -euo pipefail

# Runs on the Hetzner host, called from CI via SSH.
# Env expected: APP_IMAGE (ghcr.io/…:<sha>).

cd "$(dirname "$0")"
: "${APP_IMAGE:?APP_IMAGE not set}"

export APP_IMAGE

echo "[deploy] pulling $APP_IMAGE"
if [[ -n "${GHCR_TOKEN:-}" ]]; then
    echo "$GHCR_TOKEN" | docker login ghcr.io -u "${GHCR_USER:-token}" --password-stdin >/dev/null
fi
docker pull "$APP_IMAGE"

echo "[deploy] docker compose up"
docker compose -f docker-compose.yml --env-file .env up -d --remove-orphans

echo "[deploy] prune"
docker image prune -f
echo "[deploy] done"
