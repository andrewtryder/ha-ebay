<p align="center">
  <img src="https://upload.wikimedia.org/wikipedia/commons/4/48/EBay_logo.png" alt="eBay logo" width="220">
</p>

# eBay for Home Assistant

A HACS custom integration that brings read-only eBay activity into Home Assistant.
Monitor watched items, bidding, active listings, seller operations, and account health
through sensors, event entities, calendars, and automations.

> [!IMPORTANT]
> This is an unofficial community integration. It is not affiliated with, endorsed by,
> or sponsored by eBay Inc.

Requires Home Assistant `2026.3.0+`.

## Features

- Buying and watching: watched items, bids, winning/outbid status, price changes, and ending-soon alerts
- Selling: active listings, bids, offers, watchers, questions, views, and quantity sold
- Optional seller operations: orders, fulfillment, payment disputes, seller standards, feedback, and buyer messages
- Directional and threshold-based watched-price events
- Calendars for watched, bidding, and selling end times
- Bounded recent activity on grouped event entities
- Optional per-item sensors with configurable selection and caps
- Manual refresh and disabled-by-default diagnostic health sensors

## Installation

1. In HACS, add `https://github.com/andrewtryder/ha-ebay` as a custom **Integration** repository.
2. Install **eBay**.
3. Restart Home Assistant.
4. Go to **Settings → Devices & services → Add integration → eBay**.

A full Home Assistant restart is required after updating the integration through HACS.

## eBay Developer Setup

You need an eBay Developer Program application with:

- App ID / Client ID
- Cert ID / Client secret
- RuName
- Production or Sandbox environment
- Site ID (`0` for eBay US)

Start the integration setup in Home Assistant first, copy the callback URL it provides,
and register that URL in the matching eBay application keyset.

See the illustrated **[eBay OAuth setup guide](docs/ebay-oauth-setup.md)** for the complete walkthrough.

Production credentials must be used with Production, and Sandbox credentials with Sandbox.
The integration currently supports one eBay account per developer-app credential set.

Existing installations may need to reauthorize after an update that adds new optional eBay
API scopes. Disabling a feature stops its API calls but does not revoke previously granted scopes.

## Home Assistant Entities

The integration creates stable summary sensors and grouped activity entities:

- `event.ebay_watching_activity`
- `event.ebay_bidding_activity`
- `event.ebay_selling_activity`
- `event.ebay_seller_ops_activity`
- `calendar.ebay_watched_items`
- `calendar.ebay_bidding_items`
- `calendar.ebay_selling_items`
- `button.ebay_refresh`

Activity entities remain `Unknown` until their first detected event. Their `recent_events`
attribute keeps a bounded, memory-only history that resets after a restart or integration reload.

Diagnostic health sensors are created disabled by default. Enable them from the entity registry
when troubleshooting refresh failures, API warnings, truncation, or ending-soon timers.

## Configuration

Options include:

- Poll interval and ending-soon threshold
- Minimal, balanced, or detailed per-item entity mode
- Per-item cap and pinned item IDs
- Watched-price percentage and currency-specific drop thresholds
- Optional per-item price targets
- Buying, selling, analytics, and seller-operations feature toggles

The integration suppresses transition events during the initial startup/reload baseline and when
API collections are incomplete or truncated.

## Read-only and Privacy

The integration only reads eBay account data. It does not place bids, buy items, modify listings,
change watchlists, send messages, perform shipping actions, issue refunds, or call eBay mutation APIs.

Some eBay permissions do not have a separately named read-only scope; this integration still uses
only retrieval operations. Credentials and OAuth tokens are redacted from diagnostics and are not
intentionally written to logs.

See **[PRIVACY.md](PRIVACY.md)** for the project privacy statement.

## Troubleshooting

- **Setup or callback problem:** follow the OAuth guide or use the manual authorization fallback.
- **Production/Sandbox failure:** confirm the selected environment matches the credential keyset.
- **Authentication failure:** reauthorize the integration.
- **New entities or behavior missing after an update:** restart Home Assistant completely.
- **Partial data:** enable the diagnostic entities and download integration diagnostics.

Report reproducible problems through **[GitHub Issues](https://github.com/andrewtryder/ha-ebay/issues)**.
Do not include client secrets, OAuth codes, access tokens, or refresh tokens.

## Development

See **[docs/development.md](docs/development.md)** for the development environment, test commands,
Home Assistant source-mount workflow, and HACS testing process.

## Trademark

eBay is a trademark of eBay Inc. The eBay name and logo are used only to identify the connected service.
