# Privacy

`ha-ebay` is a self-hosted Home Assistant custom integration. It runs inside
the user's own Home Assistant instance.

## Data Storage

eBay application credentials and OAuth tokens are stored by Home Assistant in
the user's config-entry storage. The integration uses those credentials to make
outbound requests from the user's Home Assistant instance to eBay APIs.

The project author does not operate a hosted relay, proxy, or cloud service for
account data. Data is sent only between the user's Home Assistant instance and
eBay APIs as part of normal integration operation.

## Data Use

This integration is read-only. It does not sell user data, and the project does
not include telemetry or a hosted analytics service.

Home Assistant may store item titles, listing metadata, event details, and
summary values in entity state, attributes, recorder history, backups, logs, or
diagnostics according to the user's Home Assistant configuration.

The integration intentionally does **not** store buyer usernames, feedback
comments, or full message bodies in entity state attributes or diagnostics.
Seller-ops event payloads may include order/listing IDs, subjects, and
timestamps only.

Diagnostics and log output are intended to redact credentials, OAuth tokens,
and other secret values.

## Removing Access

Users can remove the integration from Home Assistant to delete the config
entry from their Home Assistant instance. Users can also revoke the
application's access from their eBay account through eBay account or developer
settings.

Users who deploy this integration under different terms or as part of another
service should provide their own privacy policy URL.
