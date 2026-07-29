"""DataUpdateCoordinator for eBay telemetry."""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    API_WARNINGS_STATE_ATTR_MAX,
    EbayApiClient,
    EbayAuthError,
    EbayError,
    enabled_sections_for_options,
)
from .const import (
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
    CONF_PER_ITEM_CAP,
    CONF_PINNED_ITEM_IDS,
    CONF_POLL_INTERVAL,
    CONF_SELLER_STANDARDS_ENABLED,
    CONF_SELLING_ENABLED,
    DOMAIN,
    ENTITY_MODE_DETAILED,
    ENTITY_MODE_MINIMAL,
    MANUAL_REFRESH_COOLDOWN_SECONDS,
    RECENT_EVENTS_MAX,
    REFRESH_RESULT_AUTH_ERROR,
    REFRESH_RESULT_ERROR,
    REFRESH_RESULT_PARTIAL,
    REFRESH_RESULT_SUCCESS,
    REFRESH_RESULT_UNKNOWN,
    section_interval_minutes,
    section_retry_minutes,
)
from .events import (
    EbayEventMixin,
    _is_active_ending_soon,
    _with_current_seconds_left,
)
from .options_parse import pinned_ids
from .sections import MISSING_SCOPE_PARTIAL_FAILURES, PARTIAL_FAILURE_SECTION

_LOGGER = logging.getLogger(__name__)

EventCallback = Callable[[str, dict[str, Any]], None]
ItemEntityCleanupCallback = Callable[..., Awaitable[int]]
EndingSoonKey = tuple[str, str, str, int, str]
SelectedItemKey = tuple[str, str]

COLLECTION_BY_EVENT_KIND = {
    "watching": "watched",
    "bidding": "bidding",
}

_BASELINE_COLLECTIONS = ("watched", "bidding", "selling")
_BASELINE_ID_LISTS = ("sold_item_ids", "unsold_item_ids")


def _duration_ms(start: float) -> float:
    """Return elapsed monotonic time in milliseconds."""
    return (time.monotonic() - start) * 1000


def _parse_section_timestamp(value: Any) -> datetime | None:
    """Parse a section timestamp from a datetime or ISO string."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _section_timestamps(data: dict[str, Any], *keys: str) -> dict[str, datetime]:
    """Merge section timestamp maps; earlier keys take precedence."""
    result: dict[str, datetime] = {}
    for key in keys:
        raw = data.get(key) or {}
        if not isinstance(raw, dict):
            continue
        for section, value in raw.items():
            if section in result:
                continue
            parsed = _parse_section_timestamp(value)
            if parsed is not None:
                result[section] = parsed
    return result


def _section_has_missing_scope(partial_failures: set[str], section: str) -> bool:
    """Return whether the section's last failure was a missing-scope condition."""
    for category in MISSING_SCOPE_PARTIAL_FAILURES:
        if category not in partial_failures:
            continue
        if PARTIAL_FAILURE_SECTION.get(category) == section:
            return True
    return False


class EbayDataUpdateCoordinator(EbayEventMixin, DataUpdateCoordinator[dict[str, Any]]):
    """Coordinate eBay polling, cached diffs, and ending-soon timers."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: EbayApiClient,
        options: dict[str, Any],
    ) -> None:
        self.entry = entry
        self.api = api
        self.options = options
        self.previous_payload: dict[str, Any] | None = None
        self.last_error_category: str | None = None
        self.last_attempt_at: datetime | None = None
        self.last_refresh_duration_seconds: float | None = None
        self.last_refresh_result: str = REFRESH_RESULT_UNKNOWN
        self.selected_item_keys: set[SelectedItemKey] = set()
        # unique_id -> UTC time the sensor left the capped selection.
        self.inactive_item_sensors: dict[str, datetime] = {}
        self._item_entity_cleanup: ItemEntityCleanupCallback | None = None
        self._event_callbacks: dict[str, EventCallback] = {}
        self._timer_listeners: list[Callable[[], None]] = []
        self._scheduled: dict[EndingSoonKey, CALLBACK_TYPE] = {}
        self._fired_ending_soon: set[EndingSoonKey] = set()
        self._fired_ending_soon_dirty = False
        self._ending_soon_fired_save_task: Any | None = None
        # Items currently at/below their effective drop threshold (no repeat events).
        self._price_drop_below_active: set[str] = set()
        self._recent_events: dict[str, deque[dict[str, Any]]] = {
            "watching": deque(maxlen=RECENT_EVENTS_MAX),
            "bidding": deque(maxlen=RECENT_EVENTS_MAX),
            "selling": deque(maxlen=RECENT_EVENTS_MAX),
            "seller_ops": deque(maxlen=RECENT_EVENTS_MAX),
        }
        # Ending-soon keys already recorded to history without an entity callback.
        self._recent_history_ending_soon: set[EndingSoonKey] = set()
        self._manual_refresh_last_requested: float | None = None
        self._force_full_refresh = False
        self._force_sections: set[str] | None = None
        self._repair_streaks: dict[str, int] = {}
        self._repair_streaks_dirty = False
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(minutes=int(options[CONF_POLL_INTERVAL])),
        )

    def recent_events(self, kind: str) -> list[dict[str, Any]]:
        """Return newest-first recent activity for a kind."""
        history = self._recent_events.get(kind)
        if not history:
            return []
        return list(reversed(history))

    @property
    def scheduled_ending_soon_count(self) -> int:
        """Return the number of scheduled ending-soon callbacks."""
        return len(self._scheduled)

    @property
    def ending_soon_threshold_seconds(self) -> int:
        """Return configured ending-soon threshold in seconds."""
        return int(self.options[CONF_ENDING_SOON_THRESHOLD]) * 60

    def register_event_callback(
        self, kind: str, callback_func: EventCallback
    ) -> CALLBACK_TYPE:
        """Register an event entity callback."""
        self._event_callbacks[kind] = callback_func
        if self.data:
            self._rebuild_ending_soon_timers(self.data)

        @callback
        def unsubscribe() -> None:
            if self._event_callbacks.get(kind) is callback_func:
                self._event_callbacks.pop(kind, None)

        return unsubscribe

    def async_add_timer_listener(
        self, update_callback: Callable[[], None]
    ) -> CALLBACK_TYPE:
        """Register a listener for scheduled ending-soon timer collection changes."""
        self._timer_listeners.append(update_callback)

        @callback
        def unsubscribe() -> None:
            try:
                self._timer_listeners.remove(update_callback)
            except ValueError:
                pass

        return unsubscribe

    @callback
    def _async_notify_timer_listeners(self) -> None:
        """Notify timer-count consumers without refreshing every coordinator entity."""
        for listener in list(self._timer_listeners):
            listener()

    async def async_request_manual_refresh(
        self, sections: set[str] | frozenset[str] | None = None
    ) -> bool:
        """Request a coordinator refresh subject to the manual cooldown.

        When ``sections`` is None, every enabled section is forced. Otherwise only
        the intersection of ``sections`` and currently enabled sections is forced.

        Returns True when a refresh was requested, False when ignored.
        """
        now = time.monotonic()
        if self._manual_refresh_last_requested is not None:
            elapsed = now - self._manual_refresh_last_requested
            if elapsed < MANUAL_REFRESH_COOLDOWN_SECONDS:
                _LOGGER.debug(
                    "Ignoring eBay manual refresh during cooldown entry_id=%s remaining_s=%.1f",
                    self.entry.entry_id,
                    MANUAL_REFRESH_COOLDOWN_SECONDS - elapsed,
                )
                return False
        refresh_task = getattr(self, "_refresh_task", None)
        if refresh_task is not None and not refresh_task.done():
            _LOGGER.debug(
                "Ignoring eBay manual refresh while refresh already running entry_id=%s",
                self.entry.entry_id,
            )
            return False
        self._manual_refresh_last_requested = now
        if sections is None:
            self._force_full_refresh = True
            self._force_sections = None
        else:
            self._force_full_refresh = False
            enabled = self._enabled_sections()
            self._force_sections = {
                section for section in sections if section in enabled
            }
        _LOGGER.debug(
            "Requesting eBay manual refresh entry_id=%s force_full=%s force_sections=%s",
            self.entry.entry_id,
            self._force_full_refresh,
            sorted(self._force_sections) if self._force_sections is not None else None,
        )
        await self.async_request_refresh()
        return True

    def _enabled_sections(self) -> set[str]:
        """Return sections enabled by the current options."""
        return enabled_sections_for_options(self.options)

    def _due_sections(self, *, force: bool) -> set[str]:
        """Return enabled sections that are due for refresh."""
        enabled = self._enabled_sections()
        if force or not self.data:
            return enabled
        now = datetime.now(timezone.utc)
        poll_interval = int(self.options[CONF_POLL_INTERVAL])
        last_success = _section_timestamps(
            self.data, "section_last_success", "section_last_fetched"
        )
        last_attempt = _section_timestamps(self.data, "section_last_attempt")
        partial_failures = set(self.data.get("partial_failures") or [])
        due: set[str] = set()
        for section in enabled:
            success = last_success.get(section)
            attempt = last_attempt.get(section)
            if success is None and attempt is None:
                due.add(section)
                continue

            interval = timedelta(
                minutes=section_interval_minutes(section, poll_interval)
            )
            if success is not None and now - success >= interval:
                due.add(section)
                continue

            # Soft-fail retry: last attempt is newer than last success (or no success).
            if attempt is None:
                continue
            if success is not None and attempt <= success:
                continue
            if _section_has_missing_scope(partial_failures, section):
                continue
            retry = timedelta(minutes=section_retry_minutes(section, poll_interval))
            if now - attempt >= retry:
                due.add(section)
        return due

    def _record_refresh_attempt(
        self,
        *,
        start: float,
        result: str,
        error_category: str | None = None,
    ) -> None:
        """Store safe refresh health metadata for diagnostic entities."""
        self.last_attempt_at = datetime.now(timezone.utc)
        self.last_refresh_duration_seconds = max(0.0, time.monotonic() - start)
        self.last_refresh_result = result
        self.last_error_category = error_category

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data and detect events."""
        start = time.monotonic()
        buying_enabled = bool(self.options[CONF_BUYING_ENABLED])
        selling_enabled = bool(self.options[CONF_SELLING_ENABLED])
        analytics_enabled = bool(self.options[CONF_ANALYTICS_ENABLED])
        fulfillment_enabled = bool(self.options[CONF_FULFILLMENT_ENABLED])
        seller_standards_enabled = bool(self.options[CONF_SELLER_STANDARDS_ENABLED])
        feedback_enabled = bool(self.options[CONF_FEEDBACK_ENABLED])
        messages_enabled = bool(self.options[CONF_MESSAGES_ENABLED])
        account_privileges_enabled = bool(self.options[CONF_ACCOUNT_PRIVILEGES_ENABLED])
        finances_enabled = bool(self.options[CONF_FINANCES_ENABLED])
        force = self._force_full_refresh
        force_sections = self._force_sections
        self._force_full_refresh = False
        self._force_sections = None
        if force:
            due_sections = self._due_sections(force=True)
        elif force_sections is not None:
            due_sections = set(force_sections)
        else:
            due_sections = self._due_sections(force=False)
        _LOGGER.debug(
            "eBay coordinator refresh started entry_id=%s force=%s force_sections=%s due_sections=%s buying_enabled=%s selling_enabled=%s analytics_enabled=%s fulfillment_enabled=%s seller_standards_enabled=%s feedback_enabled=%s messages_enabled=%s account_privileges_enabled=%s finances_enabled=%s",
            self.entry.entry_id,
            force,
            sorted(force_sections) if force_sections is not None else None,
            sorted(due_sections),
            buying_enabled,
            selling_enabled,
            analytics_enabled,
            fulfillment_enabled,
            seller_standards_enabled,
            feedback_enabled,
            messages_enabled,
            account_privileges_enabled,
            finances_enabled,
        )
        if not due_sections and self.data is not None:
            self._record_refresh_attempt(
                start=start,
                result=self.last_refresh_result
                if self.last_refresh_result != REFRESH_RESULT_UNKNOWN
                else REFRESH_RESULT_SUCCESS,
                error_category=None,
            )
            # No section attempts this tick — do not advance repair streaks.
            return self.data
        try:
            payload = await self.api.async_fetch_data(
                buying_enabled=buying_enabled,
                selling_enabled=selling_enabled,
                analytics_enabled=analytics_enabled,
                fulfillment_enabled=fulfillment_enabled,
                seller_standards_enabled=seller_standards_enabled,
                feedback_enabled=feedback_enabled,
                messages_enabled=messages_enabled,
                account_privileges_enabled=account_privileges_enabled,
                finances_enabled=finances_enabled,
                ending_soon_threshold_seconds=self.ending_soon_threshold_seconds,
                granted_scopes=self.entry.data.get(CONF_OAUTH_SCOPES),
                sections=due_sections,
                previous=self.data,
            )
        except EbayAuthError as exc:
            self._record_refresh_attempt(
                start=start,
                result=REFRESH_RESULT_AUTH_ERROR,
                error_category="auth",
            )
            _LOGGER.debug(
                "eBay coordinator refresh auth failed entry_id=%s duration_ms=%.1f",
                self.entry.entry_id,
                _duration_ms(start),
            )
            from .repairs import async_sync_reauth_issue

            await async_sync_reauth_issue(self.hass, self.entry, active=True)
            raise ConfigEntryAuthFailed("eBay authentication failed") from exc
        except EbayError as exc:
            self._record_refresh_attempt(
                start=start,
                result=REFRESH_RESULT_ERROR,
                error_category=type(exc).__name__,
            )
            _LOGGER.debug(
                "eBay coordinator refresh failed entry_id=%s error=%s duration_ms=%.1f",
                self.entry.entry_id,
                type(exc).__name__,
                _duration_ms(start),
            )
            raise UpdateFailed(f"eBay update failed: {type(exc).__name__}") from exc

        payload["summary"]["last_update"] = payload["last_update"].isoformat()
        payload["summary"]["last_successful_update"] = payload[
            "last_successful_update"
        ].isoformat()
        payload["summary"]["partial_failures"] = payload["partial_failures"]
        payload["summary"]["truncated_collections"] = payload.get(
            "truncated_collections", {}
        )
        payload["summary"]["missing_scopes"] = payload.get("missing_scopes", {})
        payload["summary"]["missing_scope_features"] = payload.get(
            "missing_scope_features", []
        )
        for key in (
            "section_last_fetched",
            "section_last_attempt",
            "section_last_success",
        ):
            payload["summary"][key] = {
                k: v.isoformat() if isinstance(v, datetime) else v
                for k, v in (payload.get(key) or {}).items()
            }
        warnings = payload.get("api_warnings") or []
        payload["summary"]["api_warnings"] = warnings[:API_WARNINGS_STATE_ATTR_MAX]
        result = (
            REFRESH_RESULT_PARTIAL
            if payload.get("partial_failures")
            else REFRESH_RESULT_SUCCESS
        )
        self._record_refresh_attempt(start=start, result=result, error_category=None)

        self._detect_transition_events(self.previous_payload, payload)
        self._rebuild_ending_soon_timers(payload)
        self.previous_payload = _baseline_payload(self.previous_payload, payload)
        self.refresh_selected_item_keys(payload)
        await self._async_flush_ending_soon_fired()

        from .repairs import async_sync_repair_issues

        refreshed = set(payload.get("refreshed_sections") or due_sections)
        await async_sync_repair_issues(
            self.hass,
            self.entry,
            self,
            payload,
            refreshed_sections=refreshed,
        )
        _LOGGER.debug(
            "eBay coordinator refresh completed entry_id=%s refreshed_sections=%s watched_count=%s bidding_count=%s selling_count=%s partial_failures=%s truncated_collections=%s duration_ms=%.1f",
            self.entry.entry_id,
            payload.get("refreshed_sections", []),
            len(payload["watched"]),
            len(payload["bidding"]),
            len(payload["selling"]),
            payload["partial_failures"],
            payload.get("truncated_collections", {}),
            _duration_ms(start),
        )
        return payload

    def refresh_selected_item_keys(
        self, data: dict[str, Any] | None = None
    ) -> set[SelectedItemKey]:
        """Compute and cache the current per-item selection."""
        payload = data if data is not None else self.data
        mode = self.options[CONF_ENTITY_MODE]
        if mode == ENTITY_MODE_MINIMAL or not payload:
            self.selected_item_keys = set()
            _LOGGER.debug(
                "Selected eBay item entities refreshed entry_id=%s mode=%s selected_count=0",
                self.entry.entry_id,
                mode,
            )
            return self.selected_item_keys
        selected = _select_item_entities(
            payload,
            mode,
            int(self.options[CONF_PER_ITEM_CAP]),
            pinned_ids(self.options.get(CONF_PINNED_ITEM_IDS, "")),
            self.ending_soon_threshold_seconds,
        )
        self.selected_item_keys = set(selected)
        _LOGGER.debug(
            "Selected eBay item entities refreshed entry_id=%s mode=%s selected_count=%s cap=%s",
            self.entry.entry_id,
            mode,
            len(self.selected_item_keys),
            int(self.options[CONF_PER_ITEM_CAP]),
        )
        return self.selected_item_keys

    def register_item_entity_cleanup(
        self, cleanup: ItemEntityCleanupCallback | None
    ) -> None:
        """Register the sensor-platform callback that removes inactive item entities."""
        self._item_entity_cleanup = cleanup

    async def async_cleanup_inactive_item_entities(self, *, force: bool = True) -> int:
        """Remove inactive item sensors (all when force, else past grace only)."""
        if self._item_entity_cleanup is None:
            return 0
        return await self._item_entity_cleanup(force=force)

    def _mark_ending_soon_fired(self, key: EndingSoonKey) -> None:
        """Record a fired ending-soon key and mark persistence dirty."""
        if key in self._fired_ending_soon:
            return
        self._fired_ending_soon.add(key)
        self._mark_fired_ending_soon_dirty()

    def _mark_fired_ending_soon_dirty(self) -> None:
        """Mark fired ending-soon keys dirty and schedule a store flush."""
        self._fired_ending_soon_dirty = True
        hass = getattr(self, "hass", None)
        if hass is None:
            return
        task = getattr(self, "_ending_soon_fired_save_task", None)
        if task is not None and not task.done():
            return
        self._ending_soon_fired_save_task = hass.async_create_task(
            self._async_flush_ending_soon_fired()
        )

    async def _async_flush_ending_soon_fired(self) -> None:
        """Persist fired ending-soon keys when the in-memory set changed."""
        if not getattr(self, "_fired_ending_soon_dirty", False):
            return
        from .ending_soon_store import async_save_ending_soon_fired

        await async_save_ending_soon_fired(
            self.hass, self.entry.entry_id, self._fired_ending_soon
        )
        self._fired_ending_soon_dirty = False

    def _prune_fired_ending_soon(self, now: datetime) -> None:
        """Drop fired keys after listing end_time plus retention window."""
        from .ending_soon_store import prune_ending_soon_fired_keys

        pruned = prune_ending_soon_fired_keys(self._fired_ending_soon, now)
        if pruned != self._fired_ending_soon:
            self._fired_ending_soon = pruned
            self._mark_fired_ending_soon_dirty()

    def _rebuild_ending_soon_timers(self, payload: dict[str, Any]) -> None:
        wanted: set[EndingSoonKey] = set()
        previous_keys = set(self._scheduled)
        threshold = self.ending_soon_threshold_seconds
        now = datetime.now(timezone.utc)
        self._prune_fired_ending_soon(now)
        for kind, collection, event_type in (
            ("watching", payload["watched"], "watched_item_ending_soon"),
            ("bidding", payload["bidding"], "bidding_item_ending_soon"),
        ):
            for item_id, item in collection.items():
                end_time = item.get("end_time")
                if not isinstance(end_time, datetime):
                    continue
                if item.get("seconds_left") == 0 or end_time <= now:
                    continue
                normalized_end_time = _normalized_end_time(end_time)
                key = (
                    self.entry.entry_id,
                    kind,
                    item_id,
                    threshold,
                    normalized_end_time,
                )
                wanted.add(key)
                fire_at = end_time - timedelta(seconds=threshold)
                if fire_at <= now:
                    if _is_active_ending_soon(item, threshold, now):
                        if key not in self._fired_ending_soon:
                            _LOGGER.debug(
                                "Firing eBay ending-soon timer immediately kind=%s item_id=%s threshold=%s end_time=%s",
                                kind,
                                item_id,
                                threshold,
                                normalized_end_time,
                            )
                            if self._emit(
                                kind,
                                event_type,
                                _with_current_seconds_left(item, now),
                                history_key=key,
                            ):
                                self._mark_ending_soon_fired(key)
                        else:
                            _LOGGER.debug(
                                "Skipping already-fired eBay ending-soon timer kind=%s item_id=%s threshold=%s end_time=%s",
                                kind,
                                item_id,
                                threshold,
                                normalized_end_time,
                            )
                        continue
                    fire_at = now
                if key in self._scheduled:
                    continue
                if any(
                    scheduled_key[:4] == key[:4] and scheduled_key != key
                    for scheduled_key in self._scheduled
                ):
                    _LOGGER.debug(
                        "Rescheduling eBay ending-soon timer kind=%s item_id=%s threshold=%s end_time=%s",
                        kind,
                        item_id,
                        threshold,
                        normalized_end_time,
                    )
                else:
                    _LOGGER.debug(
                        "Scheduling eBay ending-soon timer kind=%s item_id=%s threshold=%s end_time=%s fire_at=%s",
                        kind,
                        item_id,
                        threshold,
                        normalized_end_time,
                        fire_at.isoformat(),
                    )
                self._scheduled[key] = async_track_point_in_utc_time(
                    self.hass,
                    self._scheduled_callback(
                        kind,
                        event_type,
                        item_id,
                        normalized_end_time,
                        key,
                    ),
                    fire_at,
                )

        for key in list(self._scheduled):
            if key not in wanted:
                _, kind, item_id, threshold, normalized_end_time = key
                _LOGGER.debug(
                    "Cancelling eBay ending-soon timer kind=%s item_id=%s threshold=%s end_time=%s",
                    kind,
                    item_id,
                    threshold,
                    normalized_end_time,
                )
                self._scheduled.pop(key)()
        self._recent_history_ending_soon.intersection_update(wanted)
        if set(self._scheduled) != previous_keys:
            self._async_notify_timer_listeners()

    @callback
    def _scheduled_callback(
        self,
        kind: str,
        event_type: str,
        item_id: str,
        scheduled_end_time: str,
        key: EndingSoonKey,
    ) -> Callable[[datetime], None]:
        @callback
        def fire(now: datetime) -> None:
            self._scheduled.pop(key, None)
            self._async_notify_timer_listeners()
            _LOGGER.debug(
                "Firing scheduled eBay ending-soon timer kind=%s item_id=%s end_time=%s",
                kind,
                item_id,
                scheduled_end_time,
            )
            if key in self._fired_ending_soon:
                _LOGGER.debug(
                    "Skipping already-fired scheduled eBay ending-soon timer kind=%s item_id=%s end_time=%s",
                    kind,
                    item_id,
                    scheduled_end_time,
                )
                return
            item = (
                (self.data or {}).get(COLLECTION_BY_EVENT_KIND[kind], {}).get(item_id)
            )
            end_time = item.get("end_time") if item else None
            if (
                item is None
                or not isinstance(end_time, datetime)
                or _normalized_end_time(end_time) != scheduled_end_time
            ):
                _LOGGER.debug(
                    "Skipping stale eBay ending-soon timer kind=%s item_id=%s end_time=%s",
                    kind,
                    item_id,
                    scheduled_end_time,
                )
                return
            if not _is_active_ending_soon(
                item, self.ending_soon_threshold_seconds, now
            ):
                _LOGGER.debug(
                    "Skipping inactive eBay ending-soon timer kind=%s item_id=%s end_time=%s",
                    kind,
                    item_id,
                    scheduled_end_time,
                )
                return
            if self._emit(
                kind,
                event_type,
                _with_current_seconds_left(item, now),
                history_key=key,
            ):
                self._mark_ending_soon_fired(key)

        return fire

    def cancel_scheduled_events(self) -> None:
        """Cancel scheduled ending-soon callbacks."""
        if not self._scheduled:
            return
        for key, cancel in list(self._scheduled.items()):
            _, kind, item_id, threshold, normalized_end_time = key
            _LOGGER.debug(
                "Cancelling eBay ending-soon timer kind=%s item_id=%s threshold=%s end_time=%s",
                kind,
                item_id,
                threshold,
                normalized_end_time,
            )
            cancel()
        self._scheduled.clear()
        self._async_notify_timer_listeners()


def _baseline_payload(
    previous: dict[str, Any] | None, current: dict[str, Any]
) -> dict[str, Any]:
    """Return the next event-comparison baseline, preserving truncated collections.

    Truncated responses omit items outside the page budget. Keep prior items that
    were missing from the truncated collection, but update overlapping items with
    the current values so change events are not repeated on the next poll.
    """
    baseline = dict(current)
    if previous is None:
        return baseline
    truncated = current.get("truncated_collections") or {}
    for key in _BASELINE_COLLECTIONS:
        if truncated.get(key):
            merged = dict(previous.get(key, {}))
            merged.update(current.get(key, {}))
            baseline[key] = merged
    if truncated.get("sold_unsold"):
        for key in _BASELINE_ID_LISTS:
            previous_ids = set(previous.get(key) or [])
            current_ids = set(current.get(key) or [])
            baseline[key] = sorted(previous_ids | current_ids)
        previous_classification = dict(previous.get("selling_classification") or {})
        previous_classification.update(current.get("selling_classification") or {})
        baseline["selling_classification"] = previous_classification
    return baseline


def _pinned_ids(value: str) -> set[str]:
    """Compatibility wrapper for pinned ID parsing."""
    return pinned_ids(value)


def _select_item_entities(
    data: dict[str, Any],
    mode: str,
    cap: int,
    pinned: set[str],
    ending_soon_threshold: int,
) -> list[SelectedItemKey]:
    candidates: list[tuple[int, str, str]] = []
    for kind in ("selling", "watched", "bidding"):
        for item_id, item in data.get(kind, {}).items():
            priority = 100
            if item_id in pinned:
                priority = 0
            elif kind == "selling" and (
                (item.get("bid_count") or 0) > 0
                or (item.get("offers") or 0) > 0
                or (item.get("questions") or 0) > 0
            ):
                priority = 10
            elif (
                item.get("seconds_left") is not None
                and 0 < item["seconds_left"] <= ending_soon_threshold
            ):
                priority = 20
            elif mode != ENTITY_MODE_DETAILED:
                continue
            candidates.append((priority, kind, item_id))
    candidates.sort(key=lambda value: (value[0], value[1], value[2]))
    return [(kind, item_id) for _, kind, item_id in candidates[:cap]]


def _normalized_end_time(end_time: datetime) -> str:
    """Return a stable UTC identity for an eBay end time."""
    return end_time.astimezone(timezone.utc).isoformat()
