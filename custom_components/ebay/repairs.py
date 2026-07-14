"""Home Assistant Repairs for persistent eBay integration problems."""

from __future__ import annotations

from typing import Any, TypedDict

import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store

from .const import (
    CONF_ANALYTICS_ENABLED,
    CONF_BUYING_ENABLED,
    CONF_FEEDBACK_ENABLED,
    CONF_FULFILLMENT_ENABLED,
    CONF_MESSAGES_ENABLED,
    CONF_SELLER_STANDARDS_ENABLED,
    CONF_SELLING_ENABLED,
    CONF_SITE_ID,
    DOMAIN,
    PARTIAL_FAILURE_SECTION,
    REPAIR_STREAK_THRESHOLD,
    TRUNCATION_COLLECTION_SECTION,
)
from .seller_ops import SITE_ID_TO_MARKETPLACE, is_known_site_id

REPAIR_STREAKS_STORAGE_VERSION = 1


class RepairStreaksStoreData(TypedDict):
    """Persisted repair streak counters for a config entry."""

    streaks: dict[str, int]


FEATURE_LABELS = {
    CONF_ANALYTICS_ENABLED: "Sell Analytics views",
    CONF_FULFILLMENT_ENABLED: "orders and payment disputes",
    CONF_SELLER_STANDARDS_ENABLED: "seller standards",
    CONF_FEEDBACK_ENABLED: "feedback",
    CONF_MESSAGES_ENABLED: "buyer messages",
}

SELLER_OPS_UNAVAILABLE_CATEGORIES = {
    "orders": "orders",
    "payment_disputes": "payment_disputes",
    "seller_standards": "seller_standards",
    "feedback": "feedback",
    "messages": "messages",
}

# Soft-fail categories that are covered by truncation or missing-scope issues.
_SKIP_PARTIAL_FAILURE_CATEGORIES = frozenset(
    {
        "buying_truncated",
        "selling_truncated",
        "sold_unsold_truncated",
        "seller_list_views_truncated",
        "active_offers_truncated",
        "orders_truncated",
        "payment_disputes_truncated",
        "messages_truncated",
        "analytics_views_missing_scope",
        "orders_missing_scope",
        "payment_disputes_missing_scope",
        "seller_standards_missing_scope",
        "feedback_missing_scope",
        "messages_missing_scope",
        *SELLER_OPS_UNAVAILABLE_CATEGORIES,
    }
)


def issue_id_for(entry_id: str, suffix: str) -> str:
    """Return a config-entry-scoped issue id."""
    return f"{entry_id}_{suffix}"


def parse_issue_suffix(entry_id: str, issue_id: str) -> str | None:
    """Return the issue suffix for an entry-scoped issue id."""
    prefix = f"{entry_id}_"
    if not issue_id.startswith(prefix):
        return None
    return issue_id[len(prefix) :]


def repair_streaks_store(
    hass: HomeAssistant, entry_id: str
) -> Store[RepairStreaksStoreData]:
    """Return the storage helper for an entry's repair streaks."""
    return Store(
        hass,
        REPAIR_STREAKS_STORAGE_VERSION,
        f"{DOMAIN}.{entry_id}.repair_streaks",
    )


async def async_load_repair_streaks(
    hass: HomeAssistant, entry_id: str
) -> dict[str, int]:
    """Load persisted repair streak counters for a config entry."""
    data = await repair_streaks_store(hass, entry_id).async_load()
    if not isinstance(data, dict):
        return {}
    raw = data.get("streaks")
    if not isinstance(raw, dict):
        return {}
    streaks: dict[str, int] = {}
    for key, value in raw.items():
        try:
            streaks[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return streaks


async def async_save_repair_streaks(
    hass: HomeAssistant, entry_id: str, streaks: dict[str, int]
) -> None:
    """Persist repair streak counters for a config entry."""
    payload: RepairStreaksStoreData = {"streaks": dict(streaks)}
    await repair_streaks_store(hass, entry_id).async_save(payload)


async def async_remove_repair_streaks(hass: HomeAssistant, entry_id: str) -> None:
    """Remove persisted repair streak counters for a config entry."""
    await repair_streaks_store(hass, entry_id).async_remove()


@callback
def async_delete_entry_issues(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete all known eBay repairs for a config entry."""
    try:
        registry = ir.async_get(hass)
    except (AttributeError, TypeError):
        return
    prefix = f"{entry.entry_id}_"
    for issue in list(registry.issues.values()):
        if issue.domain != DOMAIN:
            continue
        if issue.issue_id.startswith(prefix):
            ir.async_delete_issue(hass, DOMAIN, issue.issue_id)


async def async_sync_reauth_issue(
    hass: HomeAssistant, entry: ConfigEntry, *, active: bool
) -> None:
    """Create or clear the persistent reauthorization repair."""
    issue_id = issue_id_for(entry.entry_id, "reauthorization_required")
    if active:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.ERROR,
            translation_key="reauthorization_required",
            data={"entry_id": entry.entry_id},
        )
        return
    ir.async_delete_issue(hass, DOMAIN, issue_id)


def _bump_streak(
    coordinator: Any, key: str, *, active: bool, observed: bool = True
) -> int:
    """Update and return the consecutive-observation streak for a condition.

    When ``observed`` is False the streak is left unchanged (section was not
    attempted on this refresh).
    """
    streaks: dict[str, int] = coordinator._repair_streaks
    if not observed:
        return streaks.get(key, 0)
    if not active:
        if key in streaks:
            streaks.pop(key, None)
            coordinator._repair_streaks_dirty = True
        return 0
    streaks[key] = streaks.get(key, 0) + 1
    coordinator._repair_streaks_dirty = True
    return streaks[key]


def _section_observed(refreshed_sections: set[str] | None, section: str | None) -> bool:
    """Return whether the owning section was attempted on this refresh.

    ``None`` refreshed_sections means observe all (legacy/tests).
    """
    if refreshed_sections is None or section is None:
        return True
    return section in refreshed_sections


def _enabled_sections_for_repairs(coordinator: Any) -> set[str] | None:
    """Return currently enabled payload sections, or None when unknown.

    ``None`` means every section is treated as enabled (test doubles without
    real options). Disabled sections clear their repairs instead of preserving
    them while unobserved.
    """
    options = getattr(coordinator, "options", None)
    if not isinstance(options, dict):
        return None
    from .api import enabled_sections_for_options

    return enabled_sections_for_options(
        buying_enabled=bool(options.get(CONF_BUYING_ENABLED)),
        selling_enabled=bool(options.get(CONF_SELLING_ENABLED)),
        analytics_enabled=bool(options.get(CONF_ANALYTICS_ENABLED)),
        fulfillment_enabled=bool(options.get(CONF_FULFILLMENT_ENABLED)),
        seller_standards_enabled=bool(options.get(CONF_SELLER_STANDARDS_ENABLED)),
        feedback_enabled=bool(options.get(CONF_FEEDBACK_ENABLED)),
        messages_enabled=bool(options.get(CONF_MESSAGES_ENABLED)),
    )


def _section_enabled(enabled_sections: set[str] | None, section: str | None) -> bool:
    """Return whether repairs for ``section`` should remain active."""
    if enabled_sections is None or section is None:
        return True
    return section in enabled_sections


def _section_for_streak_key(streak_key: str) -> str | None:
    """Return the owning section for a repair streak key, if known."""
    kind, _, name = streak_key.partition(":")
    if not name:
        return None
    if kind == "truncated":
        return TRUNCATION_COLLECTION_SECTION.get(name)
    if kind in ("seller_ops_unavailable", "partial_failure"):
        return PARTIAL_FAILURE_SECTION.get(name)
    return None


def _preserve_issue_if_present(
    registry: Any, domain: str, issue_id: str, desired: set[str]
) -> None:
    """Keep an existing issue in the desired set without recreating it."""
    if (domain, issue_id) in getattr(registry, "issues", {}):
        desired.add(issue_id)
        return
    # Home Assistant IssueRegistry stores issues keyed by (domain, issue_id).
    try:
        for issue in registry.issues.values():
            if issue.domain == domain and issue.issue_id == issue_id:
                desired.add(issue_id)
                return
    except (AttributeError, TypeError):
        return


async def async_sync_repair_issues(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: Any,
    payload: dict[str, Any],
    *,
    refreshed_sections: set[str] | None = None,
) -> None:
    """Create or delete repairs from the latest coordinator payload.

    Soft-fail / truncation streaks only advance when the owning section is in
    ``refreshed_sections``. Pass ``None`` to treat every section as observed.
    Disabled optional sections clear their issues and streaks immediately.
    """
    await async_sync_reauth_issue(hass, entry, active=False)

    desired: set[str] = set()
    entry_id = entry.entry_id
    registry = ir.async_get(hass)
    enabled_sections = _enabled_sections_for_repairs(coordinator)

    # Invalid Site ID / marketplace mapping.
    site_id = str(entry.data.get(CONF_SITE_ID, ""))
    if not is_known_site_id(site_id):
        suffix = "invalid_site_id"
        issue_id = issue_id_for(entry_id, suffix)
        desired.add(issue_id)
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            severity=ir.IssueSeverity.ERROR,
            translation_key="invalid_site_id",
            translation_placeholders={"site_id": site_id or "(empty)"},
            data={"entry_id": entry_id},
        )

    # Missing OAuth scopes for enabled optional features.
    missing_features = payload.get("missing_scope_features") or []
    missing_scopes = payload.get("missing_scopes") or {}
    for feature in missing_features:
        suffix = f"missing_scope_{feature}"
        issue_id = issue_id_for(entry_id, suffix)
        desired.add(issue_id)
        scopes = ", ".join(missing_scopes.get(feature) or [])
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="missing_scope",
            translation_placeholders={
                "feature": FEATURE_LABELS.get(feature, feature),
                "scopes": scopes or "(unknown)",
            },
            data={"entry_id": entry_id, "feature": feature},
        )

    partial_failures = set(payload.get("partial_failures") or [])
    truncated = payload.get("truncated_collections") or {}

    # Consistent truncation.
    for collection, is_truncated in truncated.items():
        streak_key = f"truncated:{collection}"
        section = TRUNCATION_COLLECTION_SECTION.get(collection)
        if not _section_enabled(enabled_sections, section):
            _bump_streak(coordinator, streak_key, active=False, observed=True)
            continue
        observed = _section_observed(refreshed_sections, section)
        streak = _bump_streak(
            coordinator, streak_key, active=bool(is_truncated), observed=observed
        )
        suffix = f"truncated_{collection}"
        issue_id = issue_id_for(entry_id, suffix)
        if not observed:
            _preserve_issue_if_present(registry, DOMAIN, issue_id, desired)
            continue
        if streak < REPAIR_STREAK_THRESHOLD:
            continue
        desired.add(issue_id)
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="truncated_collection",
            translation_placeholders={"collection": collection},
            data={"entry_id": entry_id, "collection": collection},
        )

    # Seller-operation API unavailable for the account.
    for category, feature in SELLER_OPS_UNAVAILABLE_CATEGORIES.items():
        streak_key = f"seller_ops_unavailable:{feature}"
        section = PARTIAL_FAILURE_SECTION.get(category)
        if not _section_enabled(enabled_sections, section):
            _bump_streak(coordinator, streak_key, active=False, observed=True)
            continue
        active = category in partial_failures
        observed = _section_observed(refreshed_sections, section)
        streak = _bump_streak(coordinator, streak_key, active=active, observed=observed)
        suffix = f"seller_ops_unavailable_{feature}"
        issue_id = issue_id_for(entry_id, suffix)
        if not observed:
            _preserve_issue_if_present(registry, DOMAIN, issue_id, desired)
            continue
        if streak < REPAIR_STREAK_THRESHOLD:
            continue
        desired.add(issue_id)
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="seller_ops_unavailable",
            translation_placeholders={
                "feature": FEATURE_LABELS.get(
                    {
                        "orders": CONF_FULFILLMENT_ENABLED,
                        "payment_disputes": CONF_FULFILLMENT_ENABLED,
                        "seller_standards": CONF_SELLER_STANDARDS_ENABLED,
                        "feedback": CONF_FEEDBACK_ENABLED,
                        "messages": CONF_MESSAGES_ENABLED,
                    }.get(feature, feature),
                    feature,
                )
            },
            data={"entry_id": entry_id, "feature": feature},
        )

    # Repeated partial failures (excluding truncation / scope / seller-ops).
    for category in partial_failures:
        if category in _SKIP_PARTIAL_FAILURE_CATEGORIES:
            continue
        streak_key = f"partial_failure:{category}"
        section = PARTIAL_FAILURE_SECTION.get(category)
        if not _section_enabled(enabled_sections, section):
            _bump_streak(coordinator, streak_key, active=False, observed=True)
            continue
        observed = _section_observed(refreshed_sections, section)
        streak = _bump_streak(coordinator, streak_key, active=True, observed=observed)
        suffix = f"partial_failure_{category}"
        issue_id = issue_id_for(entry_id, suffix)
        if not observed:
            _preserve_issue_if_present(registry, DOMAIN, issue_id, desired)
            continue
        if streak < REPAIR_STREAK_THRESHOLD:
            continue
        desired.add(issue_id)
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="partial_failure",
            translation_placeholders={"category": category},
            data={"entry_id": entry_id, "category": category},
        )

    # Reset streaks for soft-fail categories that cleared (only when observed).
    for category in list(SELLER_OPS_UNAVAILABLE_CATEGORIES):
        section = PARTIAL_FAILURE_SECTION.get(category)
        if not _section_enabled(enabled_sections, section):
            continue
        if not _section_observed(refreshed_sections, section):
            continue
        if category not in partial_failures:
            _bump_streak(
                coordinator, f"seller_ops_unavailable:{category}", active=False
            )
    # Clear streaks for disabled sections and cleared partial failures.
    for streak_key in list(coordinator._repair_streaks):
        section = _section_for_streak_key(streak_key)
        if not _section_enabled(enabled_sections, section):
            _bump_streak(coordinator, streak_key, active=False, observed=True)
            continue
        if streak_key.startswith("partial_failure:"):
            category = streak_key.split(":", 1)[1]
            if not _section_observed(refreshed_sections, section):
                continue
            if category not in partial_failures:
                _bump_streak(coordinator, streak_key, active=False)

    # Delete issues that are no longer desired (except reauth handled above).
    prefix = f"{entry_id}_"
    for issue in list(registry.issues.values()):
        if issue.domain != DOMAIN or not issue.issue_id.startswith(prefix):
            continue
        if issue.issue_id.endswith("_reauthorization_required"):
            continue
        if issue.issue_id not in desired:
            ir.async_delete_issue(hass, DOMAIN, issue.issue_id)

    if getattr(coordinator, "_repair_streaks_dirty", False):
        await async_save_repair_streaks(hass, entry_id, coordinator._repair_streaks)
        coordinator._repair_streaks_dirty = False


async def _async_start_reauth(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Start the config-entry reauth flow."""
    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
    )


class ReauthRepairFlow(RepairsFlow):
    """Confirm and start eBay reauthorization."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Handle the first step of a fix flow."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Confirm reauthorization."""
        if user_input is not None:
            await _async_start_reauth(self.hass, self._entry)
            return self.async_create_entry(title="", data={})
        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}))


class InvalidSiteIdRepairFlow(RepairsFlow):
    """Let the user pick a known Site ID."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Handle the first step of a fix flow."""
        return await self.async_step_site_id()

    async def async_step_site_id(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Collect a known Site ID and reload the entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            site_id = str(user_input.get(CONF_SITE_ID, "")).strip()
            if not is_known_site_id(site_id):
                errors["base"] = "invalid_site_id"
            else:
                data = dict(self._entry.data)
                data[CONF_SITE_ID] = site_id
                self.hass.config_entries.async_update_entry(self._entry, data=data)
                self.hass.async_create_task(
                    self.hass.config_entries.async_reload(self._entry.entry_id)
                )
                return self.async_create_entry(title="", data={})

        known_ids = sorted(SITE_ID_TO_MARKETPLACE, key=lambda value: int(value))
        return self.async_show_form(
            step_id="site_id",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SITE_ID,
                        default=str(self._entry.data.get(CONF_SITE_ID, "0")),
                    ): vol.In(known_ids)
                }
            ),
            errors=errors,
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create a repair flow for a fixable issue."""
    entry_id = None if data is None else data.get("entry_id")
    entry = hass.config_entries.async_get_entry(str(entry_id)) if entry_id else None
    if entry is None:
        # Fall back to parsing the issue id prefix.
        for candidate in hass.config_entries.async_entries(DOMAIN):
            if issue_id.startswith(f"{candidate.entry_id}_"):
                entry = candidate
                break
    if entry is None:
        from homeassistant.components.repairs import ConfirmRepairFlow

        return ConfirmRepairFlow()

    suffix = parse_issue_suffix(entry.entry_id, issue_id) or issue_id
    if suffix == "reauthorization_required" or suffix.startswith("missing_scope_"):
        return ReauthRepairFlow(entry)
    if suffix == "invalid_site_id":
        return InvalidSiteIdRepairFlow(entry)
    from homeassistant.components.repairs import ConfirmRepairFlow

    return ConfirmRepairFlow()
