"""Helpers for propagating soundbar push updates to entities."""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.core import callback
from homeassistant.helpers.entity import Entity

from .api_extension.SoundbarDevice import SoundbarDevice
from .models import SoundbarConfigEntry
from .ocf_profiles import Q800F_AUDIO_SWITCHES


def register_device_update_listener(
    config_entry: SoundbarConfigEntry,
    device: SoundbarDevice,
    entities: Iterable[Entity],
) -> None:
    """Write entity states when the shared device cache receives a push update."""
    tracked_entities = tuple(entities)
    coordinator = device.coordinator

    def available_for(entity):
        tone_field = getattr(entity, "_soundbar_tone_field", None)
        if isinstance(tone_field, str):
            return device.tone_control_available(tone_field)
        audio_field = getattr(entity, "_soundbar_audio_field", None)
        if isinstance(audio_field, str) and audio_field in Q800F_AUDIO_SWITCHES:
            return device.audio_switch_available(audio_field)
        if getattr(entity, "_soundbar_woofer_absolute", False) is True:
            return device.woofer_control_available
        ocf_fields = getattr(entity, "_soundbar_ocf_fields", ())
        if isinstance(ocf_fields, (tuple, list)) and ocf_fields:
            return (
                coordinator is not None
                and all(coordinator.ocf_field_available(key) for key in ocf_fields)
                and (
                    not getattr(entity, "_soundbar_requires_on", False)
                    or device.state in ("on", "playing", "paused")
                )
            )
        if getattr(entity, "_soundbar_local_only", False):
            local_available = (
                coordinator.state.snapshot().local_available
                if coordinator is not None
                else device.local_available
            )
            return local_available and (
                not getattr(entity, "_soundbar_requires_on", False)
                or device.state in ("on", "playing", "paused")
            )
        return device.available

    for entity in tracked_entities:
        entity._attr_available = available_for(entity)
        if coordinator is not None or device.local_rpc_enabled:
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
