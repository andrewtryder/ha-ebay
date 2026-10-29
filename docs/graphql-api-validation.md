# eBay GraphQL API Validation and Transport Specification

> **Phase:** GraphQL Phase 0 — Transport and API Validation
> **Repository:** `andrewtryder/ha-ebay`
> **Status:** Transport implementation & API contract documentation
> **Invariant:** Strictly read-only; no live Trading XML data sources replaced in this phase. No new mandatory OAuth scopes added to the integration.

---

## 1. Executive Summary & Verification Levels

This document establishes the architecture for the read-only eBay GraphQL transport layer introduced in `ha-ebay`. The transport layer provides an authenticated, resilient HTTP client for eBay's modernized GraphQL endpoint while maintaining 100% backward compatibility with existing Home Assistant entities, unique IDs, sensor states, event telemetry, and Trading XML polling coordinators.

To ensure technical rigor, all statements and specifications in this document are categorized into four distinct verification levels:

1. **[OFFICIAL SPEC] Officially Documented API Behavior**: Backed by eBay's official documentation ([Using eBay GraphQL APIs](https://developer.ebay.com/develop/guides/sell/using-ebay-graphql-apis)).
2. **[LOCAL MOCK] Verified by Local Automated Tests**: Tested against mock aiohttp sessions in `tests/test_graphql_transport.py` (validating transport error paths, backoff loops, token refresh lifecycles, and query parsing).
3. **[LIVE TESTED] Verified Against Real Authenticated eBay API**: Directly verified against eBay production servers with valid credentials (none in Phase 0; scheduled for Phase 1).
4. **[UNVERIFIED] Unverified Assumptions**: Plausible schemas or behaviors requiring live API confirmation in Phase 1.

---

## 2. HTTP Transport and Endpoint Specifications

### 2.1 Endpoint URLs

| Environment | Endpoint URL | Status | Details & Reference |
|---|---|---|---|
| **Production** | `https://graphqlapi.ebay.com/graphql` | **[OFFICIAL SPEC]** | Documented in [Using eBay GraphQL APIs](https://developer.ebay.com/develop/guides/sell/using-ebay-graphql-apis). Single GraphQL HTTP POST endpoint. |
| **Sandbox** | `https://graphqlapi.sandbox.ebay.com/graphql` | **[UNVERIFIED]** | Configured as candidate endpoint in `EbayEndpoints`. eBay documentation specifically highlights production GraphQL Explorer; sandbox GraphQL routing and resolver parity remain **UNVERIFIED**. |

### 2.2 Required HTTP Request Headers

```http
POST /graphql HTTP/1.1
Host: graphqlapi.ebay.com
Authorization: Bearer v^1.1#i^1#p^3#...
Content-Type: application/json
Accept: application/json
X-EBAY-C-MARKETPLACE-ID: EBAY_US
Accept-Language: en-US
```

- **`Authorization`** (`Bearer <access_token>`): **[OFFICIAL SPEC]** OAuth 2.0 User Access Token. eBay notes that modern public GraphQL (FGQL) tokens can stretch up to **4,000 characters** in length.
- **`X-EBAY-C-MARKETPLACE-ID`**: **[OFFICIAL SPEC]** Mandatory header specifying the target marketplace context (e.g. `EBAY_US`, `EBAY_GB`, `EBAY_DE`). Dynamically mapped in `ha-ebay` from the user's configured Site ID.
- **`Accept-Language`**: **[OFFICIAL SPEC]** Optional locale language header (e.g. `fr-CA`, `de-DE`) derived from locale-specific marketplaces.
- **`Content-Type` & `Accept`**: **[OFFICIAL SPEC]** Must be `application/json`. All GraphQL requests are delivered via HTTP POST with JSON body `{"query": "...", "variables": {...}}`.

### 2.3 Strict Read-Only Mutation Rejection

**[LOCAL MOCK]** Enforced in [`validate_read_only_query()`](file:///Users/atr/code/ha-ebay/custom_components/ebay/clients/graphql.py#L133):
- Rejects any document containing a `mutation` or `subscription` definition anywhere in the document, regardless of position.
- Rejects multi-operation documents (only single-query documents are permitted).
- Validates `operation_name` against the declared query operation, rejecting mismatched names or anonymous queries with an operation name.
- Permits fragments alongside the single query (`fragment ... on ...`).
- Safely ignores `mutation` keywords when enclosed in double-quoted strings (`"..."`), block strings (`"""..."""`), or comments (`# ...`).
- Unsafe operations raise `ValueError` before any network dispatch occurs.

---

## 3. Error Handling and Token Lifecycle Architecture

### 3.1 Documented eBay GraphQL Error Shape

**[OFFICIAL SPEC]** eBay GraphQL returns errors within an `errors` array inside a standard JSON response ([Using eBay GraphQL APIs](https://developer.ebay.com/develop/guides/sell/using-ebay-graphql-apis)):

```json
{
  "errors": [
    {
      "message": "Access Token is expired or invalid",
      "locations": [{"line": 2, "column": 3}],
      "path": ["userWatchList"],
      "extensions": {
        "code": 1001,
        "category": "REQUEST",
        "domain": "BUY_WATCHLIST"
      }
    }
  ]
}
```

- **`extensions.code`**: Numeric or string error identifier (e.g. `1001`, `"RATE_LIMITED"`, `"UNAUTHENTICATED"`).
- **`extensions.category`**:
  - `REQUEST`: **[OFFICIAL SPEC]** Issue originated with client input, malformed syntax, expired token, missing variables, or rate limiting.
  - `BUSINESS`: **[OFFICIAL SPEC]** Business-rule or state validation failure (e.g. item ended, listing restricted).
  - `APPLICATION`: **[OFFICIAL SPEC]** Application runtime exception on eBay's server.
  - `SYSTEM`: **[DEFENSIVE FALLBACK]** Not an officially documented eBay GraphQL category (which uses `APPLICATION`, `BUSINESS`, and `REQUEST`), but supported defensively in client error parsers for internal/gateway failures or 5xx responses.
- **`extensions.domain`**: Functional service domain (e.g. `BUY_WATCHLIST`).
- **No Root Extensions**: Root-level `extensions` are not used by eBay for error reporting; error metadata is strictly nested within `errors[].extensions`.

### 3.2 Response Processing & Error Classification Matrix

| Condition | Response HTTP Status | Body Structure | Client Action | Verification Level |
|---|---|---|---|---|
| **Success** | `200 OK` | `data: {...}, errors: null` | Return `data` mapping. | **[LOCAL MOCK]** |
| **Token Expired** | `401 Unauthorized` OR `200 OK` with auth expired code/message | `errors[].extensions.code in (1001, "UNAUTHENTICATED", "TOKEN_EXPIRED")` | Force-refresh OAuth access token via synchronized `async_refresh_access_token()`; retry request once. | **[LOCAL MOCK]** |
| **Permission Denied / Missing Scope** | `403 Forbidden` OR `200 OK` with permission error | `errors[].extensions.code in ("FORBIDDEN", "ACCESS_DENIED", "INSUFFICIENT_SCOPE")` | **Do NOT refresh token.** Raise `EbayAuthError` immediately without retry. | **[LOCAL MOCK]** |
| **Rate Limited** | `429 Too Many Requests` OR `200 OK` with rate limit error | `errors[].extensions.code == "RATE_LIMITED"` | Parse `Retry-After` (if present) or apply exponential backoff (`0.5 * 2^attempt`), retrying up to 3 times. Classify as `transient`. | **[LOCAL MOCK]** |
| **System / 5xx Error** | `5xx` OR `200 OK` with `data: null` & transient failure | `5xx status` or fallback `category == "SYSTEM"` | Exponential backoff retry up to 3 times. Classify as `transient`. | **[LOCAL MOCK]** |
| **Partial Collection Failure** | `200 OK` | `data: {"userWatchList": null}, errors: [...]` | **[CRITICAL SAFEGUARD]** When `allow_partial_data=False` (default), raise `EbayGraphQLError`. **Never return null/missing collections as `[]`.** | **[LOCAL MOCK]** |
| **Malformed Response** | Any | Empty text, non-JSON, or `errors` array containing non-objects (e.g. `["error"]`) | Raise `EbayParseError`. Fail closed. | **[LOCAL MOCK]** |

### 3.3 Home Assistant `OAuth2Session` Integration

**[LOCAL MOCK]** When integrated with Home Assistant's `OAuth2Session`:
- **Preserved REST Invariant**: `RestClientMixin._clear_access_token()` is an in-memory reset of the client's token cache and does **not** mutate Home Assistant's config entry data. Existing REST and Trading XML callers never trigger config-entry update listeners or reloads on 401 errors.
- **Synchronized Forced Refresh**: `async_refresh_access_token()` synchronizes token refreshes under `self._refresh_lock` (and `OAuth2Session._token_lock` when present). Double-checked verification checks whether a concurrent coroutine already refreshed the token, preventing multiple simultaneous refresh calls to eBay for the same expired token.
- **Configuration Snapshot vs. Token-Only Update Safety**: The integration's `_async_update_listener` compares the last successfully loaded configuration snapshot (`_loaded_config_snapshot`) against the updated config entry (`_config_snapshot_changed`), excluding the **entire** OAuth `token` dictionary (`access_token`, `refresh_token`, `expires_at`, `expires_in`) and in-flight `pending_options` drafts. Routine token refreshes—even when eBay rotates `refresh_token`—trigger zero reloads. User-completed reauthorizations increment a nonsecret `reauth_generation` counter in `EbayConfigFlow._finish_reauth()`, ensuring that explicit reauthorizations (even with identical application credentials and options), credential/marketplace/scope updates, and committed option changes each trigger a single reload while preserving compatibility with existing config entries lacking `reauth_generation`.
- **Cross-Account Reauthorization Limitation**: Because config entry unique IDs currently use `{environment}:{client_id}`, completing reauthorization increments `reauth_generation` and reloads the integration (resetting the in-memory `EbayApiClient` and `EbayDataUpdateCoordinator` instances), but verifying that the consenting eBay user matches the original eBay account `userId` (to prevent cross-account reauth under the same developer application keyset from inheriting persisted per-entry stores) is deferred to the multi-account identity milestone.
- **Reauthentication Propagation**: If token refresh fails with `OAuth2TokenRequestReauthError` (e.g. invalid grant or revoked refresh token), `config_entry.async_start_reauth_if_available(hass)` (available in Home Assistant 2026.8.0+) is invoked to launch Home Assistant's reauthentication flow before raising `EbayAuthError`. Temporary refresh failures (`OAuth2TokenRequestTransientError`) raise `EbayAuthTransientError` without triggering reauth.

---

## 4. Operation Query Contracts [UNVERIFIED]

The following query schemas represent candidate models based on eBay public developer references, but remain **[UNVERIFIED]** until authenticated live queries are executed in Phase 1:

### 4.1 Watch List API (`userWatchList`) — [UNVERIFIED]

- **Candidate Query Structure**:
  ```graphql
  query GetUserWatchList($limit: Int, $offset: Int) {
    userWatchList(limit: $limit, offset: $offset) {
      total
      items {
        itemId
        title
        price {
          value
          currency
        }
        itemEndDate
        itemWebUrl
        image {
          imageUrl
        }
        formatType
      }
    }
  }
  ```
- **Unverified Assumptions**:
  - Exact pagination variable names (`limit`/`offset` vs. cursor `first`/`after`).
  - Field naming for price (`price.value` vs. `currentPrice.value`).
  - Availability of `bidCount` for watched auction listings.
  - Required OAuth scope: Whether covered under base `https://api.ebay.com/oauth/api_scope` or strictly requiring `https://api.ebay.com/oauth/api_scope/buy.watchlist`.

### 4.2 Bidding Activity API (`userBiddingActivity`) — [UNVERIFIED]

- **Candidate Query Structure**:
  ```graphql
  query GetUserBiddingActivity($limit: Int, $offset: Int) {
    userBiddingActivity(limit: $limit, offset: $offset) {
      total
      items {
        itemId
        title
        currentPrice {
          value
          currency
        }
        maxBidAmount {
          value
          currency
        }
        bidCount
        itemEndDate
        currentHighestBidder
        biddingStatus
      }
    }
  }
  ```
- **Unverified Assumptions**:
  - Exact query name (`userBiddingActivity` vs. `userBids`).
  - Active vs. ended auction filtering parameters.
  - Required OAuth scope (`buy.bidding` vs. `buy.auction` vs. base scope).

---

## 5. Scope Compatibility Policy

### 5.1 Zero Scope Disruption Guarantee

- **[OFFICIAL POLICY]** No new mandatory OAuth scopes are added in Phase 0.
- Existing user installations use `https://api.ebay.com/oauth/api_scope`.
- In Phase 1, before any live coordinator migration occurs, live testing will verify whether the base scope grants access to `userWatchList`. If granular scopes (e.g. `buy.watchlist`) are required by eBay, they will be introduced with an explicit, backward-compatible reauthorization flow rather than breaking existing installations.

---

## 6. Live-Contract Validation Checklist (Phase 1 Prerequisites)

Before migrating `WatchedItemData` or replacing `GetMyeBayBuying` in production coordinators, the following live checks must be executed against production eBay using real developer credentials:

- [ ] **GraphQL Introspection Check**: Confirm server returns introspection disabled error for `__schema`.
- [ ] **Production URL Handshake**: Execute minimal `{ __typename }` query against `https://graphqlapi.ebay.com/graphql` with `X-EBAY-C-MARKETPLACE-ID: EBAY_US`.
- [ ] **Base Scope Access Verification**: Test `userWatchList` query using an access token with only `https://api.ebay.com/oauth/api_scope`. Determine if eBay accepts base scope or returns `INSUFFICIENT_SCOPE`.
- [ ] **Watchlist Schema & Pagination Verification**: Run live `userWatchList(limit: 5)` query via GraphQL Explorer. Validate exact field names (`total`, `items`, `itemId`, `price`, `itemEndDate`, `image`).
- [ ] **Bid Count Availability on Watchlist**: Check whether `bidCount` exists on auction items returned by `userWatchList` to avoid telemetry regression compared to `GetMyeBayBuying`.
- [ ] **Sandbox Endpoint Accessibility**: Verify whether `https://graphqlapi.sandbox.ebay.com/graphql` responds to requests or if GraphQL is production-only.

---

## 7. Watchlist Capability Matrix & Reproducible Parity Harness (Milestone 1)

### 7.1 Evidence-Based Watchlist Field Matrix

| Monitored Field | Trading XML Source (`GetMyeBayBuying` `WatchList`) | Candidate GraphQL Source (`userWatchList`) | Offline Normalization Parity | Live API Status | Notes |
|---|---|---|---|---|---|
| `watched_count` | `len(WatchList/ItemArray/Item)` | `userWatchList.total` or `watchListCount` | **Exact match** (`exact_match`) | **[UNVERIFIED]** | Official docs reference both `total` and `watchListCount` / `watchListMaximum`. |
| `item_id` | `Item/ItemID` (`"123456789012"`) | `itemId` / `legacyItemId` | **Equivalent after normalization** (`equivalent_after_normalization`) | **[UNVERIFIED]** | `normalize_watchlist_item_id()` normalizes composite `v1\|123456789012\|0` to `"123456789012"`. |
| `title` | `Item/Title` | `title` | **Exact match** (`exact_match`) | **[UNVERIFIED]** | String title comparison. |
| `current_price` | `Item/SellingStatus/CurrentPrice` (fallback `StartPrice`) | `price.value` / `currentPrice.amount` | **Equivalent after normalization** (`equivalent_after_normalization`) | **[UNVERIFIED]** | String decimal values normalize to `float`. |
| `currency` | `CurrentPrice/@currencyID` | `price.currency` / `currencyCode` | **Exact match** (`exact_match`) | **[UNVERIFIED]** | ISO 4217 currency code. |
| `listing_type` | `Item/ListingType` (`Chinese`, `FixedPriceItem`) | `formatType` / `buyingOptions` (`AUCTION`, `FIXED_PRICE`) | **Equivalent after normalization** (`equivalent_after_normalization`) | **[UNVERIFIED]** | `normalize_listing_type_category()` maps both XML and GraphQL formats to `"auction"` or `"fixed_price"`. |
| `end_time` | `Item/ListingDetails/EndTime` | `itemEndDate` / `endTime` | **Equivalent after normalization** (`equivalent_after_normalization`) | **[UNVERIFIED]** | Parsed to UTC `datetime` via `parse_ebay_time()`. |
| `url` | `Item/ListingDetails/ViewItemURL` | `itemWebUrl` / `itemUrl` | **Equivalent after normalization** (`equivalent_after_normalization`) | **[UNVERIFIED]** | Canonical item URL matching strips tracking query parameters and title slugs. |
| `image` | `Item/PictureDetails/GalleryURL` | `image.imageUrl` / `galleryUrl` | **Equivalent after normalization** (`equivalent_after_normalization`) | **[UNVERIFIED]** | Matches canonical eBay CDN asset hash across thumbnail resolutions (`s-l140` vs. `s-l500`). |
| `bid_count` | `Item/SellingStatus/BidCount` | `bidCount` / `bids` | **Missing field / Unverified** (`missing_field` / `unverified`) | **[UNVERIFIED]** | Must verify live whether `userWatchList` exposes `bidCount` on watched auction items. |

### 7.2 Running the Read-Only Validation Harness

The validation harness (`scripts/validate_watchlist_parity.py`) makes **zero network calls** unless `--live` is explicitly supplied, and redacts all credentials, tokens, item IDs, titles, prices, and URLs from its output:

```bash
# 1. Offline synthetic demo (zero network calls, no credentials required):
uv run --python 3.14 --with-requirements requirements-ha-current.txt \
  python scripts/validate_watchlist_parity.py --synthetic-demo

# 2. Live read-only validation using local .env credentials (maintainer opt-in):
uv run --python 3.14 --with-requirements requirements-ha-current.txt \
  python scripts/validate_watchlist_parity.py --live --output watchlist_parity.local.md

# 3. Live read-only validation using an existing HA core.config_entries file (read-only):
uv run --python 3.14 --with-requirements requirements-ha-current.txt \
  python scripts/validate_watchlist_parity.py --live \
  --ha-config-entries /path/to/.storage/core.config_entries \
  --output watchlist_parity.local.md
```
