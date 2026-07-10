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

## Local Python 3.13 environment

The repository targets Python 3.13, matching CI and the devcontainer. On macOS with Homebrew Python installed:

```bash
/opt/homebrew/bin/python3.13 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-dev.txt
```

Run the fast local checks with:

```bash
.venv/bin/python -m ruff check custom_components tests
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall custom_components
```

Install the heavier Home Assistant pytest harness only when writing tests that need Home Assistant fixtures:

```bash
.venv/bin/python -m pip install -r requirements-ha.txt
```

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

### Automatic OAuth callback test

The primary OAuth path should work through Home Assistant's normal external
callback redirect:

```text
https://my.home-assistant.io/redirect/oauth
```

1. Start source-mounted Home Assistant with `.devcontainer/start-ha-source.sh`.
2. Open `http://localhost:8123` and complete onboarding if needed.
3. Go to **Settings -> Devices & services -> Add integration -> eBay**.
4. Copy the callback URL shown by the eBay setup form.
5. In the eBay Developer Program application keys page, open or create an OAuth redirect URL.
6. Set both Auth Accepted URL and Auth Declined URL to the copied callback URL.
7. Save the redirect configuration and copy the generated RuName.
8. Enter the matching production or sandbox Client ID, Client secret, RuName, environment, and Site ID in Home Assistant.
9. Keep **Automatic callback** selected and click authorize.
10. Approve access at eBay.
11. Confirm the browser returns to the dev Home Assistant instance, the config entry is created, and the first refresh succeeds.

No Cloudflare tunnel, public Home Assistant URL, SSH access, or port forwarding
is required for this primary test path.

### Manual OAuth fallback test

To test the advanced fallback, start adding eBay again with a separate eBay
developer app credential set, choose manual authorization, open the consent URL
shown by Home Assistant, approve access at eBay, and paste either the final
callback URL or the authorization code into the setup flow. Prefer pasting the
full callback URL when available so OAuth state validation is exercised.

## HACS custom-repository installation test

This starts Home Assistant without mounting the local integration, installs HACS into the dev config volume, and lets you test the user-facing HACS install path.

The dev Home Assistant image is pinned to `2026.3.0` to match the integration baseline in `hacs.json`.

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

### HACS troubleshooting

If adding HACS shows:

```text
Config flow could not be loaded: {"message":"Invalid handler specified"}
```

that usually means the HACS package is missing or failed to import. Check the container logs:

```bash
docker logs ha-ebay-homeassistant 2>&1 | rg hacs
```

Verify the install files exist on the host:

```bash
ls .devcontainer/ha-config/custom_components/hacs/manifest.json
ls .devcontainer/ha-config/custom_components/hacs/__init__.py
```

If those files are missing, or `home-assistant_v2.db` is `0` bytes with recorder errors in the logs, reset the dev config and rerun the script:

```bash
.devcontainer/stop-ha.sh
rm -rf .devcontainer/ha-config/*
.devcontainer/start-ha-hacs.sh
```

After Home Assistant restarts, hard-refresh the browser before adding the HACS integration.

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
