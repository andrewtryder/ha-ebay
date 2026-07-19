"""OAuth scope constants and helpers."""

from __future__ import annotations

from typing import Any

from ..const import (
    CONF_ACCOUNT_PRIVILEGES_ENABLED,
    CONF_ANALYTICS_ENABLED,
    CONF_FEEDBACK_ENABLED,
    CONF_FINANCES_ENABLED,
    CONF_FULFILLMENT_ENABLED,
    CONF_MESSAGES_ENABLED,
    CONF_SELLER_STANDARDS_ENABLED,
    CONF_SELLING_ENABLED,
)

CORE_SCOPE = "https://api.ebay.com/oauth/api_scope"
SCOPE_ANALYTICS_READONLY = (
    "https://api.ebay.com/oauth/api_scope/sell.analytics.readonly"
)
SCOPE_FULFILLMENT_READONLY = (
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly"
)
SCOPE_PAYMENT_DISPUTE = "https://api.ebay.com/oauth/api_scope/sell.payment.dispute"
SCOPE_FEEDBACK = "https://api.ebay.com/oauth/api_scope/commerce.feedback"
SCOPE_MESSAGE = "https://api.ebay.com/oauth/api_scope/commerce.message"
SCOPE_ACCOUNT_READONLY = "https://api.ebay.com/oauth/api_scope/sell.account.readonly"
SCOPE_FINANCES = "https://api.ebay.com/oauth/api_scope/sell.finances"

MODULE_SCOPES: dict[str, tuple[str, ...]] = {
    CONF_ANALYTICS_ENABLED: (SCOPE_ANALYTICS_READONLY,),
    CONF_SELLER_STANDARDS_ENABLED: (SCOPE_ANALYTICS_READONLY,),
    CONF_FULFILLMENT_ENABLED: (SCOPE_FULFILLMENT_READONLY, SCOPE_PAYMENT_DISPUTE),
    CONF_FEEDBACK_ENABLED: (SCOPE_FEEDBACK,),
    CONF_MESSAGES_ENABLED: (SCOPE_MESSAGE,),
    CONF_ACCOUNT_PRIVILEGES_ENABLED: (SCOPE_ACCOUNT_READONLY,),
    CONF_FINANCES_ENABLED: (SCOPE_FINANCES,),
}

ALL_OPTIONAL_SCOPES = (
    SCOPE_ANALYTICS_READONLY,
    SCOPE_FULFILLMENT_READONLY,
    SCOPE_PAYMENT_DISPUTE,
    SCOPE_FEEDBACK,
    SCOPE_MESSAGE,
    SCOPE_ACCOUNT_READONLY,
    SCOPE_FINANCES,
)


def parse_scope_set(scopes: str | None) -> set[str]:
    """Parse a space-delimited OAuth scope string into a set."""
    if not scopes:
        return set()
    return {scope for scope in scopes.split() if scope}


def join_scopes(scopes: list[str] | tuple[str, ...] | set[str]) -> str:
    """Join scopes into a stable space-delimited string."""
    if isinstance(scopes, set):
        ordered = sorted(scopes)
    else:
        ordered = list(dict.fromkeys(scopes))
    return " ".join(ordered)


def scopes_for_options(options: dict[str, Any]) -> str:
    """Return the OAuth scope string required by enabled feature options."""
    scopes: list[str] = [CORE_SCOPE]
    for option_key, option_scopes in MODULE_SCOPES.items():
        if not options.get(option_key):
            continue
        # Analytics is only useful/scheduled when Selling is also enabled.
        if option_key == CONF_ANALYTICS_ENABLED and not options.get(
            CONF_SELLING_ENABLED
        ):
            continue
        scopes.extend(option_scopes)
    return join_scopes(scopes)


def missing_scopes_for_options(
    granted: str | set[str] | None, options: dict[str, Any]
) -> dict[str, list[str]]:
    """Return enabled modules whose required scopes are not all granted."""
    granted_set = granted if isinstance(granted, set) else parse_scope_set(granted)
    missing: dict[str, list[str]] = {}
    for option_key, option_scopes in MODULE_SCOPES.items():
        if not options.get(option_key):
            continue
        if option_key == CONF_ANALYTICS_ENABLED and not options.get(
            CONF_SELLING_ENABLED
        ):
            continue
        absent = [scope for scope in option_scopes if scope not in granted_set]
        if absent:
            missing[option_key] = absent
    return missing


def resolve_granted_scopes(requested: str, token: dict[str, Any] | None = None) -> str:
    """Prefer scopes returned by eBay; otherwise store the requested set."""
    if token:
        returned = token.get("scope")
        if isinstance(returned, str) and returned.strip():
            return join_scopes(parse_scope_set(returned))
    return join_scopes(parse_scope_set(requested))


def has_core_scope(scopes: str | None) -> bool:
    """Return True when the stored grant includes the core Trading API scope."""
    return CORE_SCOPE in parse_scope_set(scopes)


# Full union retained for docs/tests; routine authorize uses CORE_SCOPE or
# scopes_for_options().
DEFAULT_SCOPE = join_scopes((CORE_SCOPE, *ALL_OPTIONAL_SCOPES))
