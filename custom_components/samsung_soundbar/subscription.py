"""SmartThings SSE subscription lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_TOKEN, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from pysmartthings import DeviceEvent, DeviceHealthEvent, Lifecycle, SmartThings
from pysmartthings.exceptions import SmartThingsError
from pysmartthings.models import HealthStatus

from .api_extension.SoundbarDevice import SoundbarDevice
from .const import (
    CONF_INSTALLED_APP_ID,
    CONF_LOCATION_ID,
    CONF_SUBSCRIPTION_ID,
    SMARTTHINGS_REQUIRED_SCOPES,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class SmartThingsSubscriptionRuntime:
    """Track a config entry's active SmartThings SSE task."""

    client: SmartThings
    task: asyncio.Task[Any]
    stop_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stopped: bool = False


async def async_setup_subscription(
    hass: HomeAssistant,
    entry: ConfigEntry,
    client: SmartThings,
    device: SoundbarDevice,
) -> SmartThingsSubscriptionRuntime | None:
    """Set up SmartThings push events, falling back to polling when unavailable."""
    token = entry.data.get(CONF_TOKEN, {})
    granted_scopes = set(str(token.get("scope", "")).split())
    location_id = entry.data.get(CONF_LOCATION_ID)
    installed_app_id = token.get(CONF_INSTALLED_APP_ID)

    if not set(SMARTTHINGS_REQUIRED_SCOPES) <= granted_scopes:
        _LOGGER.info(
            "SmartThings push updates disabled for %s: OAuth token lacks required "
            "device scopes",
            device.device_name,
        )
        return None
    if "sse" not in granted_scopes:
        _LOGGER.debug(
            "SmartThings push updates disabled for %s: public OAuth-In apps do not "
            "support the privileged SSE scope; polling remains active",
            device.device_name,
        )
        return None
    if not location_id or not installed_app_id:
        _LOGGER.info(
            "SmartThings push updates disabled for %s: OAuth token is missing "
            "location_id or installed_app_id",
            device.device_name,
        )
        return None

    @callback
    def handle_new_subscription_id(subscription_id: str | None) -> None:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_SUBSCRIPTION_ID: subscription_id},
        )

    @callback
    def handle_max_connections() -> None:
        _LOGGER.warning(
            "SmartThings subscription connection limit reached; scheduling reload"
        )
        hass.config_entries.async_schedule_reload(entry.entry_id)

    @callback
    def handle_device_event(event: DeviceEvent) -> None:
        entry.async_create_background_task(
            hass,
            device.async_handle_smartthings_event(event),
            f"smartthings_event_{device.device_id}",
        )

    @callback
    def handle_availability(event: DeviceHealthEvent) -> None:
        status = getattr(event, "status", None)
        status_value = str(getattr(status, "value", status))
        device.handle_smartthings_availability(
            status_value == HealthStatus.ONLINE.value
        )

    @callback
    def handle_deleted_device(device_id: str) -> None:
        if device_id == device.device_id:
            device.handle_smartthings_availability(False)

    client.new_subscription_id_callback = handle_new_subscription_id
    client.max_connections_reached_callback = handle_max_connections
    entry.async_on_unload(
        client.add_device_event_listener(device.device_id, handle_device_event)
    )
    entry.async_on_unload(
        client.add_device_availability_event_listener(
            device.device_id,
            handle_availability,
        )
    )
    entry.async_on_unload(
        client.add_device_lifecycle_event_listener(
            Lifecycle.DELETE,
            handle_deleted_device,
        )
    )

    try:
        health = await client.get_device_health(device.device_id)
    except SmartThingsError as err:
        _LOGGER.debug(
            "Could not read initial SmartThings health for %s: %s",
            device.device_name,
            err,
        )
    else:
        device.handle_smartthings_availability(
            health.state == HealthStatus.ONLINE
        )

    old_subscription_id = entry.data.get(CONF_SUBSCRIPTION_ID)
    if old_subscription_id is not None:
        try:
            await client.delete_subscription(old_subscription_id)
        except SmartThingsError as err:
            _LOGGER.warning(
                "Could not delete previous SmartThings subscription for %s; "
                "polling remains active: %s",
                device.device_name,
                err,
            )
            return None

    try:
        subscription = await client.create_subscription(
            location_id,
            installed_app_id,
        )
    except SmartThingsError as err:
        _LOGGER.warning(
            "Could not enable SmartThings push updates for %s; polling remains "
            "active: %s",
            device.device_name,
            err,
        )
        return None

    handle_new_subscription_id(subscription.subscription_id)
    task = entry.async_create_background_task(
        hass,
        client.subscribe(location_id, installed_app_id, subscription),
        f"smartthings_sse_{device.device_id}",
    )
    runtime = SmartThingsSubscriptionRuntime(client, task)

    async def async_handle_shutdown(_: Event) -> None:
        await async_remove_subscription(entry, runtime)

    entry.async_on_unload(
        hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP,
            async_handle_shutdown,
        )
    )
    return runtime


async def async_remove_subscription(
    entry: ConfigEntry,
    runtime: SmartThingsSubscriptionRuntime,
) -> None:
    """Remove the active SmartThings subscription during config-entry unload."""
    async with runtime.stop_lock:
        if runtime.stopped:
            return
        runtime.stopped = True

        if not runtime.task.done():
            runtime.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runtime.task

        subscription_id = entry.data.get(CONF_SUBSCRIPTION_ID)
        if subscription_id is None:
            return
        try:
            await runtime.client.delete_subscription(subscription_id)
        except SmartThingsError as err:
            _LOGGER.debug(
                "Could not delete SmartThings subscription %s during unload: %s",
                subscription_id,
                err,
            )
