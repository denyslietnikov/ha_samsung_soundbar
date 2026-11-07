"""One polling and command owner for RPC and optional local OCF."""

import asyncio
import logging
from datetime import timedelta
from functools import wraps
from time import monotonic

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .local_ocf import OcfError
from .local_rpc import LocalRpcError
from .ocf_credentials import async_complete
from .ocf_state import OCF_FIELDS, POLL_HREFS, RESOURCE_FIELDS, project_ocf
from .state import SoundbarState, StateSnapshot

LOGGER = logging.getLogger(__name__)
LOCAL_INTERVAL = timedelta(seconds=2)
FULL_LOCAL_INTERVAL = 60.0
OCF_INTERVAL = 30.0


def coordinated_command(expected=None, *, read_fields=()):
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
            update_interval=LOCAL_INTERVAL,
            always_update=False,
        )
        self.device = device
        self.state = SoundbarState(local=device.local_rpc_enabled, clock=clock)
        self._clock = clock
        self._poll_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._command_task = None
        self._next_full_local = 0.0
        self._batching = False
        self._last_cycle_finished = float("-inf")
        self._force_refresh = False
        self.ocf = None
        self._ocf_task = None
        self._ocf_detach_task = None
        self._ocf_detaching = False
        self._next_ocf = 0.0
        self._ocf_resources = {}
        self._shutdown_task = None
        self._unsub_stop = None
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
        source = "rpc" if source == "local" else source
        if source not in self.state.transport_available:
            raise ValueError("Unknown local transport")
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
                    self._next_full_local = self._next_ocf = 0.0
                    self._force_refresh = False
                now = self._clock()
                self._schedule_ocf()
                if self.device.local_rpc_enabled:
                    if now >= self._next_full_local:
                        await self.device.update_local_status(min_age=None)
                        if self.device.local_last_error is None:
                            self._next_full_local = self._clock() + FULL_LOCAL_INTERVAL
                    else:
                        await self.device.update_local_input_source(min_age=None)
                return self.state.snapshot()
            finally:
                self._batching = False
                self._last_cycle_finished = monotonic()

    async def async_refresh_all(self):
        """Refresh configured tiers and await the shared OCF diagnostic sweep."""
        self._force_refresh = True
        await self.async_refresh()
        await self._async_wait_ocf_poll()

    async def _async_wait_ocf_poll(self):
        task = self._ocf_task
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.cancelled() or asyncio.current_task().cancelling():
                    raise

    def configure_ocf(self, client):
        """Attach the optional transport to this coordinator, never per entity."""
        if (
            self._ocf_detaching
            or self._shutdown_requested
            or self.ocf is not None
            and client is not self.ocf
        ):
            raise RuntimeError("Await async_detach_ocf() before replacing the client")
        self._ocf_detach_task = None
        self.ocf = client
        if client is not None and self._unsub_stop is None:
            self._unsub_stop = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP, self._async_handle_stop
            )
            self.config_entry.async_on_unload(self._remove_stop_listener)
        self.state.use_ocf = client is not None and self.device.ocf_enabled
        self._next_ocf = 0.0
        self._schedule_ocf()

    async def async_detach_ocf(self):
        """Drain OCF transactions and close once, despite caller cancellation."""
        if self._ocf_detach_task is None:
            self._ocf_detaching = True
            self._ocf_detach_task = self.hass.async_create_task(
                self._async_detach_ocf(), "soundbar_ocf_detach"
            )
        _, cancelled = await async_complete(self._ocf_detach_task)
        if cancelled:
            raise asyncio.CancelledError

    async def _async_detach_ocf(self):
        try:
            # Polling can own the write lock, so cancel and drain it before locking.
            if self._ocf_task is not None:
                if not self._ocf_task.done():
                    self._ocf_task.cancel()
                try:
                    await self._ocf_task
                except asyncio.CancelledError:
                    pass
            async with self._write_lock, self._poll_lock:
                client = self.ocf
                self.ocf = None
                self.state.use_ocf = False
                self.state.transport_available["ocf"] = False
                self.state.invalidate("ocf", OCF_FIELDS)
                self.state.ocf_unavailable_fields.clear()
                self._ocf_resources.clear()
            # No transaction retains the client; closing must not block RPC.
            if client is not None:
                await client.async_close()
        finally:
            self._ocf_detaching = False
            self.publish()

    def _schedule_ocf(self):
        if (
            not self.state.use_ocf
            or self._ocf_detaching
            or self._shutdown_requested
            or self._clock() < self._next_ocf
            or self._ocf_task is not None
            and not self._ocf_task.done()
        ):
            return
        # The owned task avoids making optional DTLS timeouts block the 2s RPC tier.
        self._ocf_task = self.hass.async_create_task(self._poll_ocf())

    async def _poll_ocf(self):
        try:
            for href in POLL_HREFS:
                async with self._write_lock:
                    if self._shutdown_requested or self._ocf_detaching:
                        return
                    kind = await self._read_ocf(href)
                if kind in {"auth", "identity", "transport", "backoff", "closed"}:
                    break
        finally:
            self._next_ocf = self._clock() + OCF_INTERVAL
            self.publish()

    async def _read_ocf(self, href):
        observed_at = self._clock()
        try:
            payload = await self.ocf.async_read(href)
            if not isinstance(payload, dict):
                raise OcfError("malformed")
            projection = project_ocf(href, payload)
        except OcfError as error:
            self._ocf_failed(href, error.kind)
            return error.kind
        if projection.supported is False:
            self.state.invalidate("ocf", RESOURCE_FIELDS[href])
            self.state.ocf_unavailable_fields.update(RESOURCE_FIELDS[href])
        else:
            self.state.ocf_unavailable_fields.difference_update(RESOURCE_FIELDS[href])
        self.state.invalidate("ocf", projection.invalidated)
        self.state.ocf_unavailable_fields.update(projection.invalidated)
        self._ocf_resources[href] = {
            "last_error": None,
            "supported": projection.supported,
        }
        self.state.transport_available["ocf"] = bool(self.ocf.available)
        self.receive("ocf", projection.values, observed_at=observed_at)
        return None

    def _ocf_failed(self, href, kind):
        self._ocf_resources[href] = {"last_error": kind, "supported": None}
        self.state.ocf_unavailable_fields.update(RESOURCE_FIELDS[href])
        self.transport_changed("ocf", self.ocf is not None and bool(self.ocf.available))

    async def async_ocf_snapshot(self, hrefs=POLL_HREFS):
        """Read only the fixed audio projection, never return raw OCF responses."""
        hrefs = tuple(hrefs)
        if len(hrefs) > len(POLL_HREFS) or any(
            href not in POLL_HREFS for href in hrefs
        ):
            raise OcfError("unverified")
        if (
            hrefs
            and self.ocf is not None
            and self.state.use_ocf
            and not self._ocf_detaching
        ):
            # Join the current sweep, then serialize explicitly requested reads.
            if self._ocf_task is not None and not self._ocf_task.done():
                await self._async_wait_ocf_poll()
            async with self._write_lock:
                for href in dict.fromkeys(hrefs):
                    if (
                        self._shutdown_requested
                        or self._ocf_detaching
                        or self.ocf is None
                        or not self.state.use_ocf
                    ):
                        break
                    kind = await self._read_ocf(href)
                    if kind in {"auth", "identity", "transport", "backoff", "closed"}:
                        break
        return self.ocf_diagnostics()

    def ocf_diagnostics(self):
        now = self._clock()
        records = self.state.records["ocf"]
        return {
            "configured": self.ocf is not None,
            "transport": self.ocf.diagnostics() if self.ocf is not None else {},
            "resources": {
                href: {
                    **status,
                    "available": any(
                        self.ocf_field_available(key) for key in RESOURCE_FIELDS[href]
                    ),
                }
                for href, status in self._ocf_resources.items()
            },
            "values": {
                key: record.value
                for key, record in records.items()
                if key in OCF_FIELDS and record.fresh(now)
            },
            "fields": {
                key: value
                for key, value in self.state.diagnostics().items()
                if key in OCF_FIELDS
            },
        }

    def ocf_field_available(self, key):
        if self.ocf is None or not self.state.use_ocf or not self.ocf.available:
            return False
        href = next(
            (href for href, fields in RESOURCE_FIELDS.items() if key in fields), None
        )
        status = self._ocf_resources.get(href, {})
        record = self.state.records["ocf"].get(key)
        return (
            status.get("last_error") is None
            and status.get("supported") is not False
            and record is not None
            and record.fresh(self._clock())
        )

    async def _read_required_ocf_fields(self, href, fields):
        before = {key: self.state.records["ocf"].get(key) for key in fields}
        error = await self._read_ocf(href)
        if error is not None:
            raise OcfError(error)
        if any(
            self.state.records["ocf"].get(key) is None
            or self.state.records["ocf"].get(key) is before[key]
            for key in fields
        ):
            raise OcfError("malformed")

    async def async_write_ocf(
        self,
        href,
        payload,
        *,
        woofer_profile=None,
        switch_profile=None,
        tone_profile=None,
        eq_profile=None,
        guard=None,
        required_fields=(),
        context_fields=(),
        rpc_fields=(),
    ):
        """Internal verified-route command; never an arbitrary public POST action."""
        if self._ocf_detaching or self._shutdown_requested:
            raise OcfError("closed")
        if self.ocf is None or not self.state.use_ocf or href not in RESOURCE_FIELDS:
            raise OcfError("unverified")
        if not set(rpc_fields) <= {"power", "sound_mode"}:
            raise OcfError("unverified")
        if len(context_fields) > len(RESOURCE_FIELDS) or any(
            resource not in RESOURCE_FIELDS
            or not set(fields) <= RESOURCE_FIELDS[resource]
            for resource, fields in context_fields
        ):
            raise OcfError("unverified")
        async with self._write_lock, self._poll_lock:
            if (
                self._shutdown_requested
                or self._ocf_detaching
                or self.ocf is None
                or not self.state.use_ocf
            ):
                raise OcfError("closed")
            try:
                if guard is not None:
                    for resource, fields in context_fields:
                        await self._read_required_ocf_fields(resource, fields)
                    await self._read_required_ocf_fields(href, required_fields)
                    if rpc_fields:
                        before = {
                            key: self.state.records["rpc"].get(key)
                            for key in rpc_fields
                        }
                        try:
                            await self.device.read_local_fields(set(rpc_fields))
                        except LocalRpcError:
                            raise OcfError("transport") from None
                        if any(
                            self.state.records["rpc"].get(key) is None
                            or self.state.records["rpc"].get(key) is before[key]
                            for key in rpc_fields
                        ):
                            raise OcfError("malformed")
                    guard()
                if isinstance(payload, dict):
                    self.state.begin_write(
                        project_ocf(href, payload).values, source="ocf"
                    )
                if eq_profile is not None:
                    result = await self.ocf.async_write(
                        href, payload, eq_profile=eq_profile
                    )
                elif tone_profile is not None:
                    result = await self.ocf.async_write(
                        href, payload, tone_profile=tone_profile
                    )
                elif switch_profile is not None:
                    result = await self.ocf.async_write(
                        href, payload, switch_profile=switch_profile
                    )
                elif woofer_profile is None:
                    result = await self.ocf.async_write(href, payload)
                else:
                    result = await self.ocf.async_write(
                        href, payload, woofer_profile=woofer_profile
                    )
                # No optimistic success and no other-transport replay, even on timeout.
                if required_fields:
                    await self._read_required_ocf_fields(href, required_fields)
                else:
                    await self._read_ocf(href)
                return result
            except asyncio.CancelledError:
                self._ocf_failed(href, "transport")
                raise
            except OcfError as error:
                self._ocf_failed(href, error.kind)
                raise
            finally:
                self.publish()

    async def async_shutdown(self):
        """Share one cancellation-safe cleanup across stop and entry unload."""
        if self._shutdown_task is None:
            self._shutdown_requested = True
            self._shutdown_task = self.hass.async_create_task(
                self._async_shutdown(), "soundbar_shutdown"
            )
        _, cancelled = await async_complete(self._shutdown_task)
        if cancelled:
            raise asyncio.CancelledError

    async def _async_handle_stop(self, _event: Event) -> None:
        # The one-shot bus listener has already removed itself.
        self._unsub_stop = None
        await self.async_shutdown()

    def _remove_stop_listener(self):
        if self._unsub_stop is not None:
            self._unsub_stop()
            self._unsub_stop = None

    async def _async_shutdown(self):
        await super().async_shutdown()
        self._remove_stop_listener()
        await self.async_detach_ocf()

    async def async_write(self, action, expected, *, read_fields=()):
        fields = set(expected) | set(read_fields)
        if not self.device.local_rpc_enabled or fields & OCF_FIELDS:
            raise LocalRpcError("RPC command requires locally owned fields")
        if "input_source" in fields:
            fields |= {"power", "sound_from_detail_name"}
        async with self._write_lock, self._poll_lock:
            if self._shutdown_requested:
                raise LocalRpcError("RPC coordinator is closed")
            self._command_task = asyncio.current_task()
            self._batching = True
            try:
                self.state.begin_write(fields, source="rpc")
                result = await action()
                self.state.expect(expected, source="rpc")
                if fields:
                    await self._read_after_write(fields)
                return result
            except asyncio.CancelledError:
                self.state.invalidate("rpc", fields)
                raise
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
            "ocf": self.ocf_diagnostics(),
        }
