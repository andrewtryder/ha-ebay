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
| **Milestone 0** | Complete GraphQL Phase 0 & Resolve OAuth Lifecycle Issues | `feat/ebay-graphql-transport` | [#94](https://github.com/andrewtryder/ha-ebay/pull/94) | **In Review (Draft PR)** |
| **Milestone 1** | Safe Live eBay API Contract & Parity Test Harness | `feat/ebay-live-api-validation` | TBD | Planned (Blocked on Milestone 0 merge) |
| **Milestone 2** | Implement GraphQL Watchlist Normalization | `feat/ebay-graphql-watchlist` | TBD | Planned |
| **Milestone 3** | Migrate Watchlist Carefully | `feat/ebay-watchlist-cutover` | TBD | Planned |
| **Milestone 4** | Bidding & Auction Activity Migration | `feat/ebay-graphql-bidding` | TBD | Planned |
| **Milestone 5** | Active Seller Listings Migration | `feat/ebay-graphql-selling-active` | TBD | Planned |
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

---

## Open Blockers & Unverified Contracts for Milestone 1

1. **Live Schema & Scope Verification**:
   - Candidate queries (`userWatchList`, `userBiddingActivity`, `sellerListingSearch` / `sellerListings`, `sellerNegotiationHistory`) and their exact argument/field names must be verified against an authenticated eBay account in Milestone 1.
   - Verify whether base `api_scope` is sufficient or whether specific scopes (`buy.watchlist`, `buy.bidding`, `sell.inventory.readonly`, `sell.negotiation.readonly`) are required and grantable for standard developer keysets.
2. **Sandbox GraphQL Availability**:
   - `https://graphqlapi.sandbox.ebay.com/graphql` is configured by convention and remains unverified until live testing in Milestone 1.
3. **Milestone 0 PR Review**:
   - Milestone 1 awaits maintainer review and merge of Draft PR [#94](https://github.com/andrewtryder/ha-ebay/pull/94).
