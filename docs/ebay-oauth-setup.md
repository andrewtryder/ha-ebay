# eBay OAuth Setup Guide

This guide walks through connecting the unofficial eBay integration to Home
Assistant using eBay's OAuth authorization flow.

The eBay Developer Program and the regular eBay marketplace use separate
sign-in contexts. Your developer account owns the application credentials.
Later, the regular eBay buyer or seller account you want to monitor grants
read-only access to that application.

> [!NOTE]
> eBay may change the layout of its developer portal. Follow the field names in
> this guide even if the surrounding page looks slightly different.

## Before you begin

You need:

- Home Assistant `2026.10.0` or newer
- the eBay custom integration installed through HACS
- an eBay Developers Program account
- a regular eBay account whose activity you want to monitor

Most users should use **Production**. Use **Sandbox** only when working with
eBay test accounts and test data.

## Field reference

| Home Assistant field | eBay label | What it is |
| --- | --- | --- |
| eBay environment | Production or Sandbox | Must match the selected eBay keyset |
| App ID / Client ID | App ID (Client ID) | Public application identifier |
| Cert ID / Client secret | Cert ID (Client Secret) | Secret belonging to the same keyset |
| RuName | RuName (eBay Redirect URL name) | Identifier generated for the redirect configuration |
| eBay marketplace | Site ID | Select your marketplace (default: eBay United States) |
| Callback URL | Your auth accepted/declined URL | Browser return URL supplied by Home Assistant |

## 1. Start setup in Home Assistant

1. Go to **Settings → Devices & services**.
2. Select **Add integration** and search for **eBay**.
3. Home Assistant displays the setup screen with your OAuth callback URL and credential fields.
4. Copy the OAuth callback URL displayed on the setup screen and leave the window open.

You will paste that exact callback URL into the eBay developer portal. Do not
substitute your Home Assistant local URL or external URL.

## 2. Sign in to the eBay Developers Program

Open the application keys page:

```text
https://developer.ebay.com/my/keys
```

Sign in with your eBay Developers Program account. Register for the program
first if you do not already have an account.

![eBay Developers Program sign-in page](images/ebay-oauth/01-developer-sign-in.webp)

This developer account can be different from the regular eBay account you will
authorize later.

## OAuth scopes

Initial authorization requests only the **core** eBay API scope used for buying
and selling telemetry (Trading API).

Optional seller-operation modules request additional scopes when enabled:

- `sell.analytics.readonly` — Sell Analytics views and seller standards
- `sell.fulfillment.readonly` — orders and fulfillment
- `sell.payment.dispute` (GET-only in this integration; no `.readonly` variant)
- `commerce.feedback` (GET-only in this integration; `.readonly` is Client Credential Grant only)
- `commerce.message` (GET-only in this integration; no `.readonly` variant)

Enabling a module that needs scopes you have not granted yet starts
reauthorization for the full set required by currently enabled features.
Disabling a feature stops its API calls but does not revoke granted scopes.
Missing optional scopes are reported as partial failures; the rest of the
integration keeps working.

Existing installations that already granted the full scope set continue to
work without reauthorization.


## 3. Create or open an application keyset

Create an application if needed, or open the application you want Home
Assistant to use.

On the **Application Keys** page, select the environment you chose in Home
Assistant:

- **Production** for your real eBay account and live marketplace data
- **Sandbox** for eBay test accounts and test data

![Production keyset showing the App ID and Cert ID fields](images/ebay-oauth/02-production-keyset.webp)

Copy these two values from the same keyset:

- **App ID (Client ID)**
- **Cert ID (Client Secret)**

Home Assistant does not use the **Dev ID**.

> [!WARNING]
> Do not mix Production credentials with the Sandbox environment, or Sandbox
> credentials with the Production environment.

## 4. Open the eBay sign-in settings

On the same application keys page, find:

```text
Get a Token from eBay via Your Application
```

Under **Your eBay Sign-in Settings**, select **Add eBay Redirect URL** or expand
an existing redirect configuration.

![eBay sign-in settings showing the RuName and OAuth status](images/ebay-oauth/03-redirect-runame.webp)

Use these settings:

- Select **OAuth**, not **Auth'n'Auth**.
- Set **Display Title** to a recognizable name such as `Home Assistant`.
- If eBay requests a privacy-policy URL, a personal self-hosted installation
  may use:

```text
https://github.com/andrewtryder/ha-ebay/blob/main/PRIVACY.md
```

Use your own privacy-policy URL if you deploy the integration as part of another
service or under different terms.

The **Display Title** is only a label. It is not the RuName and it is not the
callback URL.

## 5. Register the Home Assistant callback URL

Paste the callback URL from Home Assistant into both fields:

- **Your auth accepted URL**
- **Your auth declined URL**

![eBay redirect configuration showing the accepted and declined URL fields](images/ebay-oauth/04-redirect-fields.webp)

The screenshot shows a common Home Assistant callback URL. Always use the exact
value shown by your Home Assistant setup flow.

Select **Save**. eBay then assigns a **RuName** to this redirect configuration.
Copy the generated RuName; it normally appears as a long hyphenated identifier.

The three values are different:

- **Display Title**: a human-readable label
- **Callback URL**: the URL supplied by Home Assistant
- **RuName**: the identifier generated by eBay after the redirect is saved

The callback is used only during authorization and reauthorization. Normal
operation does not require SSH, a tunnel, port forwarding, or public inbound
access to Home Assistant.

## 6. Enter the application credentials in Home Assistant

Return to the Home Assistant setup window and enter:

| Home Assistant field | Value |
| --- | --- |
| eBay environment | The environment selected on the eBay keys page |
| App ID / Client ID | The App ID from that environment's keyset |
| Cert ID / Client secret | The Cert ID from the same keyset |
| RuName | The generated eBay Redirect URL name |
| eBay marketplace | Select your marketplace (default: eBay United States) |

Leave **Manual authorization (troubleshooting fallback)** unchecked for standard
automatic setup.

Continue after checking that all three eBay identifiers came from the same
Production or Sandbox configuration.

## 7. Authorize the regular eBay account

Home Assistant opens eBay's authorization page. Sign in with the regular eBay
account whose buying, watching, bidding, or selling activity you want to
monitor, then approve the requested read-only access.

This account can be different from the eBay Developers Program account used to
create the application.

> [!IMPORTANT]
> Do not use the **Sign in to Production** button under **Get a User Token
> Here** in the developer portal. That creates a developer test token and is not
> the Home Assistant authorization flow.

## 8. Link the browser back to Home Assistant

After eBay authorization, the browser may display the Home Assistant linking
page.

![Home Assistant linking page with the Link account button](images/ebay-oauth/05-link-account.webp)

Confirm that the displayed instance is the Home Assistant installation you
intend to use, then select **Link account**. Home Assistant completes the token
exchange and creates the integration entry.

## Multiple eBay accounts

As of Milestone 9, **you can configure multiple distinct eBay accounts using the same eBay developer application keyset**. You do not need to register separate developer applications on eBay to monitor additional accounts.

To add another account:
1. Go to **Settings → Devices & services → Add integration** and select **eBay**.
2. Enter the same **App ID**, **Cert ID**, and **RuName** as your existing entry.
3. When the eBay sign-in and consent page appears in your browser, sign in with your **other** regular eBay account and grant access.
4. Home Assistant verifies the account's durable identifier (`EIASToken`) and creates a separate, completely isolated `ConfigEntry` (e.g. `eBay (second_seller)`).

> [!NOTE]
> If eBay does not return a durable `EIASToken` for an account or the account identity is ambiguous, setup will fail closed (`account_ambiguous`) to protect your installation from duplicate or conflicted entries. Authorizing the exact same account twice will safely abort with `already_configured`.

## Manual authorization fallback

Use manual authorization only when your browser environment cannot complete the
automatic callback (e.g. strict firewall rules, remote VPN configurations, or issues
redirecting back from `my.home-assistant.io`).

Enable the **Manual authorization (troubleshooting fallback)** checkbox on the initial
credentials setup screen. Home Assistant will present a consent URL and a text input
to paste the final callback URL or authorization code directly.

Transient token exchange errors (such as upstream eBay API outages or invalid credentials)
will abort the flow normally so that transient network problems can be resolved directly
without forcing manual code pasting.

1. Open the consent URL shown by Home Assistant.
2. Sign in with the regular eBay account you want to monitor.
3. Approve the read-only access request.
4. Paste the complete final callback URL into Home Assistant.

Pasting the complete callback URL allows Home Assistant to verify the OAuth
state. Paste only the authorization code when the full callback URL is not
available.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| eBay rejects the redirect | Both accepted and declined URLs must exactly match the callback URL shown by Home Assistant |
| Authorization reports an invalid redirect or RuName | Enter the generated RuName, not the Display Title or callback URL |
| OAuth or API calls fail immediately | Confirm that the selected environment matches the Production or Sandbox keyset |
| Setup used a token copied from the developer portal | Restart setup and authorize from Home Assistant instead |
| US account data is missing or incorrect | Confirm that the marketplace is set to eBay United States |
| Sell Analytics 30-day views are unavailable | Reauthorize so consent includes `sell.analytics.readonly`, or disable analytics in the integration options |
| Browser does not return to Home Assistant | Use the manual authorization fallback and paste the complete final callback URL |

## Security notes

Treat the Cert ID/client secret, access tokens, refresh tokens, authorization
codes, and OAuth state values as confidential.

The screenshots in this guide are cropped and redacted. Do not publish your own
screenshots until all application identifiers, account names, email addresses,
tokens, codes, and personal Home Assistant URLs have been removed.

---

## Architectural Context: Why BYO Developer Credentials?

Users often ask why the integration requires setting up an eBay Developers Program account instead of offering a one-click "Connect eBay" button.

As documented in [`docs/one-click-oauth-architecture.md`](one-click-oauth-architecture.md), eBay OAuth 2.0 requires an application-level Client Secret (Cert ID) for token exchange and does not support public-client PKCE. Distributing a shared client secret in an open-source HACS integration is strictly prohibited by the eBay API License Agreement and creates severe security and rate-limiting risks for all users.

The Bring-Your-Own (BYO) credentials pattern ensures that:
1. Each Home Assistant installation possesses its own isolated 5,000 call/day rate limit.
2. No central server or third party has custody of your eBay tokens or account activity.
3. Your credentials and tokens reside exclusively on your local Home Assistant instance.
4. Multiple distinct eBay accounts can be securely authorized under the same keyset (Milestone 9).
