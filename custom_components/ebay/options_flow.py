"""Options flow and schema builders for the eBay integration."""

from __future__ import annotations

from typing import Any

from homeassistant import config_entries
from homeassistant.helpers import selector
import voluptuous as vol

from .clients.scopes import missing_scopes_for_options
from .const import (
    ALLOWED_PER_ITEM_CAPS,
    CONF_ACCOUNT_PRIVILEGES_ENABLED,
    CONF_ANALYTICS_ENABLED,
    CONF_BUYING_ENABLED,
    CONF_ENDING_SOON_THRESHOLD,
    CONF_ENTITY_MODE,
    CONF_FEEDBACK_ENABLED,
    CONF_FINANCES_ENABLED,
    CONF_FULFILLMENT_ENABLED,
    CONF_MESSAGES_ENABLED,
    CONF_OAUTH_SCOPES,
    CONF_PENDING_OPTIONS,
    CONF_PER_ITEM_CAP,
    CONF_PINNED_ITEM_IDS,
    CONF_PINNED_ITEM_PRICE_TARGETS,
    CONF_POLL_INTERVAL,
    CONF_SELLER_STANDARDS_ENABLED,
    CONF_SELLING_ENABLED,
    CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT,
    CONF_WATCHED_PRICE_DROP_CURRENCY,
    CONF_WATCHED_PRICE_DROP_THRESHOLD,
    DEFAULT_OPTIONS,
    DOMAIN,
    ENTITY_MODES,
)
from .options_parse import (
    COMMON_CURRENCIES,
    normalize_pinned_item_ids,
    normalize_price_targets,
    validate_pinned_item_options,
    validate_price_options,
)

OPTIONS_MENU = [
    "general",
    "buying_alerts",
    "selling",
    "seller_operations",
    "entities",
    "diagnostics",
    "save",
]

_MISSING_SCOPE_NOTE = "Reauthorization will be required when you save."


class EbayOptionsFlow(config_entries.OptionsFlow):
    """Handle eBay options across menu sections."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry
        self._draft: dict[str, Any] | None = None

    def _ensure_draft(self) -> dict[str, Any]:
        """Return the in-memory options draft, seeding it on first use."""
        if self._draft is None:
            merged = {**DEFAULT_OPTIONS, **self._config_entry.options}
            # Migrate legacy free-form pin / price-target strings into structured forms.
            merged[CONF_PINNED_ITEM_IDS] = normalize_pinned_item_ids(
                merged.get(CONF_PINNED_ITEM_IDS, [])
            )
            try:
                merged[CONF_PINNED_ITEM_PRICE_TARGETS] = normalize_price_targets(
                    merged.get(CONF_PINNED_ITEM_PRICE_TARGETS, [])
                )
            except ValueError:
                merged[CONF_PINNED_ITEM_PRICE_TARGETS] = []
            self._draft = merged
        return self._draft

    def _missing_scope_note(self, *feature_keys: str) -> str:
        """Return a reauth hint when draft-enabled features lack granted scopes."""
        draft = self._ensure_draft()
        granted = self._config_entry.data.get(CONF_OAUTH_SCOPES)
        missing = missing_scopes_for_options(granted, draft)
        if any(draft.get(key) and key in missing for key in feature_keys):
            return _MISSING_SCOPE_NOTE
        return ""

    def _listing_select_options(self) -> list[selector.SelectOptionDict]:
        """Build pin-select options from the last coordinator payload."""
        coordinator = getattr(self._config_entry, "runtime_data", None)
        data = getattr(coordinator, "data", None) if coordinator is not None else None
        if not isinstance(data, dict):
            return []
        by_id: dict[str, str] = {}
        for kind in ("watched", "bidding", "selling"):
            collection = data.get(kind) or {}
            if not isinstance(collection, dict):
                continue
            for item_id, item in collection.items():
                item_id_text = str(item_id).strip()
                if not item_id_text or item_id_text in by_id:
                    continue
                title = ""
                if isinstance(item, dict):
                    title = str(item.get("title") or "").strip()
                label = f"{item_id_text} — {title[:40]}" if title else item_id_text
                by_id[item_id_text] = label
        return [
            selector.SelectOptionDict(value=item_id, label=label)
            for item_id, label in sorted(by_id.items())
        ]

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show the options section menu."""
        self._ensure_draft()
        return self.async_show_menu(step_id="init", menu_options=OPTIONS_MENU)

    async def async_step_general(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Edit general polling options."""
        draft = self._ensure_draft()
        if user_input is not None:
            draft.update(user_input)
            return await self.async_step_init()
        return self.async_show_form(
            step_id="general",
            data_schema=_schema_general(draft),
        )

    async def async_step_buying_alerts(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Edit buying and global price-alert options, then price targets."""
        draft = self._ensure_draft()
        errors: dict[str, str] = {}
        if user_input is not None:
            candidate = {**draft, **user_input}
            # Validate globals only here; price targets are edited in a subflow.
            candidate_for_validate = {
                **candidate,
                CONF_PINNED_ITEM_PRICE_TARGETS: draft.get(
                    CONF_PINNED_ITEM_PRICE_TARGETS, []
                ),
            }
            errors = {
                key: value
                for key, value in validate_price_options(candidate_for_validate).items()
                if key != CONF_PINNED_ITEM_PRICE_TARGETS
            }
            if not errors:
                draft.update(user_input)
                draft[CONF_PINNED_ITEM_PRICE_TARGETS] = normalize_price_targets(
                    draft.get(CONF_PINNED_ITEM_PRICE_TARGETS, [])
                )
                return await self.async_step_price_targets()
            draft_for_form = candidate
        else:
            draft_for_form = draft
        return self.async_show_form(
            step_id="buying_alerts",
            data_schema=_schema_buying_alerts(draft_for_form),
            errors=errors,
        )

    async def async_step_price_targets(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Menu for managing structured per-item price targets."""
        draft = self._ensure_draft()
        targets = normalize_price_targets(draft.get(CONF_PINNED_ITEM_PRICE_TARGETS, []))
        draft[CONF_PINNED_ITEM_PRICE_TARGETS] = targets
        menu_options = ["price_target_add", "price_targets_done"]
        if targets:
            menu_options.insert(1, "price_target_remove")
        summary = (
            ", ".join(
                f"{row['item_id']}={row['amount']} {row['currency']}" for row in targets
            )
            or "(none)"
        )
        return self.async_show_menu(
            step_id="price_targets",
            menu_options=menu_options,
            description_placeholders={"price_targets_summary": summary},
        )

    async def async_step_price_targets_done(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Leave the price-targets subflow and return to the options menu."""
        return await self.async_step_init()

    async def async_step_price_target_add(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Add or replace a per-pinned-item price target."""
        draft = self._ensure_draft()
        errors: dict[str, str] = {}
        pinned = normalize_pinned_item_ids(draft.get(CONF_PINNED_ITEM_IDS, []))
        if user_input is not None:
            item_id = str(user_input.get("item_id") or "").strip()
            currency = str(user_input.get("currency") or "").strip().upper()
            amount = user_input.get("amount")
            if item_id not in pinned:
                errors["item_id"] = "target_not_pinned"
            else:
                try:
                    entry = {
                        "item_id": item_id,
                        "amount": amount,
                        "currency": currency,
                    }
                    existing = [
                        row
                        for row in normalize_price_targets(
                            draft.get(CONF_PINNED_ITEM_PRICE_TARGETS, [])
                        )
                        if row["item_id"] != item_id
                    ]
                    existing.append(entry)
                    draft[CONF_PINNED_ITEM_PRICE_TARGETS] = normalize_price_targets(
                        existing
                    )
                    return await self.async_step_price_targets()
                except ValueError:
                    errors["base"] = "invalid_price_target"
        if not pinned:
            return self.async_show_form(
                step_id="price_target_add",
                data_schema=vol.Schema({}),
                errors={"base": "target_not_pinned"},
                description_placeholders={"price_targets_summary": "(none)"},
            )
        return self.async_show_form(
            step_id="price_target_add",
            data_schema=_schema_price_target_add(pinned),
            errors=errors,
        )

    async def async_step_price_target_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Remove one structured price target."""
        draft = self._ensure_draft()
        targets = normalize_price_targets(draft.get(CONF_PINNED_ITEM_PRICE_TARGETS, []))
        if user_input is not None:
            remove_id = str(user_input.get("item_id") or "").strip()
            draft[CONF_PINNED_ITEM_PRICE_TARGETS] = [
                row for row in targets if row["item_id"] != remove_id
            ]
            return await self.async_step_price_targets()
        if not targets:
            return await self.async_step_price_targets()
        return self.async_show_form(
            step_id="price_target_remove",
            data_schema=_schema_price_target_remove(targets),
        )

    async def async_step_selling(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Edit selling and analytics options."""
        draft = self._ensure_draft()
        if user_input is not None:
            draft.update(user_input)
            if not draft.get(CONF_SELLING_ENABLED):
                draft[CONF_ANALYTICS_ENABLED] = False
            return await self.async_step_init()
        return self.async_show_form(
            step_id="selling",
            data_schema=_schema_selling(draft),
            description_placeholders={
                "missing_scope_note": self._missing_scope_note(CONF_ANALYTICS_ENABLED),
            },
        )

    async def async_step_seller_operations(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Edit seller-operations module toggles."""
        draft = self._ensure_draft()
        if user_input is not None:
            draft.update(user_input)
            return await self.async_step_init()
        return self.async_show_form(
            step_id="seller_operations",
            data_schema=_schema_seller_operations(draft),
            description_placeholders={
                "missing_scope_note": self._missing_scope_note(
                    CONF_FULFILLMENT_ENABLED,
                    CONF_SELLER_STANDARDS_ENABLED,
                    CONF_FEEDBACK_ENABLED,
                    CONF_MESSAGES_ENABLED,
                    CONF_ACCOUNT_PRIVILEGES_ENABLED,
                    CONF_FINANCES_ENABLED,
                ),
            },
        )

    async def async_step_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Edit per-item entity selection options."""
        draft = self._ensure_draft()
        errors: dict[str, str] = {}
        listing_options = self._listing_select_options()
        if user_input is not None:
            candidate = {**draft, **user_input}
            errors = validate_pinned_item_options(candidate)
            if not errors:
                draft.update(user_input)
                draft[CONF_PINNED_ITEM_IDS] = normalize_pinned_item_ids(
                    draft.get(CONF_PINNED_ITEM_IDS, [])
                )
                # Drop price targets for pins that were removed.
                pinned = set(draft[CONF_PINNED_ITEM_IDS])
                draft[CONF_PINNED_ITEM_PRICE_TARGETS] = [
                    row
                    for row in normalize_price_targets(
                        draft.get(CONF_PINNED_ITEM_PRICE_TARGETS, [])
                    )
                    if row["item_id"] in pinned
                ]
                return await self.async_step_init()
            draft_for_form = candidate
        else:
            draft_for_form = draft
        return self.async_show_form(
            step_id="entities",
            data_schema=_schema_entities(draft_for_form, listing_options),
            errors=errors,
        )

    async def async_step_diagnostics(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show diagnostics guidance (no editable options)."""
        self._ensure_draft()
        if user_input is not None:
            return await self.async_step_init()
        return self.async_show_form(
            step_id="diagnostics",
            data_schema=vol.Schema({}),
        )

    async def async_step_save(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Validate the draft and persist options."""
        draft = self._ensure_draft()
        draft[CONF_PINNED_ITEM_IDS] = normalize_pinned_item_ids(
            draft.get(CONF_PINNED_ITEM_IDS, [])
        )
        try:
            draft[CONF_PINNED_ITEM_PRICE_TARGETS] = normalize_price_targets(
                draft.get(CONF_PINNED_ITEM_PRICE_TARGETS, [])
            )
        except ValueError:
            draft[CONF_PINNED_ITEM_PRICE_TARGETS] = []
        errors = validate_price_options(draft)
        errors.update(validate_pinned_item_options(draft))
        if errors:
            return self.async_show_form(
                step_id="save",
                data_schema=vol.Schema({}),
                errors=errors,
                description_placeholders={
                    "missing_scope_note": self._missing_scope_note(
                        CONF_ANALYTICS_ENABLED,
                        CONF_FULFILLMENT_ENABLED,
                        CONF_SELLER_STANDARDS_ENABLED,
                        CONF_FEEDBACK_ENABLED,
                        CONF_MESSAGES_ENABLED,
                        CONF_ACCOUNT_PRIVILEGES_ENABLED,
                        CONF_FINANCES_ENABLED,
                    ),
                },
            )

        if not draft.get(CONF_SELLING_ENABLED):
            draft[CONF_ANALYTICS_ENABLED] = False

        granted = self._config_entry.data.get(CONF_OAUTH_SCOPES)
        if missing_scopes_for_options(granted, draft):
            # Keep live options unchanged until reauth grants the new scopes.
            self._store_pending_options(dict(draft))
            self.hass.async_create_task(
                self.hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={
                        "source": config_entries.SOURCE_REAUTH,
                        "entry_id": self._config_entry.entry_id,
                    },
                    data=self._config_entry.data,
                )
            )
            return self.async_create_entry(
                title="", data=dict(self._config_entry.options)
            )

        self._clear_pending_options()
        return self.async_create_entry(title="", data=dict(draft))

    def _store_pending_options(self, draft: dict[str, Any]) -> None:
        """Persist draft options until reauth succeeds."""
        data = dict(self._config_entry.data)
        data[CONF_PENDING_OPTIONS] = draft
        self.hass.config_entries.async_update_entry(self._config_entry, data=data)

    def _clear_pending_options(self) -> None:
        """Drop any leftover pending options from entry data."""
        if CONF_PENDING_OPTIONS not in self._config_entry.data:
            return
        data = {
            key: value
            for key, value in self._config_entry.data.items()
            if key != CONF_PENDING_OPTIONS
        }
        self.hass.config_entries.async_update_entry(self._config_entry, data=data)


def _schema_general(options: dict[str, Any]) -> vol.Schema:
    """Build the General options schema."""
    return vol.Schema(
        {
            vol.Required(
                CONF_POLL_INTERVAL, default=options[CONF_POLL_INTERVAL]
            ): vol.All(vol.Coerce(int), vol.Range(min=5, max=1440)),
        }
    )


def _schema_buying_alerts(options: dict[str, Any]) -> vol.Schema:
    """Build the Buying and alerts options schema (globals only)."""
    return vol.Schema(
        {
            vol.Required(
                CONF_BUYING_ENABLED, default=options[CONF_BUYING_ENABLED]
            ): bool,
            vol.Required(
                CONF_ENDING_SOON_THRESHOLD,
                default=options[CONF_ENDING_SOON_THRESHOLD],
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
            vol.Optional(
                CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT,
                default=options.get(CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT, 0),
            ): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Optional(
                CONF_WATCHED_PRICE_DROP_THRESHOLD,
                default=options.get(CONF_WATCHED_PRICE_DROP_THRESHOLD, ""),
            ): str,
            vol.Optional(
                CONF_WATCHED_PRICE_DROP_CURRENCY,
                default=options.get(CONF_WATCHED_PRICE_DROP_CURRENCY, ""),
            ): str,
        }
    )


def _schema_selling(options: dict[str, Any]) -> vol.Schema:
    """Build the Selling options schema."""
    return vol.Schema(
        {
            vol.Required(
                CONF_SELLING_ENABLED, default=options[CONF_SELLING_ENABLED]
            ): bool,
            vol.Required(
                CONF_ANALYTICS_ENABLED, default=options[CONF_ANALYTICS_ENABLED]
            ): bool,
        }
    )


def _schema_seller_operations(options: dict[str, Any]) -> vol.Schema:
    """Build the Seller operations options schema."""
    return vol.Schema(
        {
            vol.Required(
                CONF_FULFILLMENT_ENABLED,
                default=options[CONF_FULFILLMENT_ENABLED],
            ): bool,
            vol.Required(
                CONF_SELLER_STANDARDS_ENABLED,
                default=options[CONF_SELLER_STANDARDS_ENABLED],
            ): bool,
            vol.Required(
                CONF_FEEDBACK_ENABLED, default=options[CONF_FEEDBACK_ENABLED]
            ): bool,
            vol.Required(
                CONF_MESSAGES_ENABLED, default=options[CONF_MESSAGES_ENABLED]
            ): bool,
            vol.Required(
                CONF_ACCOUNT_PRIVILEGES_ENABLED,
                default=options[CONF_ACCOUNT_PRIVILEGES_ENABLED],
            ): bool,
            vol.Required(
                CONF_FINANCES_ENABLED, default=options[CONF_FINANCES_ENABLED]
            ): bool,
        }
    )


def _schema_entities(
    options: dict[str, Any],
    listing_options: list[selector.SelectOptionDict],
) -> vol.Schema:
    """Build the Entities options schema with a multi-select for pins."""
    return vol.Schema(
        {
            vol.Required(CONF_ENTITY_MODE, default=options[CONF_ENTITY_MODE]): vol.In(
                ENTITY_MODES
            ),
            vol.Required(CONF_PER_ITEM_CAP, default=options[CONF_PER_ITEM_CAP]): vol.In(
                ALLOWED_PER_ITEM_CAPS
            ),
            vol.Optional(
                CONF_PINNED_ITEM_IDS,
                default=normalize_pinned_item_ids(
                    options.get(CONF_PINNED_ITEM_IDS, [])
                ),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=listing_options,
                    multiple=True,
                    custom_value=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


def _schema_price_target_add(pinned: list[str]) -> vol.Schema:
    """Build the add price-target schema from currently pinned IDs."""
    pin_options = [
        selector.SelectOptionDict(value=item_id, label=item_id) for item_id in pinned
    ]
    currency_options = [
        selector.SelectOptionDict(value=code, label=code) for code in COMMON_CURRENCIES
    ]
    return vol.Schema(
        {
            vol.Required("item_id"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=pin_options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required("amount"): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    step=0.01,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required("currency", default="USD"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=currency_options,
                    custom_value=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


def _schema_price_target_remove(targets: list[dict[str, str]]) -> vol.Schema:
    """Build the remove price-target schema."""
    options = [
        selector.SelectOptionDict(
            value=row["item_id"],
            label=f"{row['item_id']} = {row['amount']} {row['currency']}",
        )
        for row in targets
    ]
    return vol.Schema(
        {
            vol.Required("item_id"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )
