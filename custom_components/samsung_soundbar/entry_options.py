"""Config entry option helpers for Samsung Soundbar."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_ENTRY_MAX_VOLUME,
    CONF_ENTRY_SETTINGS_ADVANCED_AUDIO_SWITCHES,
    CONF_ENTRY_SETTINGS_EQ_SELECTOR,
    CONF_ENTRY_SETTINGS_SOUNDMODE_SELECTOR,
    CONF_ENTRY_SETTINGS_WOOFER_NUMBER,
)

DEFAULT_ENTRY_OPTIONS = {
    CONF_ENTRY_MAX_VOLUME: 100,
    CONF_ENTRY_SETTINGS_ADVANCED_AUDIO_SWITCHES: True,
    CONF_ENTRY_SETTINGS_EQ_SELECTOR: False,
    CONF_ENTRY_SETTINGS_SOUNDMODE_SELECTOR: False,
    CONF_ENTRY_SETTINGS_WOOFER_NUMBER: False,
}


def get_entry_option(entry: ConfigEntry, key: str) -> Any:
    """Return an option value, falling back to legacy entry data."""
    if key in entry.options:
        return entry.options[key]
    if key in entry.data:
        return entry.data[key]
    return DEFAULT_ENTRY_OPTIONS[key]


def get_entry_options(entry: ConfigEntry) -> dict[str, Any]:
    """Return normalized integration options for an entry."""
    return {key: get_entry_option(entry, key) for key in DEFAULT_ENTRY_OPTIONS}
