"""One polling and command owner for Local-only, Hybrid and Cloud entries."""

import asyncio
import logging
from datetime import timedelta
from functools import wraps
from time import monotonic

from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from pysmartthings.exceptions import SmartThingsConnectionError

from .local_rpc import LocalRpcError
from .state import SoundbarState, StateSnapshot

LOGGER = logging.getLogger(__name__)
LOCAL_INTERVAL = timedelta(seconds=2)
CLOUD_INTERVAL = timedelta(seconds=15)
FULL_LOCAL_INTERVAL = 60.0


def coordinated_command(expected=None, *, cloud_only=False, read_fields=()):
    """Route existing device commands through the entry's transaction queue."""

    def decorate(method):
        @wraps(method)
        async def wrapped(device, *args, **kwargs):
            coordinator = device.coordinator
            if coordinator is None or coordinator.in_command:
                return await method(device, *args, **kwargs)
            values = expected(device, *args, **kwargs) if expected else {}
            return await coordinator.async_write(
                lambda: method(device, *args, **kwargs),
                values,
                cloud_only=cloud_only,
                read_fields=read_fields,
            )

        return wrapped

    return decorate


class SoundbarCoordinator(DataUpdateCoordinator[StateSnapshot]):
    def __init__(self, hass, entry, device, *, clock=monotonic):
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"Soundbar {device.device_name}",
            update_interval=LOCAL_INTERVAL if device.hybrid_mode else CLOUD_INTERVAL,
            always_update=False,
        )
        self.device = device
        self.state = SoundbarState(
            local=device.hybrid_mode, cloud=not device.local_only, clock=clock
        )
        self._clock = clock
        self._poll_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._command_task = None
        self._next_cloud = 0.0
        self._next_full_local = 0.0
        self._reauth_started = False
        self._batching = False
        self._last_cycle_finished = float("-inf")
        self._force_refresh = False
        device.coordinator = self

    @property
    def in_command(self):
        return self._command_task is asyncio.current_task()

    def publish(self):
        if not self._batching and not self._shutdown_requested:
            snapshot = self.state.snapshot()
            if snapshot != self.data or not self.last_update_success:
                self.async_set_updated_data(snapshot)

    def receive(self, source, values, *, observed_at=None):
        self.state.merge(source, values, observed_at=observed_at)
        self.publish()

    def transport_changed(self, source, available):
        self.state.transport_available[source] = available
        self.publish()

    async def _async_update_data(self):
        requested_at = monotonic()
        async with self._poll_lock:
            if requested_at <= self._last_cycle_finished and not self._force_refresh:
                return self.state.snapshot()
            self._batching = True
            try:
                if self._force_refresh:
                    self._next_cloud = self._next_full_local = 0.0
                    self._force_refresh = False
                now = self._clock()
                if self.device.hybrid_mode:
                    if now >= self._next_full_local:
                        await self.device.update_local_status(min_age=None)
                        if self.device.local_last_error is None:
                            self._next_full_local = self._clock() + FULL_LOCAL_INTERVAL
                    else:
                        await self.device.update_local_input_source(min_age=None)
                if (
                    not self.device.local_only
                    and not self._reauth_started
                    and now >= self._next_cloud
                ):
                    await self._refresh_cloud()
                    self._next_cloud = self._clock() + CLOUD_INTERVAL.total_seconds()
                return self.state.snapshot()
            finally:
                self._batching = False
                self._last_cycle_finished = monotonic()

    async def _refresh_cloud(self):
        try:
            await self.device.update_cloud_status()
        except ConfigEntryAuthFailed:
            self.device.handle_smartthings_availability(False)
            if self.device.local_only:
                raise
            if not self._reauth_started:
                self._reauth_started = True
                self.config_entry.async_start_reauth(self.hass)
        except (
            ConfigEntryNotReady,
            SmartThingsConnectionError,
            HomeAssistantError,
        ) as err:
            self.device.handle_smartthings_availability(False)
            LOGGER.debug("Cloud soundbar refresh failed: %s", err)

    async def async_refresh_all(self):
        """Force both configured tiers for an explicit diagnostic snapshot."""
        self._force_refresh = True
        await self.async_refresh()

    async def async_write(self, action, expected, *, cloud_only=False, read_fields=()):
        async with self._write_lock, self._poll_lock:
            self._command_task = asyncio.current_task()
            self._batching = True
            try:
                result = await action()
                self.state.expect(
                    expected,
                    write_only=cloud_only
                    and bool(expected)
                    and set(expected)
                    <= {"night_mode", "bass_mode", "voice_amplifier", "virtual_sound"},
                )
                if (
                    self.device.hybrid_mode
                    and not cloud_only
                    and self.device.local_last_error is None
                ):
                    await self._read_after_write(set(expected) | set(read_fields))
                elif not self.device.local_only:
                    await self._refresh_cloud()
                return result
            finally:
                self._command_task = None
                self._batching = False
                self.publish()

    async def _read_after_write(self, fields):
        if "input_source" in fields:
            fields |= {"power", "sound_from_detail_name"}
        try:
            await self.device.read_local_fields(fields)
        except LocalRpcError as err:
            self.transport_changed("local", False)
            LOGGER.debug("Local post-write readback failed: %s", err)

    def diagnostics(self):
        return {
            "fields": self.state.diagnostics(),
            "transports": dict(self.state.transport_available),
        }
