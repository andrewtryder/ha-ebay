# eBay GraphQL API Validation and Transport Specification

> **Phase:** GraphQL Phase 0 — Transport and API Validation
> **Repository:** `andrewtryder/ha-ebay`
> **Status:** Transport implementation & API contract documentation
> **Invariant:** Strictly read-only; no live Trading XML data sources replaced in this phase.

---

## 1. Executive Summary

This document specifies the read-only eBay GraphQL transport layer introduced in `ha-ebay`. The transport layer provides an authenticated HTTP client for eBay's modernized GraphQL endpoint (`https://graphqlapi.ebay.com/graphql`) while preserving 100% of existing Home Assistant entities, sensor states, event telemetry, and Trading XML integrations.

eBay's GraphQL APIs modernize four key traditional Trading XML operational areas:
1. **Watch List API (`userWatchList`)** — replaces `GetMyeBayBuying` (`WatchList`)
2. **Auction API (`userBiddingActivity`)** — replaces `GetMyeBayBuying` (`BidList`)
3. **Listing API (`sellerListingSearch` / `sellerListings`)** — replaces `GetMyeBaySelling` (`ActiveList`, `SoldList`, `UnsoldList`) and `GetSellerList`
4. **Offer API (`sellerNegotiationHistory`)** — replaces `GetBestOffers`

In this initial transport phase (Phase 0), we validate and establish the HTTP transport, error handling, partial-failure protections, and query contracts without altering any production polling loops or Home Assistant entity coordinators.

---

## 2. Verified Endpoints and HTTP Specifications

### 2.1 Endpoint URLs

| Environment | Endpoint URL | Protocol & Method | Auth Header |
|---|---|---|---|
| **Production** | `https://graphqlapi.ebay.com/graphql` | HTTP `POST` | `Authorization: Bearer <user_access_token>` |
| **Sandbox** | `https://graphqlapi.sandbox.ebay.com/graphql` | HTTP `POST` | `Authorization: Bearer <user_access_token>` |

Both endpoints are configured in [`EbayEndpoints`](file:///Users/atr/code/ha-ebay/custom_components/ebay/clients/endpoints.py#L12-L28) via `endpoints_for(environment).graphql`.

### 2.2 Required HTTP Headers

```http
POST /graphql HTTP/1.1
Host: graphqlapi.ebay.com
Authorization: Bearer v^1.1#i^1#p^3#...
Content-Type: application/json
Accept: application/json
X-EBAY-C-MARKETPLACE-ID: EBAY_US
Accept-Language: en-US
```

- **`Authorization`**: OAuth 2.0 User Access Token obtained via Authorization Code Grant (`Bearer <token>`). Tokens may be up to 4,000 characters long in Federated GraphQL (FGQL).
- **`X-EBAY-C-MARKETPLACE-ID`**: **Mandatory.** Identifies the marketplace context for the query (e.g. `EBAY_US`, `EBAY_GB`, `EBAY_DE`, `EBAY_AU`). Mapped from the integration's stored `site_id` via [`marketplace_id_for_site()`](file:///Users/atr/code/ha-ebay/custom_components/ebay/marketplace.py#L128-L131). Defaults to `EBAY_US` if site ID is unmapped.
- **`Accept-Language`**: Optional locale header derived from locale-specific site IDs (e.g. `fr-CA` for Canada French, `nl-BE` for Belgium Dutch) via [`accept_language_for_site()`](file:///Users/atr/code/ha-ebay/custom_components/ebay/marketplace.py#L133-L136).
- **`Content-Type`** and **`Accept`**: `application/json`.

### 2.3 Strict Read-Only Mutation Rejection

In adherence to project philosophy, mutations are strictly prohibited:
```python
def validate_read_only_query(query: str) -> None:
    # Strips comments and rejects any query starting with 'mutation'
```
Any attempt to execute a mutation through [`async_post_graphql()`](file:///Users/atr/code/ha-ebay/custom_components/ebay/clients/graphql.py#L152-L245) or [`async_execute_graphql()`](file:///Users/atr/code/ha-ebay/custom_components/ebay/clients/graphql.py#L247-L270) raises a `ValueError` immediately before any HTTP request is dispatched.

---

## 3. Verified Read-Only Operations and Query Contracts

### 3.1 Watch List API (`userWatchList`)

- **Operation Name**: `userWatchList` (read-only query)
- **Purpose**: Retrieve the authenticated buyer's default watched items.
- **Query Structure**:
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
- **Inputs**:
  - `limit` (`Int`): Number of items per page (typically 50; max 100).
  - `offset` (`Int`): 0-based start index.
- **Output Fields**:
  - `total` (`Int`): Total count of items on the watchlist.
  - `items` (`[WatchListItem]`): Array of watched listings.
- **Companion Query**: `userCustomLists` (retrieves user-created custom lists; not required for default watchlist telemetry).
- **Mutations (Forbidden in ha-ebay)**: `addToWatchList`, `removeFromWatchList`, `removeAllFromWatchList`.

### 3.2 Auction API (`userBiddingActivity`)

- **Operation Name**: `userBiddingActivity` (read-only query)
- **Purpose**: Retrieve the authenticated buyer's bidding activity on auctions.
- **Query Structure**:
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
- **Inputs**:
  - `limit` (`Int`): Page size.
  - `offset` (`Int`): 0-based offset.
- **Data Access Restrictions**: eBay returns full bidding details when querying the authenticated user's own activity, but redacts competitor buyer identifiers.
- **Companion Query**: `itemBiddingActivity` (inspects bid history for a single auction listing).
- **Mutations (Forbidden in ha-ebay)**: `placeProxyBid`.

### 3.3 Listing API (`sellerListingSearch` and `sellerListings`)

- **Operation Name**: `sellerListingSearch` (read-only query)
- **Purpose**: Search and paginate active, ended, or scheduled listings for the authenticated seller.
- **Query Structure**:
  ```graphql
  query SearchSellerListings($limit: Int, $offset: Int, $filter: SellerListingSearchFilter) {
    sellerListingSearch(limit: $limit, offset: $offset, filter: $filter) {
      total
      listings {
        listingId
        title
        formatType
        endTime
        pricingSummary {
          price {
            value
            currency
          }
        }
        quantity
        quantitySold
        quantityAvailable
        watchCount
      }
    }
  }
  ```
- **Mutations (Forbidden in ha-ebay)**: `createListing`, `updateListing`, `endListing`, `withdrawListing`.

### 3.4 Offer API (`sellerNegotiationHistory`)

- **Operation Name**: `sellerNegotiationHistory` (read-only query)
- **Purpose**: Retrieve buyer best offers on seller listings.
- **Mutations (Forbidden in ha-ebay)**: `acceptOffer`, `counterOffer`, `declineOffer`, `makeOffer`.

---

## 4. OAuth Scope Requirements

### 4.1 Scope Matrix

| GraphQL Operation | Required Scope | Token Grant Type | Can Use Base Scope? |
|---|---|---|---|
| `userWatchList` | `https://api.ebay.com/oauth/api_scope/buy.watchlist` | Authorization Code | Yes (under `https://api.ebay.com/oauth/api_scope`) |
| `userBiddingActivity` | `https://api.ebay.com/oauth/api_scope/buy.auction` | Authorization Code | Yes (under `https://api.ebay.com/oauth/api_scope`) |
| `sellerListingSearch` | `https://api.ebay.com/oauth/api_scope/sell.inventory.readonly` | Authorization Code | Yes (under `https://api.ebay.com/oauth/api_scope`) |
| `sellerNegotiationHistory`| `https://api.ebay.com/oauth/api_scope/sell.negotiation` | Authorization Code | Yes (under `https://api.ebay.com/oauth/api_scope`) |

### 4.2 Non-Breaking Compatibility Rule

Existing `ha-ebay` users authorized with the core Trading API scope (`https://api.ebay.com/oauth/api_scope`).
**Safeguard:** In Phase 0 and subsequent phases, no new mandatory OAuth scopes are added to [`CORE_SCOPE`](file:///Users/atr/code/ha-ebay/custom_components/ebay/clients/scopes.py#L22). Existing user tokens remain fully valid.

---

## 5. Pagination Contracts and Limits

1. **Variables Requirement**: eBay GraphQL strictly enforces query variables for pagination arguments. Inline literals (e.g. `userWatchList(limit: 50)`) are rejected by eBay's query validator. All pagination arguments must be passed in the `variables` JSON object.
2. **Page Sizing**:
   - Trading XML allowed `EntriesPerPage = 200`.
   - GraphQL queries enforce smaller maximums: typically `limit: 50` or `limit: 100`.
3. **Truncation Safeguard**:
   - `ha-ebay` enforces a maximum fetch cap of `MAX_PAGES = 20` pages.
   - If `offset + limit < total` after 20 pages, `buying_truncated = True` or `selling_truncated = True` must be set in coordinator fetch results.

---

## 6. Error Handling and Classification

### 6.1 HTTP Status Handling

- **401 Unauthorized**: Access token expired or invalid. Clears cached token (`_clear_access_token()`) and retries once with a refreshed token. Raises `EbayAuthError` if the second attempt fails.
- **403 Forbidden**: Insufficient permissions, missing scope, or unauthorized account. Raises `EbayAuthError`.
- **429 Too Many Requests**: Rate limit reached. Inspects `Retry-After` header (up to 30s) or applies exponential backoff (`0.5 * 2^attempt`), retrying up to 3 times. Raises `EbayApiError` (or `EbayPartialFailure` if `partial_category` is provided).
- **5xx Server Errors (500, 502, 503, 504)**: Upstream eBay server failure. Applies exponential backoff and retries up to 3 times. Raises `EbayApiError` (or `EbayPartialFailure`).
- **Network / Transport Failures (`aiohttp.ClientError`, `TimeoutError`)**: Retries up to 3 times with backoff, then raises `EbayApiError` (or `EbayPartialFailure`).

### 6.2 GraphQL Response Body Parsing

- **`EbayParseError`**: Raised on non-JSON response bodies, empty text bodies, JSON arrays/primitives, responses missing both `data` and `errors`, or malformed `data`/`errors` structures.
- **Top-Level `errors`**: When `data` is `null` and `errors` is non-empty, raises [`EbayGraphQLError`](file:///Users/atr/code/ha-ebay/custom_components/ebay/clients/errors.py#L48-L62).
- **Authentication Errors inside `errors`**: Scans `errors[].extensions` for `statusCode in {401, 403}` or `errorCode in {UNAUTHENTICATED, UNAUTHORIZED, ACCESS_DENIED, FORBIDDEN, INVALID_TOKEN, TOKEN_EXPIRED}` and message text. Raises `EbayAuthError` to trigger the token refresh loop.
- **Crucial Partial-Failure Safeguard (`allow_partial_data=False`)**:
  - When `data` contains fields but `errors` is non-empty, standard GraphQL resolver failures leave failed sub-trees as `null`.
  - In `ha-ebay`, treating a `null` collection as an empty list `[]` would wipe out entity state and trigger false deletion events (`watched_item_removed`, `item_disappeared_unknown`).
  - By default, [`async_post_graphql()`](file:///Users/atr/code/ha-ebay/custom_components/ebay/clients/graphql.py#L152-L245) raises [`EbayGraphQLError`](file:///Users/atr/code/ha-ebay/custom_components/ebay/clients/errors.py#L48-L62) whenever `errors` is present, guaranteeing that partial collections are never misidentified as empty.

---

## 7. Confirmed Field Mappings

### 7.1 Watchlist (`userWatchList` $\rightarrow$ `WatchedItemData`)

| Target Field | GraphQL Source Path | Type | Normalization Rule |
|---|---|---|---|
| `item_id` | `items[].itemId` | `str` | Strip composite REST prefixes (e.g. `v1\|123456789012\|0` $\rightarrow$ `123456789012`) |
| `title` | `items[].title` | `str` | String title, fallback `"Unknown Item"` |
| `current_price` | `items[].price.value` | `float` | Parse decimal number to `float` |
| `currency` | `items[].price.currency` | `str` | ISO 4217 currency code (default `"USD"`) |
| `end_time` | `items[].itemEndDate` | `str \| None` | ISO 8601 UTC timestamp |
| `item_url` | `items[].itemWebUrl` | `str \| None` | Listing URL |
| `image_url` | `items[].image.imageUrl` | `str \| None` | Thumbnail gallery URL |
| `listing_type` | `items[].formatType` | `str` | Map `AUCTION` $\rightarrow$ `"Auction"`, `FIXED_PRICE` $\rightarrow$ `"FixedPriceItem"` |

### 7.2 Bidding Activity (`userBiddingActivity` $\rightarrow$ `BiddingItemData`)

| Target Field | GraphQL Source Path | Type | Normalization Rule |
|---|---|---|---|
| `item_id` | `items[].itemId` | `str` | Normalize composite IDs to legacy numeric ID string |
| `title` | `items[].title` | `str` | Listing title |
| `current_price` | `items[].currentPrice.value` | `float` | Current high bid amount |
| `currency` | `items[].currentPrice.currency` | `str` | ISO 4217 currency code |
| `bids` | `items[].bidCount` | `int` | Integer bid count |
| `max_bid` | `items[].maxBidAmount.value` | `float` | User's maximum proxy bid |
| `end_time` | `items[].itemEndDate` | `str \| None` | ISO 8601 UTC timestamp |
| `winning` | `items[].currentHighestBidder` | `bool` | `True` if user currently holds high bid; filter ended auctions |

---

## 8. Unverified Capabilities and Blockers

The following items cannot be fully verified without an active, authenticated eBay developer account and live GraphQL queries:

1. **Schema Introspection**: Confirmed **unsupported** by eBay's GraphQL server. Dynamic introspection queries (`__schema`, `__type`) return an error. Schemas must be referenced from developer portal documentation or interactively queried in the eBay GraphQL Explorer.
2. **Auction Bid Count on Watched Items**: Whether `userWatchList` directly exposes `bidCount` for auction listings, or if a secondary query or Browse API call is needed to retrieve bid counts on watched auctions.
3. **Filtering Ended Auctions in `userBiddingActivity`**: Whether the query's input filter accepts status parameters (e.g. `status: ACTIVE`) to exclude ended auctions, or if client-side filtering (`end_time > now`) is required to avoid keeping ended auctions in active state.
4. **Sandbox Resolver Parity**: Whether Sandbox (`graphqlapi.sandbox.ebay.com`) has full resolver parity with Production for `userWatchList` and `userBiddingActivity`.

---

## 9. Next Steps (Phase 1 Roadmap)

1. Review and merge this transport layer PR (`feat/ebay-graphql-transport`).
2. Implement Milestone 1 (Watchlist API migration):
   - Add GraphQL query builders for `userWatchList`.
   - Add response parser normalizing GraphQL watchlist items into `WatchedItemData`.
   - Implement optional feature flag / coordinator toggle while retaining `GetMyeBayBuying` fallback.
