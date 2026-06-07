"""Config entry option helpers for Samsung Soundbar."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_CONTROL_MODE,
    CONF_ENTRY_MAX_VOLUME,
    CONF_ENTRY_SETTINGS_ADVANCED_AUDIO_SWITCHES,
    CONF_ENTRY_SETTINGS_EQ_SELECTOR,
    CONF_ENTRY_SETTINGS_SOUNDMODE_SELECTOR,
    CONF_ENTRY_SETTINGS_WOOFER_NUMBER,
    CONF_LOCAL_FALLBACK_TO_CLOUD,
    CONF_LOCAL_HOST,
    CONF_LOCAL_PORT,
    CONF_LOCAL_TIMEOUT,
    CONF_LOCAL_VERIFY_SSL,
    CONTROL_MODE_SMARTTHINGS_CLOUD,
)
from .local_rpc import DEFAULT_LOCAL_RPC_PORT, DEFAULT_LOCAL_RPC_TIMEOUT

DEFAULT_ENTRY_OPTIONS = {
    CONF_ENTRY_MAX_VOLUME: 100,
    CONF_ENTRY_SETTINGS_ADVANCED_AUDIO_SWITCHES: True,
    CONF_ENTRY_SETTINGS_EQ_SELECTOR: False,
    CONF_ENTRY_SETTINGS_SOUNDMODE_SELECTOR: False,
    CONF_ENTRY_SETTINGS_WOOFER_NUMBER: False,
    CONF_CONTROL_MODE: CONTROL_MODE_SMARTTHINGS_CLOUD,
    CONF_LOCAL_HOST: "",
    CONF_LOCAL_PORT: DEFAULT_LOCAL_RPC_PORT,
    CONF_LOCAL_VERIFY_SSL: False,
    CONF_LOCAL_TIMEOUT: DEFAULT_LOCAL_RPC_TIMEOUT,
    CONF_LOCAL_FALLBACK_TO_CLOUD: True,
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
