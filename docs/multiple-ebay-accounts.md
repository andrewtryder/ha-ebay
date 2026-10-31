# Multiple eBay Accounts & Canonical Account Identity

This document defines the architectural contracts, identity models, duplicate detection logic, and isolation guarantees for running multiple distinct eBay accounts under one Home Assistant installation in [`andrewtryder/ha-ebay`](https://github.com/andrewtryder/ha-ebay).

---

## 1. Executive Summary & Operational Status

- **Status**: **Completed & Safely Implemented** (Milestone 9).
- **Core Capability**: Multiple distinct eBay accounts—including accounts sharing the **exact same eBay developer application**—can be configured simultaneously in Home Assistant without token collisions, identity confusion, entity duplication, or historical state loss.
- **Production Cutover Invariant**: Milestone 9 establishes account identity and entry isolation only. **Zero production cutover to modern GraphQL/REST endpoints is activated.** Routine production polling remains 100% on Trading XML and verified REST providers.

---

## 2. Pre-M9 Limitation & Root Cause Analysis

### The Pre-M9 Uniqueness Blocker
In Home Assistant, `ConfigFlow` enforces entry uniqueness via `async_set_unique_id()` and `_abort_if_unique_id_configured()`.

Prior to Milestone 9, `custom_components/ebay/config_flow.py` executed:
```python
await self.async_set_unique_id(f"{environment}:{client_id}")
self._abort_if_unique_id_configured()
```
during the initial `async_step_credentials` form handler—**before the user ever authenticated with eBay OAuth**.

### Why This Blocked Multi-Account Setups
1. **Developer Application != Authenticated Principal**: A single eBay developer application (with its `client_id`, `client_secret`, and `runame`) can be used by an eBay seller to authorize multiple distinct eBay buyer or seller accounts.
2. **Premature Uniqueness Assertion**: By locking `unique_id` to `f"{environment}:{client_id}"` before OAuth, any attempt to add a second eBay account using the same developer credentials immediately aborted with `already_configured`.
3. **Conflation of Identifiers**: The system conflated developer app identity, eBay environment, OAuth grant, and authenticated user identity into a single key.

---

## 3. Official eBay Account Identity Contracts

### Investigated API Families & Fields

| API Family | Operation | Identifier Field | Stability & Characteristics | Existing Core Scope Entitlement |
|---|---|---|---|---|
| **Trading XML** | `GetUser` | `User.EIASToken` | **Durable, Encrypted Account Identifier**: Globally stable per eBay principal across username changes. Conditionally returned. | **YES** (`https://api.ebay.com/oauth/api_scope`) |
| **Trading XML** | `GetUser` | `User.UserID` | **Public Display Username / Account Identifier**: Returned for authenticated caller. Can change if user renames account. | **YES** (`https://api.ebay.com/oauth/api_scope`) |
| **REST Commerce** | `Identity.getUser` | `userId` | REST-native opaque identifier. Requires ungranted scope. | **NO** (Requires `commerce.identity.readonly`) |
| **REST Commerce** | `Identity.getUser` | `username` | REST display username. Requires ungranted scope. | **NO** (Requires `commerce.identity.readonly`) |

### Critical Identity Invariants
1. **`EIASToken` is an Account Identifier, NOT a Credential**: `EIASToken` is an encrypted string representing the account; it is never treated as a secret or OAuth authorization token.
2. **Conditional Availability & Durable Uniqueness Policy**:
   - `EIASToken` is conditionally returned depending on account type and developer status.
   - **Primary Durable Identifier**: `EIASToken` (Type: `AccountIdentifierType.EIASTOKEN`). Multi-account onboarding requires a verified `EIASToken` to guarantee durable isolation and prevent duplicate entries across renames.
   - **Fallback Policy**: If an account returns only mutable `UserID` (Type: `AccountIdentifierType.USER_ID`), multi-account setup on the same application blocks activation (`account_unverifiable`) rather than making false claims of durable isolation. Existing legacy entries remain intact.
   - **Fail-Closed**: If neither identifier is returned or the XML document is malformed, identification fails closed.
3. **Legitimate Rename Continuity**: When durable `EIASToken` matches an existing entry, reauthorization accepts a changed display username as a legitimate rename. The stored username and display title are updated, while `entry_id`, `unique_id`, and entity unique IDs remain completely intact.
4. **Data Minimization & Log Privacy**: Raw usernames and EIASTokens are never logged in warning/info logs (only symbolic reason codes and entry IDs are emitted). Opaque hexadecimal identifiers returned in `UserID` are omitted from entry display titles.
5. **No Cross-Family Conflation**: REST `userId` is never assumed to match XML `EIASToken`.
6. **No Speculative Scopes**: We do not request `commerce.identity.readonly`. Authentication identity verification is performed strictly via authenticated, read-only Trading XML `GetUser` under the existing core scope (`https://api.ebay.com/oauth/api_scope`).

---

## 4. Canonical Account Identity Model

Implemented in `custom_components/ebay/account_identity.py`:

```python
@dataclass(frozen=True)
class AccountIdentity:
    entry_id: str | None = None
    environment: str = "production"
    client_id: str = ""
    account_id: str | None = None  # EIASToken if available, else UserID
    identifier_type: AccountIdentifierType = AccountIdentifierType.UNKNOWN
    username: str | None = None  # Public display username
    binding_state: AccountBindingState = AccountBindingState.UNVERIFIED
    provenance: AccountIdentityProvenance = AccountIdentityProvenance.UNVERIFIED_OFFLINE
    verified_at: datetime | None = None
    failure_reason: IdentityFailureReason = IdentityFailureReason.NONE
    reauth_generation: int = 0
```

### Typed Enums
- **`AccountIdentifierType`**: `EIASTOKEN`, `USER_ID`, `LEGACY_APP_ENVIRONMENT`, `UNKNOWN`.
- **`AccountIdentityProvenance`**: `AUTHENTICATED_GET_USER`, `LEGACY_MIGRATION`, `UNVERIFIED_OFFLINE`.
- **`AccountBindingState`**: `VERIFIED`, `LEGACY_COMPATIBLE`, `UNVERIFIED`, `CONFLICT`, `FAILED`.
- **`IdentityFailureReason`**: `NONE`, `NETWORK_ERROR`, `TIMEOUT`, `AUTH_FAILED`, `MALFORMED_RESPONSE`, `MISSING_IDENTIFIER`, `ACCOUNT_MISMATCH`, `DUPLICATE_ACCOUNT`.

---

## 5. Duplicate Detection & Uniqueness Matrix

The integration enforces explicit policy rules across all 5 operational cases:

| Scenario | Condition | System Action | Policy Result |
|---|---|---|---|
| **Case A** | Same App ID, Different Accounts | App credentials match, but `account_id` differs | **ALLOWED**: Independent `ConfigEntry` created with unique ID `f"{environment}:{account_id}"` and title `eBay ({username})`. |
| **Case B** | Same App ID, Same Account | Matches existing entry `unique_id` or `is_same_account()` | **ABORT (`already_configured`)**: Prevents duplicate entry creation. |
| **Case C** | Different App ID, Same Account | Different credentials, but matching `account_id` or `username` | **ABORT (`already_configured`)**: Prevents multiple developer apps from configuring the same eBay account. |
| **Case D** | Legacy Pre-M9 Entry Already Configured | Legacy entry uses `f"{environment}:{client_id}"` | **PRESERVED**: Legacy entry retains entry_id, entity unique IDs, and options. If reauthenticated, updates in-place. |
| **Case E** | Account Identity Unverifiable / Ambiguous | Network error, timeout, non-200, or invalid XML | **FAIL-CLOSED (`account_unverifiable` / `cannot_connect`)**: Never guesses account identity. |

---

## 6. Onboarding & Reauthorization Workflows

### Unified Native and Manual OAuth Path
Both native OAuth browser callbacks and manual OAuth code exchanges pass through the exact same post-token pipeline in `async_oauth_create_entry()`:
1. `normalize_new_token()` formats token payload and timestamp.
2. `resolve_granted_scopes()` evaluates requested vs granted scopes.
3. `async_verify_authenticated_account()` makes an explicit, read-only `GetUser` call to discover the authenticated caller's identity.
4. Identical duplicate-detection and reauthorization checks run regardless of whether manual or native OAuth was used.

### Setup Lifecycle
```
User Enters App Credentials (client_id, client_secret, runame)
                           │
                           ▼
Credentials Normalized & Stored in Flow Draft (NO unique_id set yet)
                           │
                           ▼
User Completes OAuth Flow (Native Callback OR Manual Code Exchange)
                           │
                           ▼
Immediate Authenticated Trading XML GetUser Request (Bounded Timeout)
                           │
           ┌───────────────┴───────────────┐
           ▼                               ▼
       Verified                       Unverified / Malformed / Non-EIAS
           │                               │
           ▼                               ▼
Compute Account Unique ID         Abort: account_unverifiable
(f"{environment}:{account_id}")
           │
           ▼
Check Duplicate Matrix across All Entries (Cases B & C, including legacy entries)
           │
           ▼
Create ConfigEntry:
- unique_id = f"{environment}:{account_id}"
- title = "eBay ({username})" (or "eBay" for opaque IDs)
- data[account_identity] = serialized
```

### Reauthorization Lifecycle & Account-Switching Protection
If an existing entry undergoes reauthorization (e.g. token expired, scopes expanded, or credentials rotated):
1. **Identity Re-Verification**: The newly exchanged token executes `GetUser` to verify the freshly authenticated identity. If this verification fails, reauth aborts (`cannot_connect`).
2. **Account Continuity & Mismatch Check**:
   - **Verified Entry**: Compares existing identity with newly authenticated identity using `is_same_account()`. If durable `EIASToken` matches, reauth succeeds (including legitimate display username renames). If `EIASToken` conflicts, reauth aborts with **`account_mismatch`**.
   - **Unbound Legacy Entry**: The system calls `async_verify_entry_existing_identity()` to read-only verify the legacy entry's prior identity using its existing token or token refresh. If verified and accounts match, the legacy entry's binding is upgraded to verified. If accounts mismatch, reauth aborts with **`account_mismatch`**. If the prior token cannot be verified (dead or revoked token), reauth fails closed and aborts with **`account_unverifiable`**, preventing arbitrary accounts from overwriting existing entries.
3. **Fail-Closed Protection**: The live configuration entry is preserved untouched if reauthorization is aborted.

---

## 7. Migration & Backward Compatibility (Version 4 -> 5)

Existing users migrating from pre-M9 integration versions receive a seamless, non-breaking upgrade:
- **`async_migrate_entry` Bump to Version 5**: Associates a canonical `AccountIdentity` with `binding_state="legacy_compatible"`.
- **Zero Entity Registry Invalidation**: Entity unique IDs remain strictly formatted as `f"{entry.entry_id}_{key}"` or `f"{entry.entry_id}_{kind}_{item_id}_{suffix}"`. They are **never** rewritten to embed `account_id`.
- **Zero Polling Workload**: Routine background polling performs zero extra `GetUser` calls.
- **Rollback Safety**: Any entry with a version higher than 5 returns `False` immediately.

---

## 8. Cross-Account Isolation Guarantees

1. **Entity Uniqueness**: Overlapping ItemIDs between two separate configured accounts (e.g., both accounts watching or selling item `1122334455`) produce completely distinct entity unique IDs because all entity unique IDs are prefixed with `entry.entry_id`.
2. **Runtime Data Scoping**: `EbayDataUpdateCoordinator`, `EbayApiClient`, and active timers are stored in `entry.runtime_data` (keyed per ConfigEntry).
3. **Storage Scoping**: Persistent disk storage for ending-soon fired events and inactive item sensors are keyed by `entry.entry_id` (`f"ebay.{entry_id}.ending_soon"`).
4. **Feedback Username Cache**: `client._feedback_user_id` is an instance attribute on each `EbayApiClient`, ensuring no cross-account cache leakage.
5. **Diagnostics Privacy**: `CONF_ACCOUNT_ID` is included in `TO_REDACT`. The diagnostic summary exports SHA-256 pseudonymized identifiers (`account_id_pseudonym`, `username_pseudonym`), preventing raw account exposure in diagnostic files.
