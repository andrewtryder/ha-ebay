# Automation blueprints

Importable Home Assistant blueprints for common eBay notifications.

## Import

1. Open **Settings → Automations & scenes → Blueprints**.
2. Choose **Import blueprint**.
3. Paste one of the GitHub URLs below (or use a My Home Assistant link).
4. Create an automation from the blueprint and pick your eBay event entity plus a notify entity.

| Blueprint | Import URL |
|---|---|
| Notify when outbid | https://github.com/andrewtryder/ha-ebay/blob/main/blueprints/automation/ebay/notify_when_outbid.yaml |
| Notify when price drops below target | https://github.com/andrewtryder/ha-ebay/blob/main/blueprints/automation/ebay/notify_when_price_drops_below_target.yaml |
| Notify when watched item is ending soon | https://github.com/andrewtryder/ha-ebay/blob/main/blueprints/automation/ebay/notify_when_watched_item_ending_soon.yaml |
| Notify when an order becomes overdue | https://github.com/andrewtryder/ha-ebay/blob/main/blueprints/automation/ebay/notify_when_order_overdue.yaml |
| Notify when a buyer question arrives | https://github.com/andrewtryder/ha-ebay/blob/main/blueprints/automation/ebay/notify_when_buyer_question.yaml |
| Notify when seller status becomes at risk | https://github.com/andrewtryder/ha-ebay/blob/main/blueprints/automation/ebay/notify_when_seller_at_risk.yaml |

My Home Assistant shortcuts:

- [Outbid](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fandrewtryder%2Fha-ebay%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Febay%2Fnotify_when_outbid.yaml)
- [Price drop below target](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fandrewtryder%2Fha-ebay%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Febay%2Fnotify_when_price_drops_below_target.yaml)
- [Watched item ending soon](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fandrewtryder%2Fha-ebay%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Febay%2Fnotify_when_watched_item_ending_soon.yaml)
- [Order overdue](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fandrewtryder%2Fha-ebay%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Febay%2Fnotify_when_order_overdue.yaml)
- [Buyer question](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fandrewtryder%2Fha-ebay%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Febay%2Fnotify_when_buyer_question.yaml)
- [Seller at risk](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fandrewtryder%2Fha-ebay%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Febay%2Fnotify_when_seller_at_risk.yaml)

## Requirements by blueprint

| Blueprint | eBay event entity | Feature / options needed |
|---|---|---|
| Outbid | `event.ebay_bidding_activity` | Buying enabled |
| Price drop below target | `event.ebay_watching_activity` | Buying enabled; configure global or pinned price-drop targets |
| Watched item ending soon | `event.ebay_watching_activity` | Buying enabled; ending-soon threshold |
| Order overdue | `event.ebay_seller_ops_activity` | Fulfillment enabled |
| Buyer question | `event.ebay_seller_ops_activity` | Messages enabled |
| Seller at risk | `event.ebay_seller_ops_activity` | Seller standards enabled |

## Notes

- Event entities stay `unknown` until their first event. That does not block blueprint setup.
- Notifications use `notify.send_message` against a notify entity (mobile app, Telegram, etc.).
- Blueprints filter on the event entity's `event_type` attribute so one activity stream can drive many automations.
