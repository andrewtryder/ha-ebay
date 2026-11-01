"""eBay environment endpoint URLs and OAuth consent helpers."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse

from ..const import ENV_SANDBOX
from .scopes import CORE_SCOPE


@dataclass
class EbayEndpoints:
    """Endpoint set for an eBay environment."""

    trading: str
    token: str
    authorize: str
    analytics: str
    analytics_base: str
    fulfillment: str
    payment_disputes: str
    feedback: str
    messages: str
    account: str
    finances: str


def endpoints_for(environment: str) -> EbayEndpoints:
    """Return endpoint URLs for production or sandbox."""
    if environment == ENV_SANDBOX:
        return EbayEndpoints(
            trading="https://api.sandbox.ebay.com/ws/api.dll",
            token="https://api.sandbox.ebay.com/identity/v1/oauth2/token",
            authorize="https://auth.sandbox.ebay.com/oauth2/authorize",
            analytics="https://api.sandbox.ebay.com/sell/analytics/v1/traffic_report",
            analytics_base="https://api.sandbox.ebay.com/sell/analytics/v1",
            fulfillment="https://api.sandbox.ebay.com/sell/fulfillment/v1",
            payment_disputes="https://apiz.sandbox.ebay.com/sell/fulfillment/v1",
            feedback="https://api.sandbox.ebay.com/commerce/feedback/v1",
            messages="https://api.sandbox.ebay.com/commerce/message/v1",
            account="https://api.sandbox.ebay.com/sell/account/v1",
            finances="https://apiz.sandbox.ebay.com/sell/finances/v1",
        )
    return EbayEndpoints(
        trading="https://api.ebay.com/ws/api.dll",
        token="https://api.ebay.com/identity/v1/oauth2/token",
        authorize="https://auth.ebay.com/oauth2/authorize",
        analytics="https://api.ebay.com/sell/analytics/v1/traffic_report",
        analytics_base="https://api.ebay.com/sell/analytics/v1",
        fulfillment="https://api.ebay.com/sell/fulfillment/v1",
        payment_disputes="https://apiz.ebay.com/sell/fulfillment/v1",
        feedback="https://api.ebay.com/commerce/feedback/v1",
        messages="https://api.ebay.com/commerce/message/v1",
        account="https://api.ebay.com/sell/account/v1",
        # Finances uses the apiz host; some marketplaces also require digital
        # request signing and are gated via marketplace.finances.
        finances="https://apiz.ebay.com/sell/finances/v1",
    )


def build_consent_url(
    environment: str,
    client_id: str,
    runame: str,
    state: str,
    scope: str = CORE_SCOPE,
) -> str:
    """Build an eBay OAuth consent URL."""
    endpoints = endpoints_for(environment)
    return f"{endpoints.authorize}?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": runame,
            "response_type": "code",
            "scope": scope,
            "state": state,
        }
    )


def extract_authorization_code(value: str) -> str:
    """Extract an OAuth code from a pasted code or final callback URL."""
    value = value.strip()
    params = extract_oauth_callback_params(value)
    code = params.get("code", [None])[0]
    if code:
        return code
    return value


def extract_oauth_callback_params(value: str) -> dict[str, list[str]]:
    """Extract OAuth query parameters from a pasted callback URL."""
    parsed = urlparse(value.strip())
    if parsed.query:
        query = parse_qs(parsed.query)
        code = query.get("code", [None])[0]
        if code:
            return query
    return {}
