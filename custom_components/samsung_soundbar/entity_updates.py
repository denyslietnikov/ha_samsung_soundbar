"""Helpers for propagating soundbar push updates to entities."""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.entity import Entity

from .api_extension.SoundbarDevice import SoundbarDevice


def register_device_update_listener(
    config_entry: ConfigEntry,
    device: SoundbarDevice,
    entities: Iterable[Entity],
) -> None:
    """Write entity states when the shared device cache receives a push update."""
    tracked_entities = tuple(entities)
    for entity in tracked_entities:
        entity._attr_available = device.available

    @callback
    def async_write_states() -> None:
        for entity in tracked_entities:
            entity._attr_available = device.available
            if getattr(entity, "hass", None) is not None:
                entity.async_write_ha_state()

    config_entry.async_on_unload(device.add_update_listener(async_write_states))
