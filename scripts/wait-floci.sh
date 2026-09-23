#!/usr/bin/env bash
# make up, step 2: block until Floci answers its health endpoint.
set -euo pipefail

FLOCI_URL="${FLOCI_URL:-http://localhost:4566}"
TIMEOUT_S="${TIMEOUT_S:-90}"

printf "waiting for floci at %s " "$FLOCI_URL"
for _ in $(seq "$TIMEOUT_S"); do
  if curl -sf "$FLOCI_URL/_localstack/health" >/dev/null; then
    echo "ready"
    exit 0
  fi
  printf "."
  sleep 1
done

echo "timed out after ${TIMEOUT_S}s; see: docker compose logs floci"
exit 1
