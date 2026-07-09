#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

mkdir -p ha-config/custom_components

echo "Starting Home Assistant without mounting the local integration."
echo "Use this mode to test installation through HACS as a custom repository."
docker compose -f docker-compose.ha.yml up -d

echo "Waiting for Home Assistant container to initialize..."
sleep 15

echo "Installing HACS into the dev Home Assistant config volume..."
docker compose -f docker-compose.ha.yml exec -T homeassistant bash -lc 'wget -O - https://get.hacs.xyz | bash -'

echo "Restarting Home Assistant..."
docker compose -f docker-compose.ha.yml restart homeassistant

cat <<'EOF'

Home Assistant is starting at:
  http://localhost:8123

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
