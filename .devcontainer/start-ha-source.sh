#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

mkdir -p ha-config/custom_components

echo "Starting Home Assistant with custom_components/ebay mounted read-only."
echo "Use this for fast local integration development. It bypasses HACS installation."
docker compose -f docker-compose.ha-mounted.yml up -d

cat <<'EOF'

Home Assistant is starting at:
  http://localhost:8123

Source-mounted dev mode:
  - The local custom_components/ebay directory is mounted into /config/custom_components/ebay.
  - Restart Home Assistant after code changes that affect integration setup/imports.
  - HACS is not required for this mode.

EOF
