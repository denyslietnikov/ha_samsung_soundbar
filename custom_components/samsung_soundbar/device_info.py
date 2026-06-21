"""Build normalized Home Assistant device metadata for SmartThings soundbars."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo

from .api_extension.SoundbarDevice import SoundbarDevice
from .const import DOMAIN

SMARTTHINGS_CONFIGURATION_URL = "https://account.smartthings.com"
_LOGGER = logging.getLogger(__name__)


def async_unmerge_official_smartthings_device(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device_id: str,
) -> None:
    """Split a device previously merged through shared network connections."""
    registry = dr.async_get(hass)
    own_identifier = (DOMAIN, device_id)
    existing = registry.async_get_device(identifiers={own_identifier})
    if existing is None or not any(
        identifier_domain == "smartthings"
        for identifier_domain, _ in existing.identifiers
    ):
        return

    remaining_identifiers = existing.identifiers - {own_identifier}
    if not remaining_identifiers:
        return

    registry.async_update_device(
        existing.id,
        remove_config_entry_id=entry.entry_id,
        new_identifiers=remaining_identifiers,
    )
    _LOGGER.info(
        "Separated Samsung Soundbar device %s from the official SmartThings "
        "device registry entry",
        device_id,
    )


def build_device_info(device: SoundbarDevice) -> DeviceInfo:
    """Return normalized device registry metadata without guessing identifiers."""
    smartthings_device = device.smartthings_device
    manufacturer = _clean(device.manufacturer)
    model = _clean(device.model)
    model_id = None
    sw_version = _clean(device.firmware_version)
    hw_version = None
    serial_number = None

    if (hub := getattr(smartthings_device, "hub", None)) is not None:
        model = _clean(getattr(hub, "hardware_type", None)) or model
        sw_version = _clean(getattr(hub, "firmware_version", None)) or sw_version

    if (ocf := getattr(smartthings_device, "ocf", None)) is not None:
        manufacturer = _clean(getattr(ocf, "manufacturer_name", None)) or manufacturer
        model_id = _clean(getattr(ocf, "model_code", None)) or model_id
        model = _normalize_model(getattr(ocf, "model_number", None)) or model
        hw_version = _clean(getattr(ocf, "hardware_version", None)) or hw_version
        sw_version = _clean(getattr(ocf, "firmware_version", None)) or sw_version

    if (viper := getattr(smartthings_device, "viper", None)) is not None:
        manufacturer = (
            _clean(getattr(viper, "manufacturer_name", None)) or manufacturer
        )
        model = _clean(getattr(viper, "model_name", None)) or model
        hw_version = _clean(getattr(viper, "hardware_version", None)) or hw_version
        sw_version = _clean(getattr(viper, "software_version", None)) or sw_version

    if (matter := getattr(smartthings_device, "matter", None)) is not None:
        hw_version = _clean(getattr(matter, "hardware_version", None)) or hw_version
        sw_version = _clean(getattr(matter, "software_version", None)) or sw_version
        serial_number = (
            _clean(getattr(matter, "serial_number", None)) or serial_number
        )

    manufacturer = manufacturer or _clean(_status_value(device, "ocf", "mnmn"))
    model = model or _normalize_model(_status_value(device, "ocf", "mnmo"))
    hw_version = hw_version or _clean(_status_value(device, "ocf", "mnhw"))
    sw_version = sw_version or _clean(_status_value(device, "ocf", "mnfv"))
    serial_number = (
        _status_value(device, "samsungce.deviceIdentification", "serialNumber")
        or _status_value(device, "samsungvd.deviceInfoPrivate", "sn")
        or _status_value(device, "ocf", "mnsl")
        or serial_number
    )
    model_id = (
        model_id
        or _status_value(device, "samsungce.deviceIdentification", "modelName")
        or _status_value(device, "samsungvd.deviceInfoPrivate", "modelid")
        or _status_value(device, "samsungvd.deviceInfoPrivate", "swmodel")
    )

    return DeviceInfo(
        identifiers={(DOMAIN, device.device_id)},
        configuration_url=SMARTTHINGS_CONFIGURATION_URL,
        name=device.device_name,
        manufacturer=manufacturer,
        model=model,
        model_id=_clean(model_id),
        hw_version=hw_version,
        sw_version=sw_version,
        serial_number=_clean(serial_number),
        suggested_area=_clean(device.suggested_area),
    )


def _status_value(
    device: SoundbarDevice,
    capability: str,
    attribute: str,
) -> Any | None:
    value = device.status_attribute_value(capability, attribute)
    return _clean(value) if isinstance(value, str) else value


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or cleaned.upper() in {"NONE", "NULL", "UNKNOWN"}:
        return None
    return cleaned


def _normalize_model(value: Any) -> str | None:
    model = _clean(value)
    return model.split("|", maxsplit=1)[0] if model else None

