"""Sell Account privileges client mixin."""

from __future__ import annotations

from typing import Any

from ..seller_ops import empty_seller_ops, parse_account_privileges


class AccountClientMixin:
    """Account privileges fetcher."""

    _endpoints: Any

    async def async_fetch_account_privileges(self) -> dict[str, Any]:
        """Fetch Sell Account privileges (selling limits / registration)."""
        privileges = empty_seller_ops()["account_privileges"]
        payload = await self._async_rest_get(
            f"{self._endpoints.account}/privilege",
            partial_category="account_privileges",
            allow_404=True,
        )
        privileges.update(parse_account_privileges(payload))
        return privileges
