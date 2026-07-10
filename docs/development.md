# Development environment

This repository includes a devcontainer and two Home Assistant test modes.
The integration targets Home Assistant `2026.3.0+`.

## Open the devcontainer

Open the repository in VS Code and choose **Dev Containers: Reopen in Container**.

The devcontainer includes:

- Python 3.13
- Docker-in-Docker
- GitHub CLI
- pytest
- ruff
- pytest-homeassistant-custom-component
- Pillow for generating brand assets

## Fast source-mounted Home Assistant test

This starts Home Assistant and mounts the local integration directly into `/config/custom_components/ebay`.

```bash
.devcontainer/start-ha-source.sh
```

Open:

```text
http://localhost:8123
```

This mode is best for day-to-day integration development. It does not test the HACS installation path.

## HACS custom-repository installation test

This starts Home Assistant without mounting the local integration, installs HACS into the dev config volume, and lets you test the user-facing HACS install path.

```bash
.devcontainer/start-ha-hacs.sh
```

Then:

1. Complete Home Assistant onboarding.
2. Go to **Settings -> Devices & services -> Add integration -> HACS**.
3. Complete HACS setup.
4. Open HACS.
5. Add a custom repository:
   - Repository: `https://github.com/andrewtryder/ha-ebay`
   - Category: `Integration`
6. Download the eBay integration.
7. Restart Home Assistant.
8. Add the eBay integration from **Settings -> Devices & services**.

## Stop Home Assistant

```bash
.devcontainer/stop-ha.sh
```

## Notes

The HACS test mode needs the GitHub repository to be public, or HACS needs access to the private repository through a configured GitHub token.

Home Assistant `2026.3.0+` loads local custom integration brand assets from:

```text
custom_components/ebay/brand/
```

Regenerate those assets with:

```bash
python -m pip install pillow
python scripts/prepare_brand_assets.py
```

On macOS/Linux, make scripts executable after checkout if needed:

```bash
chmod +x .devcontainer/*.sh
```
