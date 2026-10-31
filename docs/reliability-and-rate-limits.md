# eBay Production Reliability, Rate Limits & Quota Handling

This document specifies the reliability architecture, rate-limiting semantics, retry policies, data-preservation rules, event suppression behavior, and Home Assistant lifecycle contracts implemented in [`andrewtryder/ha-ebay`](https://github.com/andrewtryder/ha-ebay) as part of **Milestone 11**.

---

## 1. Architectural Scope & Read-Only Invariant

`ha-ebay` is **strictly read-only**. The integration monitors eBay activity and exposes telemetry, sensors, buttons, calendars, and event entities into Home Assistant. It never executes write operations, places bids, creates/edits listings, sends messages, issues refunds, or mutates remote accounts.

In accordance with Milestone 11 objectives:
- **No unverified GraphQL/REST cutover**: Established Trading XML providers remain the authoritative production baseline for Watchlist, Bidding, and Active Selling.
- **No managed token brokers**: Users configure and own their developer application keysets.
- **Fail-closed & last-good-data preservation**: Failures never masquerade as empty collections or zero counts.

---

## 2. Endpoint Families & Quota Models

eBay does not operate a single global 5,000-call quota pool. Limits vary significantly by endpoint family and keyset tier:

| Endpoint Family | Underlying Protocol | Official Baseline Quota | Observed Error Behavior | Throttling Action |
|---|---|---|---|---|
| **Trading API** | SOAP / XML (`/ws/api.dll`) | Keyset-level daily call limit (historically 5,000/day for default keysets, expandable upon application review). | HTTP 429, or HTTP 200 with XML `Ack=Failure` and `ErrorCode` 518 ("Call usage limit reached") or 17 ("Daily limit exceeded"). | Recorded in `RateLimitTracker` under `EndpointFamily.TRADING`. Backoff/cooldown applied; fast-fail if cooldown > 2.0s. |
| **REST APIs** | JSON over HTTPS (`/sell/fulfillment`, `/sell/finances`, `/sell/analytics`, `/commerce/feedback`, etc.) | Resource-specific rate limits (often expressed per minute / hour per developer application or user token). | HTTP 429 with standard `Retry-After` header (delta-seconds or HTTP-date). | Recorded in `RateLimitTracker` under `EndpointFamily.REST`. Respects `Retry-After` (capped at 30s); fast-fail if cooldown > 2.0s. |
| **GraphQL API** | JSON over HTTPS (`/commerce/user/graphql`) | Read-only validation queries only; not cut over in production. | HTTP 429 or execution throttled extension. | Recorded in `RateLimitTracker` under `EndpointFamily.GRAPHQL`. |
| **OAuth Services** | Form POST (`/identity/v1/oauth2/token`) | Per-application token exchange and refresh rate limits. | HTTP 429 or `invalid_grant` / `invalid_request`. | Bounded retries for transient errors; reauth flow triggered on permanent invalid grant. |

---

## 3. Polling Cadence & Request Volume

### Scheduled Polling

Coordinator polling cadence is user-configurable via `CONF_POLL_INTERVAL` (default: **30 minutes**). Per-section intervals allow finer scheduling where supported:
- **Full Refresh**: Traverses enabled sections (Buying, Selling, Analytics, Fulfillment, Seller Standards, Feedback, Messages, Account Privileges, Finances).
- **Page Traversal**: Paginated sections (e.g., Active Selling, Watchlist, Bidding) traverse up to `MAX_PAGES` (default 5 pages, 100-200 entries/page).
- **Request Volume**: A standard 30-minute poll of Buying + Selling typically executes 2-4 Trading API calls. Enabling optional REST sections adds 1-2 calls per section, well within standard daily limits.
- **Manual Refresh Cooldown**: `MANUAL_REFRESH_COOLDOWN_SECONDS` prevents rapid manual triggers from amplifying API usage.

---

## 4. Canonical Bounded Retry & Rate-Limit Tracking

### Bounded Retry Policy (`BoundedRetryPolicy`)

Located in [`custom_components/ebay/clients/resilience.py`](file:///Users/atr/code/ha-ebay/custom_components/ebay/clients/resilience.py):
- **Max Retries**: 2 retries (up to 3 attempts total).
- **Deterministic Backoff**: Exponential progression ($0.5 \times 2^{\text{attempt}}$: 0.5s, 1.0s, 2.0s, capped at 10.0s).
- **Transient Eligibility**: Only HTTP 429, 500, 502, 503, 504 and transport errors (`TimeoutError`, `ClientConnectorError`, `ServerDisconnectedError`, `ClientPayloadError`, `ClientOSError`) are eligible for retry. Permanent 4xx errors (400, 401, 403, 404) are never retried.
- **Non-blocking Waits**: All delays use cancellation-aware asynchronous sleeping (`await asyncio.sleep(delay)`).

### Rate-Limit Tracker (`RateLimitTracker`)

Tracks observed provider throttling without assuming theoretical usage:
- **Thread/Async-Safe**: In-memory monotonic timestamps.
- **Family & Client Scoping**: Keys are formatted as `<family>:<client_id>` or `<family>`. Accounts sharing keysets share cooldowns where appropriate, while independent accounts remain isolated.
- **Fast-Fail on Active Cooldown**: If an endpoint family has an active cooldown > 2.0s, outgoing requests fast-fail immediately with `EbayPartialFailure` (REST) or `EbayApiError` (Trading) without hitting the network, preventing request amplification. If cooldown $\le 2.0$s, the client waits the remaining time before proceeding.

### `Retry-After` Header Parsing

The function `parse_retry_after()` supports both:
1. **Delta-seconds**: Integer or float values (e.g., `"30"` $\rightarrow$ 30.0s).
2. **RFC 7231 / RFC 9110 HTTP-dates**: Full date strings (e.g., `"Sun, 06 Nov 1994 08:49:37 GMT"` $\rightarrow$ computed offset against `time.time()`).
3. **Caps**: Excessive server delays are capped at 30.0s to prevent polling tasks from stalling. Negative values clamp to 0.0s; unparseable strings fall back to safe defaults.

---

## 5. Preservation of Last-Good Data

A critical requirement in Home Assistant is that temporary API outages or rate limits must **never** cause entities to drop to 0 or disappear.

In `custom_components/ebay/section_fetch.py`:
1. **Sold / Unsold Classification**: If `async_fetch_sold_unsold_classification()` fails due to transient API errors, the prior `selling_classification` dictionary, `sold_items_count`, and `unsold_items_count` are retained from `previous`. `truncated_collections["sold_unsold"]` is flagged.
2. **Listing Views**: If `_fetch_seller_list_views()` fails, each item in `selling` retains its prior `views` count from `previous_selling`.
3. **Best Offers**: If `GetBestOffers` fails, each item in `selling` retains its prior `offers` count from `previous_selling`.
4. **Traffic Analytics**: If `async_fetch_analytics_views()` fails or partially returns, items in `selling` retain their prior `analytics_views_30d` metrics.
5. **Seller-Ops Collections**: Orders, payment disputes, seller standards, feedback, and messages retain prior payload dictionaries upon partial failure.

---

## 6. Event Suppression & Churn Prevention

The domain event detection layer in [`custom_components/ebay/events.py`](file:///Users/atr/code/ha-ebay/custom_components/ebay/events.py) evaluates payload diffs between consecutive polls:

- **Incomplete / Partial Refresh Detection**: If a section experienced truncation (`_collection_truncated`) or reported partial failures (e.g., `selling`, `selling_truncated`, `buying`, `buying_truncated`, `sold_unsold_classification`), the suppression flag `suppress_incomplete` is set to `True`.
- **Suppressed Transitions**:
  - `selling`: Suppresses `item_disappeared_unknown`, `item_sold`, and `item_ended_unsold` when selling listings or classification are incomplete.
  - `watching`: Suppresses `watched_item_disappeared_unknown`, `watched_item_added`, and false price-drop alerts when buying or watchlists are incomplete.
  - `bidding`: Suppresses false outbid, lost, or won events when bidding lists are incomplete.
- **Genuine Transitions**: Legitimate completed refreshes continue to detect price changes, bid increments, new offers, messages, and order updates.

---

## 7. OAuth2 Concurrency & Lifecycle Protection

- **Request Timeout**: All OAuth token requests via `EbayOAuth2Implementation._token_request()` specify an explicit 30s `aiohttp.ClientTimeout(total=30)` to prevent indefinite socket hangs.
- **Concurrency Locking**: `RestClientMixin.async_refresh_access_token()` uses double-checked locking under `_token_refresh_lock` to ensure multiple concurrent callers share a single refresh request.
- **Safe Persistence**: Routine token refreshes update `entry.data["token"]` via `hass.config_entries.async_update_entry()`. `_loaded_config_snapshot()` excludes the entire OAuth token dictionary, preventing integration reloads during automatic token renewal.
- **Clean Unload (`async_shutdown`)**: On integration unload, `coordinator.async_shutdown()`:
  1. Cancels active ending-soon event timers.
  2. Clears registered timer listeners and event callbacks.
  3. Cancels any in-flight background save tasks (`_ending_soon_fired_save_task`).
  4. Calls `super().async_shutdown()` to cancel Home Assistant coordinator refresh intervals.

---

## 8. Diagnostics Privacy & Observability

Diagnostic data exported via `async_get_config_entry_diagnostics()` includes non-sensitive operational telemetry:
- `rate_limiting`:
  - `throttled`: Boolean indicating if REST or Trading cooldowns are active.
  - `rest_cooldown_remaining_seconds`: Remaining REST cooldown in seconds.
  - `trading_cooldown_remaining_seconds`: Remaining Trading API cooldown in seconds.
  - `last_retry_count`: Total bounded retries executed.
- **Strict Privacy**:
  - No access tokens, refresh tokens, client secrets, authorization codes, or user passwords are ever included.
  - Sensitive identifiers (`account_id`, `client_id`, `runame`) are redacted.
  - upstream URLs with sensitive parameters are scrubbed.

---

## 9. Operational Limitations & Live Provider Verification

1. **Undocumented Rate Limits**: eBay does not publish dynamic rate-limit thresholds for every REST resource. The integration relies strictly on observed HTTP 429 headers and Trading XML error codes rather than synthetic rate-capping.
2. **Mocked Offline Validation**: All CI and regression tests run in 100% mocked offline environments without live eBay credentials or network calls.
