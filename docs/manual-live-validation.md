# Manual Live-Validation Checklist (Milestone 12)

This document provides a comprehensive, opt-in manual verification checklist for maintainers to execute on a real running Home Assistant instance using private eBay developer credentials and live eBay accounts.

> [!CAUTION]
> **Safety & Privacy Guidelines:**
> - Never automate live authenticated tests in CI or scripts without user oversight.
> - Never commit or store real client secrets, OAuth authorization codes, access tokens, refresh tokens, private listing payloads, or account IDs in Git commits, PR descriptions, or public issues.
> - All tests listed below are designated as **[UNVERIFIED - PENDING MAINTAINER LIVE RUN]** until explicitly executed and signed off by the maintainer.

---

## Pre-requisites

- Home Assistant `2026.10.0` or newer running in a test or production environment.
- HACS custom component `ha-ebay` installed at branch `test/ebay-end-to-end-release` (or release `0.9.0`).
- An eBay Developers Program account with a registered Application Keyset (App ID, Cert ID, RuName).
- One primary regular eBay account (buyer and/or seller).
- (Optional for multi-account check) A second distinct regular eBay account.

---

## 1. Guided BYO OAuth Onboarding Flow

- [ ] **[UNVERIFIED] Launch guided setup**:
  - Navigate to **Settings → Devices & Services → Add Integration → eBay**.
  - Verify that the Welcome screen displays clear options: **Start guided setup (Recommended)** and **Enter credentials directly**.
- [ ] **[UNVERIFIED] RuName guidance & callback verification**:
  - Select guided setup.
  - Verify that the step clearly displays the exact callback URL (e.g. `https://my.home-assistant.io/redirect/oauth`).
- [ ] **[UNVERIFIED] Credential entry & validation**:
  - Enter App ID, Cert ID (masked password selector), and RuName.
  - Test input validation: paste an RuName with spaces or a URL into App ID; verify that helpful error messages appear without redisclosing the Client Secret.
- [ ] **[UNVERIFIED] Consent redirect & token exchange**:
  - Click submit; verify browser redirects to eBay's official consent page.
  - Sign in with the primary regular eBay account and grant access.
  - Verify redirect back to Home Assistant and successful entry creation (`eBay (<username>)`).

---

## 2. Existing-User Upgrade & Schema Migration

- [ ] **[UNVERIFIED] Upgrade migration to Version 5**:
  - Take a pre-M12 installation (v1, v2, v3, or v4 config entry).
  - Update `ha-ebay` to M12 / v0.9.0 and restart Home Assistant.
  - Verify in `.storage/core.config_entries` that `version` is updated to `5`.
  - Verify `CONF_ACCOUNT_IDENTITY` is generated with `binding_state: "legacy_compatible"` and `identifier_type: "legacy_app_environment"`.
  - Verify existing tokens and options remain intact.
- [ ] **[UNVERIFIED] Entity Unique ID stability**:
  - Confirm existing dashboard cards and automations referencing `sensor.ebay_*` continue working without orphaned `_2` entity suffixes.

---

## 3. Real Watchlist & Bidding Polling

- [ ] **[UNVERIFIED] Watchlist polling**:
  - Verify `sensor.ebay_<user>_watching_count` matches the exact number of watched items on `ebay.com/myb/WatchList`.
  - Verify per-item sensors show correct current price, bids, and end time.
- [ ] **[UNVERIFIED] Bidding activity**:
  - Place a test bid or inspect active bids; verify `sensor.ebay_<user>_bidding_count` reflects active bids.
  - Verify winning / outbid status accurately updates on `event.ebay_bidding_activity`.

---

## 4. Active Selling & Seller Operations

- [ ] **[UNVERIFIED] Active listings**:
  - Verify `sensor.ebay_<user>_active_selling_count` matches active listings in Seller Hub.
- [ ] **[UNVERIFIED] Seller operations (optional)**:
  - If enabled, verify orders awaiting shipment, overdue orders, buyer messages, and seller level.
  - Confirm zero write operations: integration never modifies listings, answers questions, or creates refunds.

---

## 5. Multi-Account Onboarding (Same Keyset)

- [ ] **[UNVERIFIED] Onboard second account**:
  - Add integration again with the same App ID, Cert ID, and RuName.
  - In the browser consent page, sign in as a **second distinct eBay account**.
  - Verify Home Assistant creates a distinct second config entry (`eBay (<second_user>)`).
  - Verify both accounts appear side-by-side with isolated entities, independent sensors, and isolated activity events.
- [ ] **[UNVERIFIED] Keyset rate limit sharing**:
  - Verify both accounts share the keyset `RateLimitTracker` under `hass.data["ebay"]["rate_limit_trackers"]`.
  - If account 1 experiences rate-limiting (429), confirm account 2 shares the cooldown safely without hitting the network.

---

## 6. Entity State, Attributes & Calendars

- [ ] **[UNVERIFIED] Entity attributes & states**:
  - Verify summary sensor states have correct numerical values and `SensorStateClass.MEASUREMENT`.
  - Verify item end times match eBay listing end times across timezones.
- [ ] **[UNVERIFIED] Calendars**:
  - Open `calendar.ebay_watched_items` and `calendar.ebay_selling_items` in Home Assistant Calendar dashboard.
  - Confirm events are positioned at the exact listing conclusion times.

---

## 7. Automation Events & Blueprints

- [ ] **[UNVERIFIED] Ending-soon event**:
  - Monitor an item ending within the configured threshold (e.g. 60 minutes).
  - Verify `event.ebay_watching_activity` fires `item_ending_soon` once.
  - Verify restart/reload does NOT duplicate the ending-soon event (deduplication check).
- [ ] **[UNVERIFIED] Blueprints**:
  - Import blueprints from `docs/blueprints.md`; confirm triggers fire on simulated or real transitions.

---

## 8. Manual Refresh & Rate-Limit Debounce

- [ ] **[UNVERIFIED] Manual refresh button**:
  - Press `button.ebay_refresh`.
  - Verify coordinator performs an immediate poll of enabled sections.
  - Rapidly press `button.ebay_refresh` a second time; confirm UI displays rate-limited / cooldown notice without crashing.

---

## 9. Reload, Restart & State Restoration

- [ ] **[UNVERIFIED] Integration reload**:
  - Go to **Settings → Devices & Services → eBay → ⋮ → Reload**.
  - Verify integration reloads cleanly without duplicate entity registrations.
- [ ] **[UNVERIFIED] Full Home Assistant restart**:
  - Restart Home Assistant.
  - Verify persisted states (`.storage/ebay_ending_soon_fired_*`, `ebay_repair_streaks_*`, `ebay_inactive_item_sensors_*`) are restored without errors.

---

## 10. Diagnostics Privacy Audit

- [ ] **[UNVERIFIED] Download integration diagnostics**:
  - Go to **Settings → Devices & Services → eBay → ⋮ → Download diagnostics**.
  - Open the JSON file in an editor and search for:
    - `client_secret` (must not appear or must be `**REDACTED**`)
    - `access_token` (must not appear or must be `**REDACTED**`)
    - `refresh_token` (must not appear or must be `**REDACTED**`)
    - `runame` (must not appear or must be `**REDACTED**`)
  - Verify no sensitive OAuth tokens or private credentials exist in the export.
