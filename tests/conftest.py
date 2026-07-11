"""Shared pytest fixtures."""

from __future__ import annotations

import asyncio
from collections.abc import Generator

import pytest


@pytest.fixture(autouse=True)
def enable_event_loop_debug() -> Generator[None]:
    """Ensure a current event loop exists for HA harness cleanup and sync tests.

    Overrides the Home Assistant plugin fixture of the same name so unit tests
    that call ``asyncio.run`` and async ``hass`` fixture tests can coexist.
    """
    created = False
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        created = True
    loop.set_debug(True)
    try:
        yield
    finally:
        if created and not loop.is_closed():
            asyncio.set_event_loop(None)
            loop.close()
