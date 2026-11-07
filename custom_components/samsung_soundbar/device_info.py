"""Build device metadata from verified local properties only."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.entity import DeviceInfo

from .api_extension.SoundbarDevice import SoundbarDevice
from .const import DOMAIN


def build_device_info(device: SoundbarDevice) -> DeviceInfo:
    """Omit unknown metadata so existing registry values survive partial reads."""
    info = DeviceInfo(
        identifiers={(DOMAIN, device.device_id)},
        name=device.device_name,
        configuration_url=None,
    )
    for key, value in (
        ("manufacturer", _clean(device.manufacturer)),
        ("model", _normalize_model(device.model)),
        ("sw_version", _clean(device.firmware_version)),
        ("suggested_area", _clean(device.suggested_area)),
    ):
        if value is not None:
            info[key] = value
    return info


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or cleaned.upper() in {"NONE", "NULL", "UNKNOWN"}:
        return None
    return cleaned


def _normalize_model(value: Any) -> str | None:
    model = _clean(value)
    return _clean(model.split("|", maxsplit=1)[0]) if model else None
