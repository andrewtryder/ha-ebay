"""Commerce Messages client mixin."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..seller_ops import (
    CONVERSATIONS_MAX_PAGES,
    CONVERSATIONS_PAGE_LIMIT,
    normalize_conversation,
    summarize_messages,
)


class MessagesClientMixin:
    """Member conversation fetchers."""

    _endpoints: Any

    async def async_fetch_messages(self) -> tuple[dict[str, Any], bool]:
        """Fetch member conversation metadata aggregates."""
        merged: dict[str, dict[str, Any]] = {}
        truncated = False
        now = datetime.now(timezone.utc)
        offset = 0
        for page in range(CONVERSATIONS_MAX_PAGES):
            payload = await self._async_rest_get(
                f"{self._endpoints.messages}/conversation",
                params={
                    "conversation_type": "FROM_MEMBERS",
                    "limit": str(CONVERSATIONS_PAGE_LIMIT),
                    "offset": str(offset),
                },
                partial_category="messages",
            )
            conversations = payload.get("conversations") or []
            for raw in conversations:
                normalized = normalize_conversation(raw)
                if normalized.get("conversation_id"):
                    merged[normalized["conversation_id"]] = normalized
            offset += CONVERSATIONS_PAGE_LIMIT
            total = payload.get("total")
            if not conversations:
                break
            if total is not None:
                try:
                    if offset >= int(total):
                        break
                except (TypeError, ValueError):
                    pass
            elif len(conversations) < CONVERSATIONS_PAGE_LIMIT:
                break
            if page == CONVERSATIONS_MAX_PAGES - 1:
                truncated = True
        return summarize_messages(list(merged.values()), now=now), truncated
