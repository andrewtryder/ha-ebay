# Advanced usage

Details about options, refresh behavior, supported data, and limits for the eBay integration. For installation and setup, return to the [README](../README.md).

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
| Buying, selling, active orders | Configured poll interval (default 30 minutes) |
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
**[docs/blueprints.md](blueprints.md)**.

## Examples

1. **Outbid notification** — trigger on `event.ebay_bidding_activity` with event type
   `outbid`, then send a mobile notification with the item title from the event data.
2. **Ending soon** — use the watched ending-soon blueprint, or automate on
   `event.ebay_watching_activity` / `item_ending_soon`.
3. **Seller at risk** — enable seller standards, then use the seller-at-risk blueprint
   or the related binary sensor / event when standards drop.

## Known limitations

- Multiple distinct eBay user accounts can share the same developer application keyset; duplicate setups for the same eBay account in the same environment are prevented
- Read-only: no bids, purchases, listing changes, messages, or shipping actions
- Marketplace capabilities differ (for example analytics/traffic report); unsupported
  markets soft-fail without breaking the rest of the integration
- Per-item sensors are disabled by default to avoid polluting your entity registry and recorder; you can enable individual sensors under the eBay device page or pin item IDs in Options to enable them automatically. Summary sensors provide a clean, capped `items` attribute list (with title, price, bids, end time, and item URL) for Lovelace dashboards. Items outside selection enter a grace period before cleanup (or use the cleanup button to remove them immediately)
- Recent activity on event entities is memory-only and resets on restart or reload
- Optional features need matching OAuth scopes; enabling them triggers reauthorization
- **GraphQL Watchlist is experimental and blocked (`FORBIDDEN`):**
  - Live authenticated testing of eBay's modern GraphQL endpoint confirmed that while the
    `userWatchList` query schema was accepted, the resolver returned `FORBIDDEN`.
  - The exact required OAuth scope or developer application entitlement is still unknown.
    `buy.watchlist.read` remains an unverified candidate, not a confirmed requirement.
  - Production Watchlist monitoring still uses Trading XML (`GetMyeBayBuying`) and remains
    fully operational.
  - Existing users should **not** delete/re-add the integration or attempt reauthorization to
    troubleshoot this experimental GraphQL limitation.
  - Detailed architectural findings and API contracts are documented in
    **[docs/api-architecture.md](api-architecture.md)**.
