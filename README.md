<p align="center">
  <img src="https://upload.wikimedia.org/wikipedia/commons/4/48/EBay_logo.png" alt="eBay logo" width="220">
</p>

# eBay for Home Assistant

`ha-ebay` is a HACS custom integration that exposes read-only eBay telemetry in Home Assistant. It monitors buying, watching, bidding, and selling activity through summary sensors and fixed event entities designed for automations.

This integration targets Home Assistant `2026.3.0+`.

## Read-only Boundary

This integration is intentionally read-only. It does not place bids, buy items, create listings, revise listings, end listings, accept/counter/decline offers, send messages, change watchlists, perform shipping actions, or call eBay mutation APIs.

The MVP uses these read-only calls:

- `GetMyeBayBuying`
- `GetMyeBaySelling`
- `GetSellerList`
- `GetBestOffers`
- optional Sell Analytics traffic report

## HACS Installation

1. In HACS, add this repository as a custom repository:
   - Repository: `https://github.com/andrewtryder/ha-ebay`
   - Category: `Integration`
2. Install `eBay`.
3. Restart Home Assistant.
4. Go to Settings -> Devices & services -> Add integration -> eBay.

## eBay Developer Setup

Create or open an eBay developer application and collect:

- Client ID
- Client secret
- OAuth-enabled RuName / redirect URI identifier
- Environment: `production` or `sandbox`
- Site ID, usually `0` for US

Use production credentials with the production environment and sandbox credentials with sandbox. Mixing them will cause OAuth or Trading API failures.

For traffic views from Sell Analytics, mint consent with:

```text
https://api.ebay.com/oauth/api_scope/sell.analytics.readonly
```

The integration also requests the base eBay API scope.

## OAuth Setup

The setup flow generates an eBay consent URL from your Client ID and RuName.

1. Enter the eBay app fields in the config flow.
2. Open the consent URL shown by Home Assistant.
3. Approve access at eBay.
4. Paste either the final authorization code or the full final callback URL into the Home Assistant flow.
5. The integration exchanges the authorization code and stores the refresh token in the config entry.

Access tokens are refreshed automatically before API calls. Tokens and client secrets are redacted from diagnostics and are never logged intentionally.

### Nabu Casa / External URL Notes

The MVP is designed so automatic callback support can be added cleanly, including Home Assistant Cloud and externally reachable URLs. The safest working path today is the manual fallback: paste the authorization code or final callback URL into the setup flow.

If eBay callback routing fails, copy the final browser URL after eBay redirects and paste it into the flow.

## Options

- Poll interval: default 30 minutes
- Ending-soon threshold: default 1 hour
- Per-item entity mode: `minimal`, `balanced`, or `detailed`
- Per-item entity cap: 25, 50, 100, or 250
- Pinned item IDs
- Enable/disable buying/watch data
- Enable/disable selling data
- Enable/disable Sell Analytics views

## Summary Sensors

Always-created sensors:

- `sensor.ebay_active_selling_items`
- `sensor.ebay_watched_items`
- `sensor.ebay_bidding_items`
- `sensor.ebay_selling_total_bids`
- `sensor.ebay_selling_total_offers`
- `sensor.ebay_selling_total_sold`
- `sensor.ebay_selling_total_watchers`
- `sensor.ebay_selling_total_views`
- `sensor.ebay_watched_ending_soon`
- `sensor.ebay_selling_ending_soon`

Attributes contain bounded context such as items ending soon, items with offers, items with bids, items with questions, highest watcher/view items, update timestamps, and partial failure categories.

## Event Entities

Fixed event entities:

- `event.ebay_selling_activity`
- `event.ebay_watching_activity`
- `event.ebay_bidding_activity`

Selling event types:

- `bid_count_increased`
- `offer_received`
- `question_received`
- `watcher_count_increased`
- `quantity_sold_increased`
- `item_sold`
- `item_ended`
- `item_ended_unsold`
- `item_disappeared_unknown`
- `item_disappeared`

Watching event types:

- `watched_item_ending_soon`
- `watched_item_price_changed`
- `watched_item_bid_count_changed`
- `watched_item_ended`

Bidding event types:

- `outbid`
- `winning`
- `bidding_item_ending_soon`
- `bid_item_ended`

Event data includes item ID, title, kind, old/new values where relevant, price, currency, end time, seconds left, URL, image, and detection timestamp.

## Optional Per-item Entities

Per-item entities are convenience sensors, not the main automation surface.

- `minimal`: summary sensors and event entities only
- `balanced`: pinned items, watched items ending soon, selling items with bids/offers/questions
- `detailed`: active selling, watched, and bidding item sensors

The cap is always enforced, and pinned/actionable items have priority.

## Development

See:

- [`docs/development.md`](docs/development.md) for devcontainer, source-mounted Home Assistant, and HACS custom-repository test flows.
- [`docs/branding.md`](docs/branding.md) for local brand asset requirements in `custom_components/ebay/brand/` and generation.

To generate local Home Assistant brand assets:

```bash
python -m pip install pillow
python scripts/prepare_brand_assets.py
```

## Automation Examples

Notify when a selling item gets a bid:

```yaml
alias: eBay selling item got a bid
triggers:
  - trigger: state
    entity_id: event.ebay_selling_activity
conditions:
  - condition: template
    value_template: "{{ trigger.to_state.attributes.event_type == 'bid_count_increased' }}"
actions:
  - action: notify.notify
    data:
      message: "{{ trigger.to_state.attributes.title }} now has {{ trigger.to_state.attributes.new_value }} bids."
```

Notify when a selling item gets an offer:

```yaml
alias: eBay offer received
triggers:
  - trigger: state
    entity_id: event.ebay_selling_activity
conditions:
  - condition: template
    value_template: "{{ trigger.to_state.attributes.event_type == 'offer_received' }}"
actions:
  - action: notify.notify
    data:
      message: "Offer received on {{ trigger.to_state.attributes.title }}."
```

Notify when a watched item has one hour left:

```yaml
alias: eBay watched item ending soon
triggers:
  - trigger: state
    entity_id: event.ebay_watching_activity
conditions:
  - condition: template
    value_template: "{{ trigger.to_state.attributes.event_type == 'watched_item_ending_soon' }}"
actions:
  - action: notify.notify
    data:
      message: "{{ trigger.to_state.attributes.title }} is ending soon."
```

Notify when you are outbid:

```yaml
alias: eBay outbid
triggers:
  - trigger: state
    entity_id: event.ebay_bidding_activity
conditions:
  - condition: template
    value_template: "{{ trigger.to_state.attributes.event_type == 'outbid' }}"
actions:
  - action: notify.notify
    data:
      message: "You were outbid on {{ trigger.to_state.attributes.title }}."
```

## Troubleshooting

Missing analytics views: regenerate consent with `sell.analytics.readonly`, or disable analytics views in options. Setup still succeeds if analytics views are unavailable.

Expired or revoked refresh token: reconfigure the integration and complete OAuth consent again.

Production/sandbox mismatch: make sure the selected environment matches the eBay app credentials and refresh token.

External callback URL problems: use the manual fallback and paste the final callback URL into the setup flow.

## Trademark

eBay is a trademark of eBay Inc. This project is an unofficial Home Assistant integration and is not affiliated with, endorsed by, or sponsored by eBay Inc. The eBay logo is used only for identification.
