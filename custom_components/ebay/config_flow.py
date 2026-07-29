"""Config flow for the eBay integration."""

from __future__ import annotations

import logging
import secrets
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow, selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    CORE_SCOPE,
    EbayApiClient,
    EbayAuthError,
    build_consent_url,
    extract_authorization_code,
    extract_oauth_callback_params,
    missing_scopes_for_options,
    resolve_granted_scopes,
    scopes_for_options,
)
from .const import (
    ALLOWED_PER_ITEM_CAPS,
    CONF_ACCOUNT_PRIVILEGES_ENABLED,
    CONF_ANALYTICS_ENABLED,
    CONF_BUYING_ENABLED,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ENDING_SOON_THRESHOLD,
    CONF_ENTITY_MODE,
    CONF_ENVIRONMENT,
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
    CONF_RUNAME,
    CONF_SELLER_STANDARDS_ENABLED,
    CONF_SELLING_ENABLED,
    CONF_SITE_ID,
    CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT,
    CONF_WATCHED_PRICE_DROP_CURRENCY,
    CONF_WATCHED_PRICE_DROP_THRESHOLD,
    DEFAULT_OPTIONS,
    DEFAULT_SITE_ID,
    DOMAIN,
    ENTITY_MODES,
    ENVIRONMENTS,
    OAUTH_MODE_CALLBACK,
    OAUTH_MODE_MANUAL,
)
from .oauth2 import (
    DEVELOPER_CONSOLE_URL,
    OAUTH_SETUP_GUIDE_URL,
    PRIVACY_POLICY_URL,
    EbayOAuth2Implementation,
    async_get_ebay_callback_url,
    normalize_new_token,
)
from .options_parse import (
    COMMON_CURRENCIES,
    normalize_pinned_item_ids,
    normalize_price_targets,
    validate_pinned_item_options,
    validate_price_options,
)

_LOGGER = logging.getLogger(__name__)


class EbayConfigFlow(config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN):
    """Handle an eBay config flow."""

    DOMAIN = DOMAIN
    VERSION = 4

    def __init__(self) -> None:
        """Initialize the flow."""
        super().__init__()
        self._data: dict[str, Any] = {}
        self._state = secrets.token_urlsafe(24)
        self._consent_url: str | None = None
        self._oauth_mode = OAUTH_MODE_CALLBACK
        self._requested_scopes = CORE_SCOPE

    @property
    def logger(self) -> logging.Logger:
        """Return logger."""
        return _LOGGER

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Start the guided setup flow."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["automatic", "manual"],
        )

    async def async_step_authorization_method(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Choose automatic callback or manual authorization."""
        return await self.async_step_user(user_input)

    async def async_step_automatic(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Use Home Assistant's native OAuth callback."""
        self._oauth_mode = OAUTH_MODE_CALLBACK
        return await self.async_step_prepare_application()

    async def async_step_prepare_application(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show eBay developer-console setup instructions before credentials."""
        if user_input is not None:
            return await self.async_step_credentials()

        return self.async_show_form(
            step_id="prepare_application",
            data_schema=vol.Schema({}),
            description_placeholders=self._setup_description_placeholders(),
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Collect eBay app settings."""
        if user_input is not None:
            self._data, errors = _normalize_credentials(user_input)
            if errors:
                return self.async_show_form(
                    step_id="credentials",
                    data_schema=self._credentials_schema(self._data),
                    errors=errors,
                )
            await self.async_set_unique_id(
                f"{self._data[CONF_ENVIRONMENT]}:{self._data[CONF_CLIENT_ID]}"
            )
            if self.source != config_entries.SOURCE_REAUTH:
                self._abort_if_unique_id_configured()

            self._requested_scopes = self._scopes_for_authorization()
            self.flow_impl = EbayOAuth2Implementation(
                self.hass, self._data, scope=self._requested_scopes
            )
            if self._oauth_mode == OAUTH_MODE_MANUAL:
                self._consent_url = build_consent_url(
                    self._data[CONF_ENVIRONMENT],
                    self._data[CONF_CLIENT_ID],
                    self._data[CONF_RUNAME],
                    self._state,
                    scope=self._requested_scopes,
                )
                return await self.async_step_manual()
            return await self.async_step_auth()

        defaults = self._reauth_defaults()
        return self.async_show_form(
            step_id="credentials",
            data_schema=self._credentials_schema(defaults),
        )

    async def async_oauth_create_entry(
        self, data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Create or update an eBay entry after native OAuth completes."""
        token = normalize_new_token(data["token"])
        entry_data = {
            **self._data,
            "auth_implementation": data["auth_implementation"],
            "token": token,
            CONF_OAUTH_SCOPES: resolve_granted_scopes(self._requested_scopes, token),
        }
        if self.source == config_entries.SOURCE_REAUTH:
            return self._finish_reauth(entry_data)
        return self.async_create_entry(
            title="eBay",
            data=entry_data,
            options=DEFAULT_OPTIONS,
        )

    def async_remove(self) -> None:
        """Discard pending options when reauth is cancelled or fails."""
        if self.source != config_entries.SOURCE_REAUTH:
            return
        try:
            entry = self._get_reauth_entry()
        except config_entries.UnknownEntry:
            return
        if CONF_PENDING_OPTIONS not in entry.data:
            return
        data = {
            key: value
            for key, value in entry.data.items()
            if key != CONF_PENDING_OPTIONS
        }
        self.hass.config_entries.async_update_entry(entry, data=data)

    def _finish_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Update the reauth entry, committing pending options when present."""
        entry = self._get_reauth_entry()
        pending = entry.data.get(CONF_PENDING_OPTIONS)
        cleaned = {
            key: value
            for key, value in entry_data.items()
            if key != CONF_PENDING_OPTIONS
        }
        if isinstance(pending, dict):
            return self.async_update_reload_and_abort(
                entry, data=cleaned, options=dict(pending)
            )
        return self.async_update_reload_and_abort(entry, data=cleaned)

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Start reauthorization after token refresh fails."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Confirm before collecting credentials for reauth."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        self._oauth_mode = OAUTH_MODE_CALLBACK
        return await self.async_step_credentials()

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Choose or complete manual OAuth authorization."""
        if user_input is None and not self._data:
            self._oauth_mode = OAUTH_MODE_MANUAL
            return await self.async_step_credentials()

        errors: dict[str, str] = {}
        if user_input is not None:
            authorization_value = user_input["authorization_code"]
            callback_params = extract_oauth_callback_params(authorization_value)
            state = callback_params.get("state", [None])[0]
            if callback_params and state != self._state:
                errors["base"] = "invalid_state"
                return self._manual_form(errors)
            try:
                code = extract_authorization_code(authorization_value)
                client = EbayApiClient(
                    async_get_clientsession(self.hass),
                    environment=self._data[CONF_ENVIRONMENT],
                    client_id=self._data[CONF_CLIENT_ID],
                    client_secret=self._data[CONF_CLIENT_SECRET],
                    runame=self._data[CONF_RUNAME],
                    refresh_token=None,
                    site_id=self._data[CONF_SITE_ID],
                )
                payload = await client.async_exchange_authorization_code(code)
            except EbayAuthError:
                errors["base"] = "cannot_connect"
            else:
                token = normalize_new_token(payload)
                entry_data = {
                    **self._data,
                    "auth_implementation": self.flow_impl.domain,
                    "token": token,
                    CONF_OAUTH_SCOPES: resolve_granted_scopes(
                        self._requested_scopes, token
                    ),
                }
                if self.source == config_entries.SOURCE_REAUTH:
                    return self._finish_reauth(entry_data)
                return self.async_create_entry(
                    title="eBay",
                    data=entry_data,
                    options=DEFAULT_OPTIONS,
                )

        return self._manual_form(errors)

    async def async_step_oauth(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Backward-compatible alias for in-progress legacy manual flows."""
        return await self.async_step_manual(user_input)

    def _manual_form(self, errors: dict[str, str]) -> config_entries.ConfigFlowResult:
        """Return the manual OAuth fallback form."""
        return self.async_show_form(
            step_id="manual",
            description_placeholders={
                "consent_url": self._consent_url or "",
            },
            data_schema=vol.Schema({vol.Required("authorization_code"): str}),
            errors=errors,
        )

    def _credentials_schema(self, defaults: dict[str, Any]) -> vol.Schema:
        """Return the eBay application credential schema."""
        return vol.Schema(
            {
                vol.Required(
                    CONF_ENVIRONMENT,
                    default=defaults.get(CONF_ENVIRONMENT, "production"),
                ): vol.In(ENVIRONMENTS),
                vol.Required(
                    CONF_CLIENT_ID,
                    default=defaults.get(CONF_CLIENT_ID, ""),
                ): str,
                vol.Required(CONF_CLIENT_SECRET, default=""): str,
                vol.Required(
                    CONF_RUNAME,
                    default=defaults.get(CONF_RUNAME, ""),
                ): str,
                vol.Required(
                    CONF_SITE_ID,
                    default=defaults.get(CONF_SITE_ID, DEFAULT_SITE_ID),
                ): str,
            }
        )

    def _setup_description_placeholders(self) -> dict[str, str]:
        """Return setup instruction URLs for translation placeholders."""
        return {
            "callback_url": async_get_ebay_callback_url(self.hass),
            "developer_console_url": DEVELOPER_CONSOLE_URL,
            "oauth_setup_guide_url": OAUTH_SETUP_GUIDE_URL,
            "privacy_policy_url": PRIVACY_POLICY_URL,
        }

    def _reauth_defaults(self) -> dict[str, Any]:
        """Return defaults from the existing entry during reauth."""
        if self.source != config_entries.SOURCE_REAUTH:
            return {}
        return dict(self._get_reauth_entry().data)

    def _scopes_for_authorization(self) -> str:
        """Return scopes for the current authorize/reauth attempt."""
        if self.source != config_entries.SOURCE_REAUTH:
            return CORE_SCOPE
        entry = self._get_reauth_entry()
        pending = entry.data.get(CONF_PENDING_OPTIONS)
        if isinstance(pending, dict):
            options = {**DEFAULT_OPTIONS, **pending}
        else:
            options = {**DEFAULT_OPTIONS, **dict(getattr(entry, "options", {}) or {})}
        return scopes_for_options(options)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return options flow."""
        return EbayOptionsFlow(config_entry)


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


def _normalize_credentials(
    user_input: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Normalize credential form input and report blank required fields."""
    data = dict(user_input)
    errors: dict[str, str] = {}
    for field in (
        CONF_CLIENT_ID,
        CONF_CLIENT_SECRET,
        CONF_RUNAME,
        CONF_SITE_ID,
    ):
        raw_value = data.get(field)
        value = "" if raw_value is None else str(raw_value).strip()
        data[field] = value
        if not value:
            errors[field] = "required"
    return data, errors
