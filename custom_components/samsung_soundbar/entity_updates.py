"""Helpers for propagating soundbar push updates to entities."""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.core import callback
from homeassistant.helpers.entity import Entity

from .api_extension.SoundbarDevice import SoundbarDevice
from .models import SoundbarConfigEntry


def register_device_update_listener(
    config_entry: SoundbarConfigEntry,
    device: SoundbarDevice,
    entities: Iterable[Entity],
) -> None:
    """Write entity states when the shared device cache receives a push update."""
    tracked_entities = tuple(entities)
    coordinator = device.coordinator

    def available_for(entity):
        if getattr(entity, "_soundbar_woofer_absolute", False) is True:
            return device.woofer_control_available
        if getattr(entity, "_soundbar_woofer_relative", False) is True and device.ocf_woofer_profile is not None:
            return False
        ocf_fields = getattr(entity, "_soundbar_ocf_fields", ())
        if isinstance(ocf_fields, (tuple, list)) and ocf_fields:
            return coordinator is not None and all(
                coordinator.ocf_field_available(key) for key in ocf_fields
            ) and (
                not getattr(entity, "_soundbar_requires_on", False)
                or device.state in ("on", "playing", "paused")
            )
        if getattr(entity, "_soundbar_local_only", False):
            local_available = (
                coordinator.state.snapshot().local_available
                if coordinator is not None else device.local_available
            )
            return local_available and (
                not getattr(entity, "_soundbar_requires_on", False)
                or device.state in ("on", "playing", "paused")
            )
        capabilities = getattr(entity, "_soundbar_cloud_capabilities", ())
        if (
            capabilities
            and (
                getattr(entity, "_soundbar_cloud_only", False) or not device.hybrid_mode
            )
            and device.cloud_feature_disabled(*capabilities)
        ):
            return False
        if coordinator is not None and getattr(entity, "_soundbar_cloud_only", False):
            return coordinator.state.snapshot().cloud_available
        return device.available

    for entity in tracked_entities:
        entity._attr_available = available_for(entity)
        if coordinator is not None or device.hybrid_mode:
            entity._attr_should_poll = False

    @callback
    def async_write_states() -> None:
        for entity in tracked_entities:
            entity._attr_available = available_for(entity)
            if getattr(entity, "hass", None) is not None:
                entity.async_write_ha_state()

    config_entry.async_on_unload(
        coordinator.async_add_listener(async_write_states)
        if coordinator is not None
        else device.add_update_listener(async_write_states)
    )
