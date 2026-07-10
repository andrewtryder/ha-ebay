#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

COMPOSE_FILE="docker-compose.ha.yml"
HA_URL="http://127.0.0.1:8123"
WAIT_TIMEOUT_SECONDS=180

wait_for_ha_ready() {
  local label="$1"
  local deadline=$((SECONDS + WAIT_TIMEOUT_SECONDS))

  echo "Waiting for Home Assistant to be ready (${label})..."
  while (( SECONDS < deadline )); do
    if docker compose -f "${COMPOSE_FILE}" exec -T homeassistant test -f /config/.HA_VERSION; then
      if curl -fsS "${HA_URL}/" >/dev/null 2>&1; then
        return 0
      fi
    fi
    sleep 3
  done

  echo "Timed out waiting for Home Assistant (${label})." >&2
  echo "Expected /config/.HA_VERSION in the container and a response from ${HA_URL}/." >&2
  return 1
}

verify_hacs_files() {
  local missing=0

  for path in \
    /config/custom_components/hacs/manifest.json \
    /config/custom_components/hacs/__init__.py; do
    if ! docker compose -f "${COMPOSE_FILE}" exec -T homeassistant test -f "${path}"; then
      echo "Missing required HACS file: ${path}" >&2
      missing=1
    fi
  done

  if (( missing )); then
    echo "HACS installation verification failed." >&2
    return 1
  fi

  return 0
}

mkdir -p ha-config/custom_components

echo "Starting Home Assistant without mounting the local integration."
echo "Use this mode to test installation through HACS as a custom repository."
docker compose -f "${COMPOSE_FILE}" up -d

wait_for_ha_ready "before HACS install"

echo "Installing HACS into the dev Home Assistant config volume..."
docker compose -f "${COMPOSE_FILE}" exec -T homeassistant bash -lc 'wget -O - https://get.hacs.xyz | bash -'

verify_hacs_files

echo "Restarting Home Assistant..."
docker compose -f "${COMPOSE_FILE}" restart homeassistant

wait_for_ha_ready "after restart"
verify_hacs_files

HA_VERSION="$(docker compose -f "${COMPOSE_FILE}" exec -T homeassistant cat /config/.HA_VERSION | tr -d '\r\n')"

cat <<EOF

Home Assistant ${HA_VERSION} is ready at:
  ${HA_URL}

Verified HACS files:
  ha-config/custom_components/hacs/manifest.json
  ha-config/custom_components/hacs/__init__.py

Manual HACS install test:
  1. Complete Home Assistant onboarding.
  2. Settings -> Devices & services -> Add integration -> HACS.
  3. Complete HACS setup.
  4. HACS -> Custom repositories.
  5. Add:
       Repository: https://github.com/andrewtryder/ha-ebay
       Category: Integration
  6. Download the eBay integration.
  7. Restart Home Assistant.
  8. Settings -> Devices & services -> Add integration -> eBay.

EOF
