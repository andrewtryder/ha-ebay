# eBay OAuth Setup Guide

This guide maps the Home Assistant eBay setup fields to the labels shown in
the eBay Developer Program.

## Terminology

| Home Assistant field | eBay label | Meaning |
| --- | --- | --- |
| App ID / Client ID | App ID / Client ID | Public app identifier |
| Cert ID / Client secret | Cert ID | Secret for the selected keyset |
| RuName | RuName (eBay Redirect URL name) | Identifier generated after redirect setup |
| Callback URL | Your auth accepted/declined URL | HA browser return URL |
| Environment | Production/Sandbox | Must match the selected eBay keys |
| Site ID | Site ID | 0 means eBay United States |

## 1. Create or Open an eBay Developer Application

Open the eBay Developer Program application keys page:

```text
https://developer.ebay.com/my/keys
```

Create an application if you do not already have one, or open the existing
application you want Home Assistant to use.

The eBay Developer Program account owns the application. The regular eBay
buyer/seller account grants access to that application. They can be different
accounts.

## 2. Choose Production or Sandbox

Choose the environment you want Home Assistant to use. Most users should use
production.

Production credentials must be paired with the production environment in Home
Assistant. Sandbox credentials must be paired with sandbox.

## 3. Find "Get a Token from eBay via Your Application"

On the application keys page, scroll to the section named:

```text
Get a Token from eBay via Your Application
```

This is where eBay shows sign-in redirect settings for your application.

> Warning: The "Sign in to Production" button under "Get a User Token Here" is
> an eBay developer test-token tool. Home Assistant does not require you to
> copy that token. Complete authorization from Home Assistant instead.

## 4. Add or Edit an eBay Redirect URL

Under "Your eBay Sign-in Settings," click "Add eBay Redirect URL" or expand an
existing redirect.

## 5. Set Display Title

Set Display Title to a name you will recognize, such as:

```text
Home Assistant
```

Display Title is only a human-readable label. It is not the RuName and it is
not the callback URL.

## 6. Set the Privacy-Policy URL

eBay may ask for "Your privacy policy URL."

For a personal self-hosted install, you may use the project privacy policy:

```text
https://github.com/andrewtryder/ha-ebay/blob/main/PRIVACY.md
```

If you deploy this integration under different terms or as part of another
service, use your own privacy policy URL.

## 7. Set Auth Accepted URL

Copy the callback URL shown by the Home Assistant setup flow and paste it into:

```text
Your auth accepted URL
```

Home Assistant provides this callback URL. You register it with eBay so the
browser can return to Home Assistant after authorization.

## 8. Set Auth Declined URL

Paste the same Home Assistant callback URL into:

```text
Your auth declined URL
```

The callback URL is used only for initial authorization and reauthorization.
Normal operation does not require a tunnel, SSH, port forwarding, Cloudflare
Tunnel, or public inbound access to Home Assistant.

## 9. Save and Copy the RuName

Save the redirect settings. eBay generates a RuName after the redirect is
saved.

Copy the generated RuName into Home Assistant. The RuName usually looks like a
long hyphenated identifier. It is not the Display Title and it is not the
callback URL.

Home Assistant sends this RuName to eBay during OAuth authorization and token
exchange.

## 10. Find App ID / Client ID

Near the top of the same eBay application keys page, copy:

```text
App ID / Client ID
```

Paste it into the Home Assistant field with the same name.

## 11. Find Cert ID / Client Secret

Copy the Cert ID from the same production or sandbox keyset:

```text
Cert ID
```

Paste it into Home Assistant as:

```text
Cert ID / Client secret
```

Do not mix production and sandbox keysets.

## 12. Return to Home Assistant

Enter the eBay environment, App ID / Client ID, Cert ID / Client secret,
RuName, and Site ID.

Use Site ID `0` for eBay United States. Other eBay site IDs are defined by
eBay's Trading API documentation.

## 13. Authorize the Regular eBay Account

After credentials are submitted, Home Assistant opens eBay authorization.

Sign in with the regular eBay account whose buying and selling activity you
want Home Assistant to monitor. This may be different from your eBay Developer
Program account.

Approve the requested read-only access. The browser returns through Home
Assistant's callback, Home Assistant exchanges the authorization code for
tokens, and the config entry is created.

## 14. Troubleshooting

If the automatic browser callback does not complete, restart setup and choose
"Manual authorization - Advanced troubleshooting."

Manual authorization shows a consent URL. Open it, sign into the regular eBay
account you want to monitor, approve read-only access, then paste the complete
final callback URL into Home Assistant. Pasting the full callback URL allows
Home Assistant to validate OAuth state. Paste only the authorization code if
the full URL is unavailable.

If authorization fails, confirm that:

- Production credentials are used with production, or sandbox credentials with
  sandbox.
- The Auth Accepted URL and Auth Declined URL exactly match the callback URL
  shown by Home Assistant.
- The Home Assistant RuName field contains the generated RuName, not the
  Display Title and not the callback URL.
- The regular eBay account approved access from the Home Assistant flow, not
  from the developer test-token button.
- Site ID is `0` for eBay United States.

## Screenshots

Annotated screenshots can be added here later when repository-safe image assets
are available.
