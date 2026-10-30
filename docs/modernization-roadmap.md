# eBay Modernization Roadmap

This document tracks the multi-milestone engineering roadmap for modernizing eBay API access, OAuth onboarding, and reliability in [`andrewtryder/ha-ebay`](https://github.com/andrewtryder/ha-ebay).

---

## Core Architectural Principles

1. **Strictly Read-Only Monitoring**: The integration never places bids, purchases items, creates/updates/ends listings, responds to offers, sends messages, ships orders, issues refunds, or executes GraphQL mutations.
2. **Backward Compatibility**: Existing config entries, credentials, entity IDs, unique IDs, sensor states/attributes, binary sensors, buttons, calendars, event entities, and automation event payloads are preserved.
3. **Fail-Closed Partial-Failure Safety**: Unavailable or failed API fields are never silently replaced with `0`, `False`, or empty lists `[]`. Incomplete responses suppress diff-based removal/outbid/won/lost/sold/unsold events.

---

## Milestone Status Overview

| Milestone | Title | Branch | PR | Status |
|---|---|---|---|---|
| **Pre-Phase** | Simplified OAuth Onboarding (BYO Credentials) | `feat/simplify-ebay-oauth-onboarding` | [#92](https://github.com/andrewtryder/ha-ebay/pull/92) | **Merged** |
| **Milestone 0** | Complete GraphQL Phase 0 & Resolve OAuth Lifecycle Issues | `feat/ebay-graphql-transport` | [#94](https://github.com/andrewtryder/ha-ebay/pull/94) | **Merged** (`60f656f`) |
| **Milestone 1** | Safe Live eBay API Contract & Parity Test Harness | `feat/ebay-live-api-validation` | [#95](https://github.com/andrewtryder/ha-ebay/pull/95) | **Completed / Merged** (`14ce43f`; Live Run Complete, GraphQL Blocker Documented) |
| **Milestone 2** | Implement GraphQL Watchlist Normalization | `feat/ebay-graphql-watchlist` | TBD | **BLOCKED / DEFERRED** (GraphQL resolver `FORBIDDEN`; Trading XML authoritative) |
| **Milestone 3** | Migrate Watchlist Carefully | `feat/ebay-watchlist-cutover` | TBD | Planned |
| **Milestone 4** | Auction & Bidding Modernization Preparation & Parity Harness | `feat/ebay-auction-validation` | [#97](https://github.com/andrewtryder/ha-ebay/pull/97) | **Merged** (`4c2f394`) |
| **Milestone 5** | Active Seller Listings Migration & Parity Harness | `feat/ebay-seller-listings-validation` | Draft PR | **In Progress** (Parity Harness & Active Seller Listings Research Complete) |
| **Milestone 6** | Best Offers & Negotiation History | `feat/ebay-graphql-offers` | TBD | Planned |
| **Milestone 7** | Sold / Unsold Listing Classification | `feat/ebay-modern-sold-unsold` | TBD | Planned |
| **Milestone 8** | Remaining Legacy Fields & API Operations | Per-operation branches | TBD | Planned |
| **Milestone 9** | Account Identity & Multiple eBay Accounts | `feat/ebay-multiple-accounts` | TBD | Planned |
| **Milestone 10** | One-Click eBay OAuth Architecture Assessment | Design deliverable | TBD | Planned |
| **Milestone 11** | Reliability, Quotas, & Compatibility Hardening | `feat/ebay-modern-api-reliability` | TBD | Planned |
| **Milestone 12** | End-to-End Home Assistant Testing & Release | `test/ebay-end-to-end-release` | TBD | Planned |

---

## Verified API & Lifecycle Findings

### 1. OAuth & Home Assistant Config-Entry Lifecycle (Pre-Phase & Milestone 0)
- **Home Assistant 2026.12 Reload Compatibility**: Integrations registering `entry.add_update_listener(...)` must use `async_update_and_abort()` during reauthorization rather than `async_update_reload_and_abort()` to avoid deprecation warnings and duplicate reloads.
- **Snapshot-Based Update Listener (`_loaded_config_snapshot`)**:
  - `OAuth2Session.async_ensure_token_valid()` and forced token refreshes persist updated `token` dicts via `hass.config_entries.async_update_entry()`, which invokes all registered config-entry update listeners.
  - Comparing only `entry.options` is insufficient because reauthorization (`_finish_reauth()`) can update `client_id`, `client_secret`, `runame`, `environment`, `site_id`, `auth_implementation`, `oauth_scopes`, or `refresh_token` while leaving `entry.options` unchanged.
  - `_async_update_listener` compares `_loaded_config_snapshot(entry)` against the snapshot captured at setup (`coordinator._loaded_config`), excluding the **entire** OAuth `token` dictionary (`access_token`, `refresh_token`, `expires_in`, `expires_at`) and in-flight `pending_options` drafts so routine OAuth renewals—even if eBay rotates `refresh_token`—trigger zero reloads.
  - Explicit user-completed reauthorizations increment a nonsecret `reauth_generation` marker in `EbayConfigFlow._finish_reauth()`. Any completed reauthorization (even with unchanged credentials and options), credential/marketplace/scope change, or committed option change triggers a single reload, while legacy entries without `reauth_generation` default cleanly to generation `0`.
  - *Cross-Account Reauthorization Note*: While `reauth_generation` reloads the integration and resets in-memory client/coordinator caches (such as `_feedback_user_id`), config entries are currently keyed by `{environment}:{client_id}` rather than eBay `userId`. Enforcing account-identity continuity across reauth when a user signs into a different eBay account under the same developer app keyset is tracked under Milestone 9 (`feat/ebay-multiple-accounts`).
- **Home Assistant Version Baseline (2026.8.0+)**:
  - `ConfigEntry.async_start_reauth_if_available(hass)` is standard across supported Home Assistant versions (2026.8.0+). Forced refresh error handling uses it directly with defensive fallback.
- **REST Invariant**: `RestClientMixin._clear_access_token()` strictly clears in-memory cached token state and never writes to the Home Assistant config entry.

### 2. eBay GraphQL Transport (Milestone 0)
- **Production Endpoint**: `https://graphqlapi.ebay.com/graphql` (**[OFFICIAL SPEC]**).
- **Headers**: Requires `Authorization: Bearer <token>`, `Content-Type: application/json`, and `X-EBAY-C-MARKETPLACE-ID` (**[OFFICIAL SPEC]**).
- **Error Categories**: eBay documents `APPLICATION`, `BUSINESS`, and `REQUEST` under `errors[].extensions.category` ([Using eBay GraphQL APIs](https://developer.ebay.com/develop/guides/sell/using-ebay-graphql-apis)). `SYSTEM` is treated only as a defensive fallback.
- **Schema Introspection**: Disabled on eBay's GraphQL endpoint (`__schema` / `__type` queries are unsupported).
- **Read-Only Enforcement**: `validate_read_only_query()` strips string literals and comments, parses top-level definitions, enforces a single `query` operation (plus optional fragments), rejects `mutation` and `subscription` definitions anywhere in the document, and validates `operation_name` alignment before network dispatch.

### 3. Watchlist Contract & Parity Harness (Milestone 1)
- **Milestone 1 Completion & Merged PR**:
  - Implemented on `feat/ebay-live-api-validation` and merged into `main` ([#95](https://github.com/andrewtryder/ha-ebay/pull/95), commit `14ce43f`).
  - Safe, local, opt-in contract and parity validation harness is complete and verified across both supported Home Assistant versions (2026.8.0 minimum and 2026.9.2 current).
- **Explicit External Authorization Blocker Documented**:
  - Live authenticated production testing confirmed that `query Handshake { __typename }` succeeded (HTTP 200) and `userWatchList { watchListCount watchListMaximum }` passed schema validation (HTTP 200), but the resolver returned `errorCode: "FORBIDDEN"`.
  - Required OAuth scope or developer application entitlement is unknown; candidate scope `buy.watchlist.read` remains unverified.
  - Existing Trading XML `GetMyeBayBuying` remains the sole authoritative production data source; Watchlist monitoring remains fully operational.
  - Milestone 2 (GraphQL Watchlist migration) is deferred and blocked until eBay's authorization requirements are identified.
- **Read-Only Opt-In Validation Tool**:
  - Added [`custom_components/ebay/parsers/watchlist_parity.py`](../custom_components/ebay/parsers/watchlist_parity.py) and [`scripts/validate_watchlist_parity.py`](../scripts/validate_watchlist_parity.py).
  - Makes **zero network calls** unless `--live` is explicitly passed.
  - Independently runs read-only Trading XML (`GetMyeBayBuying` `WatchList`) and read-only GraphQL contract probes (`Handshake`, `GetWatchListTotal`, `GetWatchListCountAlt`, `GetUserWatchList`, `GetUserWatchListAlt`, or a custom `--query-file` + `--variables-file` with automatic single-operation inference).
  - Preserves allowlisted symbolic and documented numeric GraphQL diagnostic codes (`code`, `error_code`, `category`, `domain`), replacing any unrecognized values with generic labels (`UNKNOWN_CODE`, `UNKNOWN_CATEGORY`, `UNKNOWN_DOMAIN`) and stripping all raw error messages, OAuth tokens, client secrets, item IDs, titles, prices, and URLs via `sanitize_parity_report()`.
  - Selects the best probe for parity comparison (`rank_graphql_probe_candidate()`) by completeness/pagination validity, usable item records, and field coverage before applying custom-over-built-in preference.
- **Evidence-Based Watchlist Field Classification & Fail-Closed Completeness**:
  - Compares all 10 monitored Watchlist fields (`watched_count`, `item_id`, `title`, `current_price`, `currency`, `listing_type`, `end_time`, `url`, `image`, `bid_count`) using the 6 roadmap classifications (`exact_match`, `equivalent_after_normalization`, `different_semantics`, `missing_field`, `unverified`, `unsupported`).
  - Fails closed (`status: "inconclusive"`, downgrading count and sample-matched fields to `unverified`) when either XML or GraphQL is truncated, only a partial page is retrieved, duplicate/missing item IDs occur, or non-null field coverage is partial (`both_absent_count > 0`).
  - Handles XML or GraphQL failures resiliently without inventing parity results (`xml_available: False`).

### 4. Auction & Bidding Modernization Preparation (Milestone 4)
- **Modern API Feasibility Research (Milestones 2–8)**:
  - Conducted extensive official API research and documented modern replacements across all remaining roadmap milestones in [`docs/api-migration-feasibility.md`](api-migration-feasibility.md).
  - Identified that Trading XML `GetMyeBayBuying` (`BidList`) is designated for replacement by eBay GraphQL `userBiddingActivity` / `itemBiddingActivity` (Buy Auction API) or Buy Offer REST API (`/buy/offer/v1_beta/bidding/{item_id}`).
  - Documented that `GetMyeBayBuying` (`LostList`) is deprecated with no modern replacement, while `GetFeedback` and `GetMemberMessages` have no direct modern equivalents.
- **Read-Only Auction & Bidding Parity Validation Module**:
  - Added [`custom_components/ebay/parsers/bidding_parity.py`](../custom_components/ebay/parsers/bidding_parity.py) and CLI harness [`scripts/validate_bidding_parity.py`](../scripts/validate_bidding_parity.py).
  - Makes **zero network calls** unless `--live` is explicitly passed.
  - Compares all 12 monitored bidding fields (`bidding_count`, `item_id`, `title`, `current_price`, `currency`, `max_bid`, `winning`, `bid_count`, `end_time`, `listing_type`, `url`, `image`) using the 6-state taxonomy.
  - Normalizes composite eBay item IDs (`v1|...|...`), string monetary amounts, case-insensitive currencies, and UTC timestamps.
  - Strictly enforces fail-closed completeness: truncated XML, truncated GraphQL, unreconciled items, missing IDs, or duplicates mark fields `unverified` and status `inconclusive`.
  - Enforces privacy-safe error sanitization and completely scrubs tokens, secrets, private item titles, IDs, prices, bids, and URLs from reports.
  - Production Trading XML `GetMyeBayBuying` (`BidList`) remains 100% authoritative in Home Assistant; no live entities or polling modified.

### 5. Active Seller Listings Normalization and Parity Validation (Milestone 5)
- **Active Seller Listings Architecture & Identity Analysis**:
  - Researched eBay modern listing representations across GraphQL (`sellerListingSearch`), Sell Inventory REST API (`/sell/inventory/v1/inventory_item`), and Sell Fulfillment REST API.
  - Identified critical identity differences: Sell Inventory API operates on SKUs and unpublished/published offers rather than Trading XML `ItemID`s. A seller may have active legacy listings created without SKUs or inventory records, or multi-variation items with complex SKU mappings. Inventory API records **must never** be treated as a complete census of active listings unless completeness is proven.
- **Read-Only Seller Listings Parity Validation Module**:
  - Added [`custom_components/ebay/parsers/seller_parity.py`](../custom_components/ebay/parsers/seller_parity.py) and CLI harness [`scripts/validate_seller_parity.py`](../scripts/validate_seller_parity.py).
  - Strictly read-only: performs **zero network calls** unless `--live` is explicitly passed; dispatches **zero mutations** (no listing creation/revision/ending, no offer actions, no order updates).
  - Evaluates all 16 monitored seller fields (`selling_count`, `item_id`, `title`, `current_price`, `currency`, `quantity`, `quantity_available`, `quantity_sold`, `views`, `watchers`, `questions`, `offers`, `end_time`, `listing_type`, `url`, `image`) using the 6-state taxonomy.
  - Implements 6 built-in probe definitions: `typename_handshake`, `seller_listings_total`, `seller_listings_count_alt`, `seller_listings_paginated`, `seller_listings_alt_fields`, and `seller_inventory_probe`.
  - Strictly enforces fail-closed completeness: partial pages, GraphQL partial error envelopes, contradictory pagination metadata, or unreconciled items automatically fail closed to `inconclusive` and mark fields `unverified`.
  - Enforces privacy-safe diagnostics: allowlists symbolic error codes, redacts private item IDs, titles, prices, quantities, counts, and URLs, while scrubbers sanitize Markdown and JSON outputs.
  - Production Trading XML `GetMyeBaySelling` (`ActiveList`) remains 100% authoritative in Home Assistant; runtime coordinators and entities remain untouched.

---

## Open Blockers & Live Findings for Milestone 2

1. **Live Production Validation Results (October 29, 2026)**:
   - **Production Endpoint Handshake**: `query Handshake { __typename }` returned **HTTP 200 OK** (`{"data": {"__typename": "Query"}}`), live-verifying that `https://graphqlapi.ebay.com/graphql` accepts the user access token and marketplace headers in production.
   - **Trading XML Authoritative**: `GetMyeBayBuying` (`WatchList`) returned **19 watched items** (complete, not truncated).
   - **GraphQL Schema Validation**: `userWatchList { watchListCount watchListMaximum }` passed schema validation (HTTP 200), confirming field names match official Watch List API documentation. Candidate `total`, `items`, and pagination field layouts were rejected during schema validation (HTTP 400).
   - **Resolver Authorization**: The `userWatchList` resolver returned `errorCode: "FORBIDDEN"`, indicating that `userWatchList` is not authorized for the user's current token/application credentials.
   - **Parity Status**: All 10 monitored Watchlist fields remain **Unverified** (`unverified`).
   - **Architectural Gate Result**: No GraphQL data-source cutover is justified. Trading XML `GetMyeBayBuying` remains authoritative. Milestone 2 (GraphQL Watchlist Migration) is **BLOCKED / DEFERRED** until scope/entitlement requirements are identified.
2. **OAuth Scope & Entitlement Investigation**:
   - The candidate scope `https://api.ebay.com/oauth/api_scope/buy.watchlist.read` is documented for Watchlist Revision notifications via Commerce Notification API. It is treated **strictly as a candidate for investigation**, NOT as a verified GraphQL query scope.
   - Adding speculative OAuth scopes without verified necessity is prohibited because it carries breaking compatibility implications for existing users.
3. **Sandbox GraphQL Availability**:
   - `https://graphqlapi.sandbox.ebay.com/graphql` remains **[UNVERIFIED]** until probed with sandbox credentials.
