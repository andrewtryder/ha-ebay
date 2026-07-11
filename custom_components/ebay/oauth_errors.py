"""OAuth token request exception compatibility helpers."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from aiohttp import RequestInfo
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from .const import DOMAIN

try:
    from homeassistant.helpers.config_entry_oauth2_flow import (
        OAuth2TokenRequestError,
        OAuth2TokenRequestReauthError,
        OAuth2TokenRequestTransientError,
    )
except ImportError:

    class OAuth2TokenRequestError(Exception):
        """OAuth token request failed."""

        def __init__(
            self, *args: Any, message: str | None = None, **kwargs: Any
        ) -> None:
            super().__init__(
                message or (args[0] if args else "OAuth token request failed")
            )

    class OAuth2TokenRequestTransientError(OAuth2TokenRequestError):
        """OAuth token request failed due to a transient condition."""

    class OAuth2TokenRequestReauthError(OAuth2TokenRequestError):
        """OAuth token request failed because reauthorization is required."""


_DEFAULT_TOKEN_URL = URL("https://api.ebay.com/identity/v1/oauth2/token")


def _request_info(
    request_info: RequestInfo | None = None,
    *,
    token_url: str | None = None,
) -> RequestInfo:
    """Return a request_info suitable for HA OAuth token exceptions."""
    if request_info is not None:
        return request_info
    url = URL(token_url) if token_url else _DEFAULT_TOKEN_URL
    return RequestInfo(
        url=url,
        method="POST",
        headers=CIMultiDictProxy(CIMultiDict()),
        real_url=url,
    )


def oauth_token_error(
    status: int,
    message: str | None = None,
    *,
    error_code: str | None = None,
    request_info: RequestInfo | None = None,
    history: tuple[Any, ...] = (),
    headers: Any | None = None,
    token_url: str | None = None,
) -> OAuth2TokenRequestError:
    """Build the HA OAuth exception matching an eBay token failure."""
    info = _request_info(request_info, token_url=token_url)
    if (
        status == HTTPStatus.TOO_MANY_REQUESTS
        or status >= HTTPStatus.INTERNAL_SERVER_ERROR
    ):
        return OAuth2TokenRequestTransientError(
            request_info=info,
            history=history,
            status=status,
            message=message
            or f"eBay OAuth token request failed temporarily with HTTP {status}",
            headers=headers,
            domain=DOMAIN,
        )
    if status < HTTPStatus.INTERNAL_SERVER_ERROR and (
        error_code
        in {
            "invalid_grant",
            "invalid_client",
            "invalid_request",
            "unauthorized_client",
            "access_denied",
        }
        or status
        in {
            HTTPStatus.BAD_REQUEST,
            HTTPStatus.UNAUTHORIZED,
            HTTPStatus.FORBIDDEN,
        }
    ):
        return OAuth2TokenRequestReauthError(
            request_info=info,
            history=history,
            status=status,
            message=message
            or f"eBay OAuth token request requires reauthorization; HTTP {status}",
            headers=headers,
            domain=DOMAIN,
        )
    return OAuth2TokenRequestError(
        request_info=info,
        history=history,
        status=status,
        message=message or f"eBay OAuth token request failed with HTTP {status}",
        headers=headers,
        domain=DOMAIN,
    )


def oauth_token_transient_error(
    message: str,
    *,
    status: int = HTTPStatus.SERVICE_UNAVAILABLE,
    request_info: RequestInfo | None = None,
    token_url: str | None = None,
) -> OAuth2TokenRequestTransientError:
    """Build a transient OAuth token failure."""
    return OAuth2TokenRequestTransientError(
        request_info=_request_info(request_info, token_url=token_url),
        status=status,
        message=message,
        domain=DOMAIN,
    )


def oauth_token_request_error(
    message: str,
    *,
    status: int = 0,
    request_info: RequestInfo | None = None,
    token_url: str | None = None,
) -> OAuth2TokenRequestError:
    """Build a generic OAuth token failure."""
    return OAuth2TokenRequestError(
        request_info=_request_info(request_info, token_url=token_url),
        status=status,
        message=message,
        domain=DOMAIN,
    )
