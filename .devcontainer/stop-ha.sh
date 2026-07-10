#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

docker compose -f docker-compose.ha.yml down || true
docker compose -f docker-compose.ha-mounted.yml down || true
