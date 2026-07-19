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
- Selling: active listings, bids, offers, watchers, questions, views, quantity sold, and recent sold/unsold counts (last 60 days via SoldList/UnsoldList)
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

Existing installations keep previously granted OAuth scopes. New installs
authorize with the core buying/selling scope only; enable optional seller-operation
modules in options when needed (Home Assistant will reauthorize for extra scopes).
Disabling a feature stops its API calls but does not revoke previously granted scopes.

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

## Automation blueprints

Import-ready blueprints cover common notifications (outbid, price drop below target,
watched item ending soon, overdue shipment, buyer question, and seller at risk).

See **[docs/blueprints.md](docs/blueprints.md)** for import links and feature requirements.

## Configuration

Options are grouped into sections (General, Buying and alerts, Selling, Seller
operations, Entities, Diagnostics). They include:

- Poll interval and ending-soon threshold
- Minimal, balanced, or detailed per-item entity mode
- Per-item cap and pinned item IDs (multi-select from known listings, or type an ID)
- Watched-price percentage and currency-specific drop thresholds
- Optional per-item price-drop targets (add/remove for pinned listings)
- Buying, selling, analytics, and seller-operations feature toggles

Prefer per-item **end time** sensors on dashboards; **seconds left** sensors exist for
automations but are disabled by default.

Buying, selling, and active orders refresh on the configured poll interval. Messages refresh at
twice that interval; feedback and analytics refresh hourly; seller standards (including evaluation
dates) refresh daily. A manual refresh fetches every enabled section immediately.

The integration suppresses transition events during the initial startup/reload baseline and when
API collections are incomplete or truncated.

## Data updates

The integration polls eBay over the cloud (no local discovery). Cadence by section:

| Section | Cadence |
|---------|---------|
| Buying, selling, active orders | Configured poll interval (default 15 minutes) |
| Messages | Twice the poll interval |
| Feedback, analytics | Hourly |
| Seller standards | Daily |

Use **button.ebay_refresh** for a full refresh of every enabled section, or the
section refresh buttons (Buying, Selling, Feedback, Seller operations, Analytics)
to refresh only that subset. Manual refresh is rate-limited; presses during cooldown
or while a refresh is already running raise an error in the UI.

## Supported functions

Read-only monitoring of one linked eBay account:

- Watched items, bids, and selling listings (counts, calendars, optional per-item sensors)
- Ending-soon timers and watched-price / threshold events
- Optional seller operations: orders, fulfillment, disputes, standards, feedback, messages
- Optional account privileges (selling registration, listing amount/quantity used vs remaining, near-limit binary; requires `sell.account.readonly` reauth) and finances (available/pending/held funds, last payout, failed/returned payouts, daily/monthly fees/refunds/net; requires `sell.finances`; capability-gated where request signing is required) when enabled and scoped
- Optional Sell Analytics traffic views when the marketplace and scopes support them
- Manual refresh, inactive-item cleanup, diagnostics download, and Settings → Repairs issues

Mutation APIs (bidding, listing edits, messaging, shipping actions) are not used.

## Use cases

- Notify when you are outbid or a watched item ends soon
- Track active listings and overdue shipments for a small seller account
- Surface seller-standards or feedback risk before an evaluation period closes
- Build dashboards for watched inventory and recent selling activity

Import-ready blueprints cover several of these flows; see
**[docs/blueprints.md](docs/blueprints.md)**.

## Examples

1. **Outbid notification** — trigger on `event.ebay_bidding_activity` with event type
   `outbid`, then send a mobile notification with the item title from the event data.
2. **Ending soon** — use the watched ending-soon blueprint, or automate on
   `event.ebay_watching_activity` / `item_ending_soon`.
3. **Seller at risk** — enable seller standards, then use the seller-at-risk blueprint
   or the related binary sensor / event when standards drop.

## Known limitations

- One eBay account per developer-app credential set
- Read-only: no bids, purchases, listing changes, messages, or shipping actions
- Marketplace capabilities differ (for example analytics/traffic report); unsupported
  markets soft-fail without breaking the rest of the integration
- Per-item sensors are capped; items outside the selection enter a grace period before
  cleanup (or use the cleanup button to remove them immediately)
- Recent activity on event entities is memory-only and resets on restart or reload
- Optional features need matching OAuth scopes; enabling them triggers reauthorization

## Removal

1. Go to **Settings → Devices & services → eBay**.
2. Open the integration entry → **Delete**.
3. Optionally uninstall the integration in HACS and restart Home Assistant.

Deleting the entry removes entities and stored tokens for that account. OAuth consent
on eBay’s side is not automatically revoked; revoke the app in your eBay account
settings if you want to withdraw authorization there as well.

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
- **Authentication failure:** reauthorize the integration (also shown under Settings → Repairs).
- **Missing optional data (feedback, analytics, finances):** enable the feature in options and
  complete reauthorization so the new scopes are granted.
- **Refresh ignored / error on button press:** wait for the manual-refresh cooldown, or wait until
  the in-flight refresh finishes, then try again.
- **New entities or behavior missing after an update:** restart Home Assistant completely.
- **Persistent problems:** check Settings → Repairs for actionable issues such as missing OAuth
  scopes, an invalid Site ID, repeated partial failures, or consistently truncated collections.
- **Partial data:** enable the diagnostic entities and download integration diagnostics for
  technical detail beyond Repairs. Diagnostics may include a sanitized `last_api_failure`.

Report reproducible problems through **[GitHub Issues](https://github.com/andrewtryder/ha-ebay/issues)**.
Do not include client secrets, OAuth codes, access tokens, or refresh tokens.

## Development

See **[docs/development.md](docs/development.md)** for the development environment, test commands,
Home Assistant source-mount workflow, and HACS testing process.

## Trademark

eBay is a trademark of eBay Inc. The eBay name and logo are used only to identify the connected service.
