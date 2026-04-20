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

# Keep the local `:latest` tag aligned with the freshly pulled image so
# manual `docker compose up -d` (which resolves APP_IMAGE from .env, where
# we pin `:latest`) doesn't run stale code. GHA always passes APP_IMAGE
# as the SHA tag so this is purely for out-of-band restarts.
local_latest="${APP_IMAGE%:*}:latest"
if [[ "$APP_IMAGE" != "$local_latest" ]]; then
    docker tag "$APP_IMAGE" "$local_latest"
fi

echo "[deploy] docker compose up"
docker compose -f docker-compose.yml --env-file .env up -d --remove-orphans

echo "[deploy] prune"
docker image prune -f
echo "[deploy] done"
