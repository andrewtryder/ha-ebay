# Modern eBay API Migration Feasibility Matrix (Milestones 2–8)

> **Repository:** `andrewtryder/ha-ebay`
> **Status:** Research & Engineering Feasibility Assessment
> **Version Baseline:** Home Assistant 2026.8.0+
> **Policy:** Strictly read-only monitoring; no mutations (bids, offers, listings, messages, orders, or refunds); fail-closed partial data safety; Trading XML remains authoritative until field-level parity and reliable authorization are proven.

---

## 1. Executive Summary & Verification Levels

This document establishes the official API feasibility, architectural comparison, and migration path for modernizing eBay data sources in `ha-ebay` across Milestones 2 through 8.

All claims and classifications in this document adhere to strict verification tags:

- **`[OFFICIAL SPEC]`**: Verified against official eBay Developer documentation ([Modern APIs Migration Guide](https://developer.ebay.com/develop/guides/sell/modern-apis-migration-guide), [Using eBay GraphQL APIs](https://developer.ebay.com/develop/guides/sell/using-ebay-graphql-apis), [Buy Auction API](https://developer.ebay.com/develop/api/buy/auction), [Buy API Call Limits](https://developer.ebay.com/develop/api/buy/api_call_limits)).
- **`[LIVE TESTED]`**: Directly observed and confirmed by authenticated testing against eBay's production environment by the repository maintainer.
- **`[LOCAL MOCK]`**: Verified by unit/integration tests with synthetic mock data.
- **`[UNVERIFIED]`**: Plausible hypothesis or undocumented behavior requiring live verification.
- **`[BLOCKED]`**: Operation blocked by an external authorization, scope, or platform entitlement restriction.
- **`[NO REPLACEMENT]`**: Confirmed by eBay documentation that no modern REST or GraphQL replacement exists.

---

## 2. Comprehensive Migration Feasibility Matrix

### 2.1 Milestone 2 & 3: Watchlist Monitoring

| Dimension | Details & Assessment |
|---|---|
| **1. Existing Source** | Trading XML `GetMyeBayBuying` with `<WatchList><Include>true</Include><Pagination>...</Pagination><Sort>EndTime</Sort></WatchList>` **[OFFICIAL SPEC]** **[LIVE TESTED]**. |
| **2. Modern Replacement** | eBay GraphQL **Watch List API** **[OFFICIAL SPEC]**. |
| **3. Exact Operation Name** | `query GetUserWatchList { userWatchList { ... } }` **[OFFICIAL SPEC]**. |
| **4. Required Fields & Semantics** | Monitored: `item_id`, `title`, `current_price`, `currency`, `listing_type`, `end_time`, `url`, `image`, `bid_count`, `watched_count` (10 fields). Companion fields: `watchListCount` (integer current count), `watchListMaximum` (ceiling, typically 400). Semantic difference: Item IDs formatted as modern composite IDs (`v1\|<id>\|0`) rather than legacy 12-digit integers; prices formatted as `{ value, currency }` objects **[OFFICIAL SPEC]**. |
| **5. Token Requirements** | User Access Token (OAuth 2.0 Authorization Code Grant) **[OFFICIAL SPEC]**. |
| **6. Required Scopes** | Base `https://api.ebay.com/oauth/api_scope` was insufficient in production live testing. Candidate scope `https://api.ebay.com/oauth/api_scope/buy.watchlist.read` is documented for Commerce Notification topic `WATCHLIST_REVISION`, but remains an **[UNVERIFIED]** candidate for the GraphQL query itself. Adding unverified scopes is prohibited **[OFFICIAL SPEC]** **[BLOCKED]**. |
| **7. Entitlement / Approval** | Developer tier eligibility or partner gating may apply. The maintainer's live authenticated request against production GraphQL returned `errorCode: "FORBIDDEN"` on `userWatchList` **[LIVE TESTED]** **[BLOCKED]**. |
| **8. Pagination & Date Window** | Candidate pagination parameters (`limit`, `offset`) rejected at GraphQL schema validation (HTTP 400) **[LIVE TESTED]**. Documented Watch List API uses cursor or offset pagination depending on schema version **[UNVERIFIED]**. |
| **9. Call Limits & Polling** | Individual GraphQL operations carry independent daily call limits (default 5,000/day for public GraphQL), governed by query-complexity limits **[OFFICIAL SPEC]**. Home Assistant 15-minute polling requires ~96 calls/day per account, well within standard quota. |
| **10. HA Entities Dependent** | `sensor.ebay_watched_items` (count), per-item watched sensors (`price`, `bids`, `seconds_left`, `end_time`), `event.ebay_watching_activity`, `calendar.ebay_watched_items`, and blueprints `notify_when_watched_item_ending_soon.yaml` / `notify_when_price_drops_below_target.yaml`. |
| **11. Verified Blockers** | **BLOCKED:** Production resolver returned `FORBIDDEN` (HTTP 200). Candidate `total` and `items[]` rejected (HTTP 400). Trading XML remains 100% authoritative. |
| **12. Migration Priority** | **DEFERRED (Priority 2)**: Retain Trading XML until eBay Developer Program resolves required scope/entitlement. |

---

### 2.2 Milestone 4: Bidding and Auction Activity

| Dimension | Details & Assessment |
|---|---|
| **1. Existing Source** | Trading XML `GetMyeBayBuying` with `<BidList><Include>true</Include><Pagination>...</Pagination><Sort>EndTime</Sort></BidList>` **[OFFICIAL SPEC]** **[LIVE TESTED]**. |
| **2. Modern Replacement** | eBay GraphQL **Auction API** (`userBiddingActivity`) **[OFFICIAL SPEC]**. (Supplementary: Buy Offer REST `GET /buy/offer/v1_beta/bidding/{item_id}` **[OFFICIAL SPEC]**). |
| **3. Exact Operation Name** | `query GetUserBiddingActivity { userBiddingActivity { ... } }` (and item-level `query GetItemBiddingActivity($itemId: String!) { itemBiddingActivity(itemId: $itemId) { ... } }`) **[OFFICIAL SPEC]**. |
| **4. Required Fields & Semantics** | Monitored: `bidding_count`, `item_id`, `title`, `current_price`, `currency`, `max_bid`, `winning`, `bid_count`, `end_time`, `listing_type`, `url`, `image` (12 fields). Crucial semantics: `max_bid` (buyer's private proxy limit) and `winning` (boolean high bidder state) are only returned when querying the authenticated user's own bids. Third-party views mask private bid values **[OFFICIAL SPEC]**. |
| **5. Token Requirements** | User Access Token (OAuth 2.0 Authorization Code Grant) strictly required. Application tokens cannot access user bidding records **[OFFICIAL SPEC]**. |
| **6. Required Scopes** | Documented base user scope `https://api.ebay.com/oauth/api_scope`. For REST Offer API bidding status, `https://api.ebay.com/oauth/api_scope/buy.offer.auction` is documented **[OFFICIAL SPEC]**. Whether GraphQL `userBiddingActivity` requires an additional scope or inherits base user scope is **[UNVERIFIED]**. |
| **7. Entitlement / Approval** | REST Offer API (`/buy/offer/v1_beta/...`) is a **Limited Release** API requiring explicit developer application approval from eBay **[OFFICIAL SPEC]**. GraphQL Auction API access rules are operation-level; whether `userBiddingActivity` is open to general developers or gated similarly to `userWatchList` is **[UNVERIFIED]**. |
| **8. Pagination & Date Window** | Documented support for date range filters (`startAtRange`, `endAtRange`) and standard pagination controls **[OFFICIAL SPEC]**. Inactive/ended auctions fall outside active bid lists. |
| **9. Call Limits & Polling** | Auction API operations have tracked call limits managed via Developer Technical Support (DTS) **[OFFICIAL SPEC]**. Rate limiting applies at the operation level. Single combined polling cadence matches coordinator interval. |
| **10. HA Entities Dependent** | `sensor.ebay_bidding_items` (count), per-item bidding sensors (`current_price`, `max_bid`, `seconds_left`, `end_time`), `event.ebay_bidding_activity` (`bid_item_added`, `outbid`, `winning`, `bidding_item_ending_soon`, `bid_item_ended`, `bid_item_disappeared_unknown`), `calendar.ebay_bidding_items`, and blueprint `notify_when_outbid.yaml`. |
| **12. Migration Priority** | **COMPLETED & MERGED (Milestone 4)**: Normalizer, parity validation harness, and Explorer procedures implemented and merged via PR #97. Production remains on Trading XML. |

---

### 2.3 Milestone 5: Active Seller Listings

| Dimension | Details & Assessment |
|---|---|
| **1. Existing Source** | Trading XML `GetMyeBaySelling` (`ActiveList`, `SellingSummary`) and `GetSellerEvents` **[OFFICIAL SPEC]** **[LIVE TESTED]**. |
| **2. Modern Replacement** | eBay GraphQL **Listing API** (`sellerListingSearch`) **[OFFICIAL SPEC]** or REST **Sell Inventory API** (`GET /sell/inventory/v1/inventory_item`) / **Sell Fulfillment API** **[OFFICIAL SPEC]**. |
| **3. Exact Operation Name** | `query GetSellerListings { sellerListingSearch { ... } }` **[OFFICIAL SPEC]**. |
| **4. Required Fields & Semantics** | Monitored: `item_id`, `title`, `current_price`, `currency`, `quantity`, `quantity_available`, `quantity_sold`, `watchers`, `views`, `questions`, `offers`, `end_time`, `url`, `image`. Semantic differences: Inventory API uses SKU/offer architecture; `sellerListingSearch` retains listing-level item IDs and supports `startAtRange`, `modifiedAtRange`, `endAtRange` **[OFFICIAL SPEC]**. |
| **5. Token Requirements** | User Access Token with selling privileges **[OFFICIAL SPEC]**. |
| **6. Required Scopes** | `https://api.ebay.com/oauth/api_scope/sell.inventory` or `sell.inventory.readonly` for Inventory API; GraphQL listing queries typically require `sell.inventory.readonly` or base sell scopes **[OFFICIAL SPEC]**. |
| **7. Entitlement / Approval** | Sell Inventory and Listing APIs are standard Public APIs for registered eBay sellers **[OFFICIAL SPEC]**. |
| **8. Pagination & Date Window** | Standard pagination (`limit`, `offset`); date window filters required for large listing catalogs **[OFFICIAL SPEC]**. |
| **9. Call Limits & Polling** | Sell Inventory: 2,000,000 calls/day standard limit **[OFFICIAL SPEC]**. Easily supports 15-minute polling. |
| **10. HA Entities Dependent** | `sensor.ebay_selling_active_items`, `sensor.ebay_selling_total_views`, `sensor.ebay_selling_total_watchers`, `sensor.ebay_selling_total_bids`, `sensor.ebay_selling_total_offers`, `sensor.ebay_active_listings_quantity_sold`, per-item selling sensors, `calendar.ebay_selling_items`, `event.ebay_seller_ops_activity`. |
| **11. Verified Limitations** | Selling metrics (`views`, `watchers`, `questions`) may require combining `sellerListingSearch` with Sell Analytics. Inventory API records do not represent a full census of active listings. |
| **12. Migration Priority** | **CURRENT FOCUS (Milestone 5)**: Implement offline normalizer, 16-field parity validation harness, probe definitions, and SKU/inventory vs listing ID identity analysis. Production remains on Trading XML. |

#### Active Seller Listings Architecture & Identity Findings (Milestone 5)

##### 1. Listing Identity: Inventory API vs. Trading XML ItemID
A central finding of Milestone 5 research is that **eBay Sell Inventory API entities are not a drop-in census of active listings**:
- **Trading XML ItemID**: Every active listing on eBay has a unique 12-digit numeric `ItemID`. This represents the actual public listing with its auction or fixed-price lifecycle, watcher count, views, and bids.
- **Inventory API SKU & Offers**: The Sell Inventory API models the seller's catalog as inventory items indexed by merchant `SKU`, and commercial terms as `offers`.
  - An inventory item can exist without being published to any listing.
  - A published offer has a `listingId` that corresponds to an `ItemID`, but multiple SKUs can be bundled or mapped into multi-variation listings.
  - Listings created directly on eBay's consumer web or mobile interfaces (or via older listing tools) often do not have associated Inventory API records or SKUs unless migrated by the seller.
- **Fail-Closed Census Rule**: **Inventory API records must never be treated as a complete census of active seller listings unless completeness is proven**. The validation harness flags any SKU-based probe without verified listing IDs as `unsupported` or `different_semantics` rather than claiming parity.

##### 2. Monitored Fields & Parity Classification Taxonomy
The Milestone 5 normalization module (`custom_components/ebay/parsers/seller_parity.py`) evaluates 16 seller fields using the established 6-state taxonomy:

| Monitored Field | Trading XML Source | Modern GraphQL / REST Candidate | Parity Status / Semantic Notes |
|---|---|---|---|
| `selling_count` | `ActiveList.PaginationResult.TotalNumberOfEntries` | `sellerListingSearch.total` or connection length | `exact_match` when both sides match; fails closed on truncation. |
| `item_id` | `Item.ItemID` | `item.itemId` / `legacyItemId` | `equivalent_after_normalization` (handles prefix `v1\|...\|0`). |
| `title` | `Item.Title` | `item.title` | `exact_match` or `equivalent_after_normalization` (whitespace). |
| `current_price` | `Item.SellingStatus.CurrentPrice` | `item.currentPrice.value` | `exact_match` (float rounded to 2 decimals). |
| `currency` | `Item.SellingStatus.CurrentPrice[@currencyID]` | `item.currentPrice.currency` | `exact_match` (uppercase normalized). |
| `quantity` | `Item.Quantity` | `item.quantity` / `item.availableQuantity + item.quantitySold` | `exact_match` (total quantity listed). |
| `quantity_available` | `Item.QuantityAvailable` (or `Quantity - QuantitySold`) | `item.availableQuantity` / `item.quantityAvailable` | `exact_match` or `equivalent_after_normalization`. |
| `quantity_sold` | `Item.SellingStatus.QuantitySold` | `item.quantitySold` | `exact_match` (cumulative units sold on listing). |
| `views` | `Item.WatchCount` / `Item.HitCount` | Analytics API / `item.views` | `unverified` or `missing_field` on basic listing payloads. |
| `watchers` | `Item.WatchCount` | `item.watchCount` / `item.watchers` | `exact_match` when present; otherwise `missing_field`. |
| `questions` | `Item.QuestionCount` | None (Post-Order only) | `missing_field` / `unsupported` in modern listing queries. |
| `offers` | `Item.BestOfferDetails.BestOfferCount` | `item.bestOfferCount` / Negotiation API | `equivalent_after_normalization` or `missing_field`. |
| `end_time` | `Item.ListingDetails.EndTime` | `item.listingDetails.endTime` / `item.endAt` | `equivalent_after_normalization` (ISO-8601 UTC). |
| `listing_type` | `Item.ListingType` | `item.format` / `item.listingType` | `exact_match` or `different_semantics` (e.g. ChineseAuction vs FixedPriceItem). |
| `url` | `Item.ListingDetails.ViewItemURL` | `item.itemWebUrl` / `item.itemAffiliateWebUrl` | `equivalent_after_normalization` (strips tracking query params). |
| `image` | `Item.PictureDetails.GalleryURL` | `item.image.imageUrl` / `item.thumbnailImages` | `equivalent_after_normalization` (protocol/dimension variations). |

##### 3. Built-In Seller Contract Probes
The validation harness includes 6 built-in probes for safe schema and contract discovery:
1. `typename_handshake`: `query Handshake { __typename }` (verifies gateway connectivity and OAuth validity).
2. `seller_listings_total`: Probes `sellerListingSearch { total }` to discover root field support.
3. `seller_listings_count_alt`: Probes `sellerListings { count totalCount }` alternate root field.
4. `seller_listings_paginated`: Probes paginated `sellerListingSearch(limit: 10, offset: 0) { total items { ... } }` for item records.
5. `seller_listings_alt_fields`: Probes alternate fields (`activeListings { listings { ... } }`) for field coverage discovery.
6. `seller_inventory_probe`: Probes `inventoryItems { total size items { sku } }` to verify whether SKU-based inventory schema is exposed.

##### 4. Read-Only Safety & Error Isolation
- **Strictly Read-Only**: `custom_components/ebay/parsers/seller_parity.py` validates queries using `infer_and_validate_query_operation()`, strictly rejecting any GraphQL operations containing `mutation` or disallowed operations.
- **Probe Error Isolation**: Each candidate probe preserves its own error summary (`last_error_summary`). A failure on one probe does not taint a successfully selected item-bearing probe.
- **Privacy Sanitization**: All error messages, client secrets, tokens, account names, item IDs, prices, bids, and URLs are stripped from reports via `sanitize_seller_parity_report()`.


---

### 2.4 Milestone 6: Best Offers and Negotiation History

| Dimension | Details & Assessment |
|---|---|
| **1. Existing Source** | Trading XML `GetBestOffers` (for seller) and `GetMyeBayBuying` `BestOfferList` (for buyer) **[OFFICIAL SPEC]** **[LIVE TESTED]**. |
| **2. Modern Replacement** | eBay GraphQL **Offer API** (`buyerNegotiationHistory`) and REST **Sell Negotiation API** (`GET /sell/negotiation/v1/offer`) **[OFFICIAL SPEC]**. |
| **3. Exact Operation Name** | `query GetBuyerOffers { buyerNegotiationHistory { ... } }` and REST `findEligibleItems` / `getOffers` **[OFFICIAL SPEC]**. |
| **4. Required Fields & Semantics** | Offer ID, item ID, offer amount, status (`Pending`, `Accepted`, `Declined`, `Countered`, `Expired`), buyer/seller message, expiration time **[OFFICIAL SPEC]**. Note: `ha-ebay` is strictly read-only; mutation operations (`makeOffer`, `acceptOffer`, `counterOffer`, `declineOffer`) are prohibited. |
| **5. Token Requirements** | User Access Token **[OFFICIAL SPEC]**. |
| **6. Required Scopes** | `https://api.ebay.com/oauth/api_scope/buy.offer.auction` (buyer) or `https://api.ebay.com/oauth/api_scope/sell.negotiation` (seller) **[OFFICIAL SPEC]**. |
| **7. Entitlement / Approval** | Buy Offer API is a Limited Release API; Sell Negotiation is generally available to sellers **[OFFICIAL SPEC]**. |
| **8. Pagination & Date Window** | Supports filtering by offer status, user role, and date range **[OFFICIAL SPEC]**. |
| **9. Call Limits & Polling** | Standard Sell Negotiation limits: 5,000 calls/day **[OFFICIAL SPEC]**. |
| **10. HA Entities Dependent** | `sensor.ebay_selling_total_offers`, per-item offers count, `event.ebay_seller_ops_activity` (`best_offer_received`). |
| **11. Verified Limitations** | Buy-side offer negotiation history requires buyer offer scopes and potential partner approval. |
| **12. Migration Priority** | **PLANNED (Milestone 6)**: Maintain Trading XML fallback. |

---

### 2.5 Milestone 7: Sold and Unsold Listing Classification

| Dimension | Details & Assessment |
|---|---|
| **1. Existing Source** | Trading XML `GetMyeBaySelling` (`SoldList`, `UnsoldList`) **[OFFICIAL SPEC]** **[LIVE TESTED]**. |
| **2. Modern Replacement** | REST **Sell Fulfillment API** (`GET /sell/fulfillment/v1/order`) for sold orders **[OFFICIAL SPEC]**; GraphQL `sellerListingSearch` (`endAtRange`) for unsold listings **[OFFICIAL SPEC]**. |
| **3. Exact Operation Name** | `GET /sell/fulfillment/v1/order?filter=orderfulfillmentstatus:{FULFILLED\|NOT_STARTED\|IN_PROGRESS}` and `sellerListingSearch` **[OFFICIAL SPEC]**. |
| **4. Required Fields & Semantics** | Order ID, line items, sale price, buyer handle, fulfillment status, payment status, tracking numbers, end date **[OFFICIAL SPEC]**. |
| **5. Token Requirements** | User Access Token with `sell.fulfillment` scopes **[OFFICIAL SPEC]**. |
| **6. Required Scopes** | `https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly` or `sell.fulfillment` **[OFFICIAL SPEC]**. Already optionally supported in `ha-ebay`! |
| **7. Entitlement / Approval** | Public Sell API; no partner gating **[OFFICIAL SPEC]**. |
| **8. Pagination & Date Window** | Orders filtered by date range (last 90 days); cursor pagination **[OFFICIAL SPEC]**. |
| **9. Call Limits & Polling** | 2,000,000 calls/day standard Sell limit **[OFFICIAL SPEC]**. |
| **10. HA Entities Dependent** | `sensor.ebay_seller_orders_awaiting_shipment`, `sensor.ebay_seller_orders_paid`, `event.ebay_seller_ops_activity` (`order_paid`, `order_shipped`, `listing_sold`), blueprint `notify_when_order_overdue.yaml`. |
| **11. Verified Limitations** | Unsold items with 0 bids/sales do not generate orders, so they cannot be read from the Fulfillment API; they require `sellerListingSearch` ended-listing filtering. |
| **12. Migration Priority** | **HIGH FEASIBILITY (Milestone 7)**: Sell Fulfillment REST is already partially integrated in `ha-ebay` and has high reliability. |

---

### 2.6 Milestone 8: Remaining Trading XML Operations

| Legacy Operation | Modern Replacement | Status | Analysis & HA Impact |
|---|---|---|---|
| **`GetUser`** | REST **Commerce Identity API** (`GET /commerce/identity/v1/user/`) | **[OFFICIAL SPEC]** High Feasibility | Used for account ID, username, registration site, and seller business status. Requires `https://api.ebay.com/oauth/api_scope/commerce.identity.readonly`. |
| **`GetFeedback`** | None | **[NO REPLACEMENT]** | Official eBay Modern APIs Migration Guide confirms no direct modern REST/GraphQL equivalent for full public feedback scores and comments. Trading XML `GetFeedback` must remain the permanent source. |
| **`GetSellerDashboard`** / **`GetSellerStandardsProfiles`** | REST **Sell Analytics API** (`GET /sell/analytics/v1/seller_standards_profile`) | **[OFFICIAL SPEC]** Already Integrated | Already used in `ha-ebay` for seller standards and defect tracking under `CONF_SELLER_STANDARDS_ENABLED`. |
| **`GetMemberMessages`** | None (Post-Order / Notification topics only) | **[NO REPLACEMENT]** | eBay has deprecated member-to-member message retrieval in modern APIs; buyer questions are only delivered via Event Notification webhooks or legacy XML. |
| **`GetMyeBayBuying` (`LostList`)** | None | **[NO REPLACEMENT]** | eBay migration guide explicitly states: *"Retrieve LostList: No replacement (this feature is no longer supported in My eBay)."* |

---

## 3. Recommended Sequencing and Architectural Decision

```
[Milestone 0: GraphQL Transport] -> MERGED (PR #94)
[Milestone 1: Watchlist Harness] -> MERGED (PR #95) [External userWatchList: FORBIDDEN]
             │
             ▼
[Milestone 4: Auction/Bidding Parity Harness] -> MERGED (PR #97)
             │
             ▼
[Milestone 5: Active Seller Listings Parity Harness] (CURRENT)
  - Isolated normalizer: custom_components/ebay/parsers/seller_parity.py
  - Standalone validation CLI: scripts/validate_seller_parity.py
  - Full offline tests & documentation
  - Production coordinator unchanged (Trading XML ActiveList authoritative)
             │
             ▼
[Future Milestone 7: Sold Orders via Sell Fulfillment REST]
  - High feasibility: public REST API, already optional in ha-ebay
             │
             ▼
[Future Milestone 6: Best Offers via Sell Negotiation REST]
             │
             ▼
[Future Milestones 2, 4, 5 Live Cutover]:
  - Only when eBay scope/entitlement gating is resolved.
```
