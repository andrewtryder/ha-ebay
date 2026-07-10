"""Shared pytest fixtures."""

from __future__ import annotations

import asyncio
from collections.abc import Generator

import pytest


@pytest.fixture(autouse=True)
def enable_event_loop_debug() -> Generator[None]:
    """Provide a current event loop for Home Assistant's pytest plugin."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.set_debug(True)
    try:
        yield
    finally:
        asyncio.set_event_loop(None)
        loop.close()
