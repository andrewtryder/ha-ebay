# eBay for Home Assistant

<img src="custom_components/ebay/brand/logo.png" alt="eBay integration logo" width="180">

Keep an eye on your eBay buying and selling activity from Home Assistant. This community integration reads your account data; it does not place bids, buy items, edit listings, send messages, or perform shipping actions.

[![Latest release](https://img.shields.io/github/v/release/andrewtryder/ha-ebay?style=flat-square)](https://github.com/andrewtryder/ha-ebay/releases/latest)
[![Tests on main](https://img.shields.io/github/actions/workflow/status/andrewtryder/ha-ebay/test.yml?branch=main&style=flat-square&label=tests)](https://github.com/andrewtryder/ha-ebay/actions/workflows/test.yml)
[![License](https://img.shields.io/github/license/andrewtryder/ha-ebay?style=flat-square)](LICENSE)
[![HACS custom integration](https://img.shields.io/badge/HACS-Custom-orange?style=flat-square)](https://my.home-assistant.io/redirect/hacs_repository/?owner=andrewtryder&repository=ha-ebay&category=integration)

> [!IMPORTANT]
> This is an unofficial community integration. It is not affiliated with, endorsed by, or sponsored by eBay Inc.

## What can I do with it?

- Track watched items, bids, ending times, and changes in price.
- Monitor active listings and recent sales. Optional seller features include orders, feedback, messages, and account health, where supported.
- Create notifications for events such as being outbid or an item ending soon. Start with the [automation blueprints](docs/blueprints.md).
- Add summary sensors, calendars, and recent activity to a dashboard.

## Requirements and compatibility

- Home Assistant **2026.10.0 or newer**, with HACS (or the built-in Home Assistant Marketplace in **2026.11+**).
- An eBay account and an **eBay Developer Program application**. You need the application's App ID, Cert ID, and eBay Redirect URL name (RuName).
- Production credentials for Production or Sandbox credentials for Sandbox. Available seller and analytics features vary by marketplace and the permissions granted.

See the [OAuth setup guide](docs/oauth-and-accounts.md) for the callback URL, developer application, and RuName steps.

## Install with HACS or Home Assistant Marketplace

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=andrewtryder&repository=ha-ebay&category=integration)

1. Click the **Open your Home Assistant instance** button above (backwards compatible: opens **HACS** on earlier versions and the built-in **Home Assistant Marketplace** on **2026.11+**), or manually add `https://github.com/andrewtryder/ha-ebay` as a custom repository with category **Integration**.
2. Find and install **eBay**.
3. Restart Home Assistant completely. A full restart is also required after updates.

## First-time setup

1. Go to **Settings → Devices & services → Add integration**, then select **eBay**. You can also use [Add eBay to Home Assistant](https://my.home-assistant.io/redirect/config_flow_start/?domain=ebay).
2. Choose guided setup or enter existing developer credentials. Guided setup shows the callback URL to register with eBay to obtain your RuName.
3. Enter the matching environment, App ID, Cert ID, RuName, and marketplace, then authorize the eBay account you want to monitor.
4. Enable optional seller features in the integration options when needed. Home Assistant may ask you to authorize additional permissions.

If a browser callback cannot reach your instance, the setup flow offers a manual authorization fallback. Multiple distinct accounts can share one developer application; the same account cannot be added twice in the same environment.

## What you'll see in Home Assistant

The integration provides summary sensors, activity events, calendars, and refresh buttons. Per-item sensors are disabled by default; you can enable individual sensors or pin items in the options. Activity events appear after a change is detected, and their recent history resets after a restart or reload.

Buying, selling, and active orders normally refresh on the configured interval; other sections update less often. See [advanced usage](docs/advanced-usage.md) for entity examples, refresh behavior, options, and limits.

## Common problems and troubleshooting

- **Setup or callback fails:** check the [OAuth guide](docs/oauth-and-accounts.md) and try the manual authorization fallback if needed.
- **Credentials are rejected:** check that the selected Production or Sandbox environment matches the keyset.
- **Optional data is missing:** enable the relevant feature and complete any requested reauthorization. Some marketplaces do not support every feature.
- **Refresh button reports an error:** wait for the cooldown or an in-progress refresh to finish.
- **New features do not appear after updating:** restart Home Assistant. Check **Settings → Repairs** for additional guidance.

When reporting a problem, do not include client secrets, authorization codes, access tokens, or refresh tokens.

## Limitations, safety, and privacy

The integration is read-only, but account and listing information can appear in Home Assistant states, history, backups, logs, or diagnostics. Credentials and OAuth tokens are intended to be redacted from diagnostics and logs. Read the [privacy statement](PRIVACY.md) before sharing diagnostic information.

Some optional features need additional permissions. Disabling a feature stops its API calls but does not revoke previously granted permissions. Recent event history is memory-only. Removing the integration deletes the local entry and tokens; revoke authorization in your eBay account separately if desired.

## More documentation

- [Advanced usage and limitations](docs/advanced-usage.md)
- [OAuth and account setup](docs/oauth-and-accounts.md)
- [Automation blueprints](docs/blueprints.md)
- [API architecture](docs/api-architecture.md) and [rate limits](docs/reliability-and-rate-limits.md)
- [Development](docs/development.md) and [contributing](CONTRIBUTING.md)

## Support and license

Open a [GitHub issue](https://github.com/andrewtryder/ha-ebay/issues) for reproducible problems. This project is distributed under the [license](LICENSE).

eBay is a trademark of eBay Inc. The eBay name and logo identify the connected service; this project is not an official eBay product.
