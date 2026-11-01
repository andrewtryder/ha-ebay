# eBay Integration Architecture & API Design

This document details the architectural design, API transport layer, empirical investigation findings, and runtime guarantees of the `ha-ebay` integration.

---

## 1. High-Level System Architecture

`ha-ebay` uses a **hybrid API architecture** optimized for completeness, reliability, and conservative rate-limit consumption:

```
                                  +---------------------------------------+
                                  |         Home Assistant Core           |
                                  |  (Config Entries, Entity Registry,    |
                                  |   State Machine, Event Bus, Repairs)  |
                                  +-------------------+-------------------+
                                                      |
                                                      v
                                  +---------------------------------------+
                                  |       EbayDataUpdateCoordinator       |
                                  |   (Section-based Polling Scheduler,   |
                                  |    Rate-Limit Backoff, Debouncing)    |
                                  +-------------------+-------------------+
                                                      |
                                                      v
                                  +---------------------------------------+
                                  |             EbayApiClient             |
                                  |  (Session Token Lifecycle, Auth Lock) |
                                  +---------+-------------------+---------+
                                            |                   |
                       +--------------------+                   +--------------------+
                       v                                                             v
        +-----------------------------+                               +-----------------------------+
        |     TradingClientMixin      |                               |       RestClientMixin       |
        |      (eBay Trading XML)     |                               |      (eBay REST APIs)       |
        +--------------+--------------+                               +--------------+--------------+
                       |                                                             |
                       v                                                             v
     +-----------------------------------+                         +-----------------------------------+
     | Core Buyer & Seller Operations:   |                         | Dedicated Seller Operations:      |
     | - GetMyeBayBuying (Watch, Bids)   |                         | - Sell Fulfillment (Orders)       |
     | - GetMyeBaySelling (Active, Sold) |                         | - Sell Analytics (Traffic, Views) |
     | - GetUser (Account Identity)      |                         | - Sell Finances (Payouts, Balances|
     |                                   |                         | - Sell Feedback, Account, etc.    |
     +-----------------------------------+                         +-----------------------------------+
```

---

## 2. The Trading XML Permanent Baseline

Extensive empirical research and live testing (conducted across Milestones 1–8) verified that **eBay's Trading XML API remains the only stable, publicly entitled API capable of delivering complete buyer and seller censuses for consumer and developer accounts**.

### Why Not REST for Core Buying & Selling?
1. **Browse API (`buy/browse`)**:
   - The Browse API is designed for affiliate item search, not personal account management.
   - It cannot retrieve a buyer's private watchlist, active bids, outbid status, or purchased items.
2. **Inventory API (`sell/inventory`)**:
   - Manages merchant inventory records and SKUs, **not** active eBay listing ItemIDs.
   - Items listed via the eBay consumer mobile app or web interface do not have inventory records in `sell/inventory`.
3. **Fulfillment API (`sell/fulfillment`)**:
   - Excellent for completed order fulfillment, shipping labels, and tracking.
   - Does **not** provide unsold item history, active seller listings, or watcher counts.

### Why Not eBay GraphQL? (Empirical Findings)
During Milestones 0–8, a potential migration to eBay's GraphQL API (`api.ebay.com/graphql`) was investigated. Live probes and contract audits revealed:
- **Private Entitlements**: GraphQL operations such as `userWatchList`, `userBiddingActivity`, and `sellerListingSearch` are internal eBay operations restricted to eBay first-party applications.
- **OAuth Rejection**: Standard developer OAuth tokens (bearing public scopes like `myebay` or `sell.inventory`) receive `403 Forbidden` or `PERMISSION_DENIED` errors when querying these schemas.
- **Conclusion**: All dead GraphQL exploration scaffolding was removed in Phase 1 of codebase consolidation. The integration maintains a rock-solid, production-proven Trading XML foundation.

---

## 3. Dedicated REST APIs for Seller Operations

While Trading XML powers core buying and selling censuses, modern eBay REST APIs are used where they provide superior granularity and dedicated endpoints:

| Domain | API Family | Endpoint | Capabilities |
|---|---|---|---|
| **Orders & Shipping** | Sell Fulfillment v1 | `GET /sell/fulfillment/v1/order` | Real-time order status, tracking numbers, line items, buyer checkout status. |
| **Traffic & Analytics** | Sell Analytics v1 | `GET /sell/analytics/v1/traffic_report` | Total page views, impressions, click-through rates. |
| **Finances & Payouts** | Sell Finances v1 | `GET /sell/finances/v1/transaction` | Available balance, pending payouts, transaction fees. |
| **Seller Standards** | Sell Analytics v1 | `GET /sell/analytics/v1/customer_service_metric` | Defect rates, late shipment rates, seller level standing. |
| **Account Privileges** | Sell Account v1 | `GET /sell/account/v1/privilege` | Selling limits, gross merchandise volume caps. |
| **Feedback** | Commerce Feedback v1 | `GET /commerce/feedback/v1/summary` | Positive, neutral, and negative feedback percentages and counts. |
| **Member Messages** | Trading XML | `GetMemberMessages` | Unread buyer inquiries and alert counts. |

---

## 4. Section-Based Polling & Rate-Limit Management

To avoid request bursts and respect eBay API rate limits, polling is split into independent **sections**:

- **Core Buying** (`watched`, `bidding`): Evaluated every poll interval (default: 15–30 minutes). Can be accelerated during active bidding.
- **Core Selling** (`active_selling`, `sold`, `unsold`): Polled periodically.
- **Seller Operations** (`orders`, `finances`, `analytics`, `standards`): Polled on a staggered schedule or only when enabled by user options.
- **High-Priority Auctions**: Watched items ending in under 12 hours or active bids can be polled at an expedited cadence if configured.

### Rate-Limit Resilience
- All requests adhere to a unified token bucket and circuit breaker.
- HTTP 429 (Rate Limit Exceeded) responses trigger exponential backoff.
- Per-day call budgets are respected, ensuring consumer accounts never exhaust their daily quota.

---

## 5. Token Lifecycle & Error Recovery Semantics

Authentication recovery adheres to strict production invariants:

1. **HTTP 401 Recovery**:
   - If an endpoint returns HTTP 401, the integration invokes `async_refresh_access_token(failed_token=...)`.
   - The failed token is passed explicitly so concurrent callers deduplicate refreshes and avoid redundant token rotations.
   - Exactly **one** forced refresh sequence and replay is permitted per logical request.
   - Total request deadline is capped at 35 seconds across all retries.
2. **HTTP 403 Permanent Behavior**:
   - HTTP 403 (Forbidden / Permission Denied) indicates a lack of scope or account entitlement.
   - HTTP 403 **never** retries and **never** refreshes tokens. It fails immediately to prevent reauthorization storms.
3. **HTTP 400 & 404**:
   - Client errors fail immediately without retry.
4. **Transient Network Errors (5xx, timeouts)**:
   - Retried up to 2 times with jittered exponential backoff before surfacing as a temporary coordinator update failure.

---

## 6. Strict Read-Only Safety Guarantee

The `ha-ebay` integration is strictly **read-only**:
- It **never** places bids, accepts offers, initiates purchases, or sends buyer messages.
- It **never** modifies active listings, prices, or inventory quantities.
- Every HTTP request method is strictly verified (`GET` or read-only Trading XML calls). Any speculative mutation payloads are rejected at the transport layer.
