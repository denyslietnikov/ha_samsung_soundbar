"""Build normalized Home Assistant device metadata for SmartThings soundbars."""

from __future__ import annotations

import re
from typing import Any

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo

from .api_extension.SoundbarDevice import SoundbarDevice
from .const import DOMAIN

SMARTTHINGS_CONFIGURATION_URL = "https://account.smartthings.com"


def build_device_info(device: SoundbarDevice) -> DeviceInfo:
    """Return normalized device registry metadata without guessing identifiers."""
    smartthings_device = device.smartthings_device
    manufacturer = _clean(device.manufacturer)
    model = _clean(device.model)
    model_id = None
    sw_version = _clean(device.firmware_version)
    hw_version = None
    serial_number = None
    connections: set[tuple[str, str]] = set()

    if (hub := getattr(smartthings_device, "hub", None)) is not None:
        model = _clean(getattr(hub, "hardware_type", None)) or model
        sw_version = _clean(getattr(hub, "firmware_version", None)) or sw_version
        _add_mac_connection(
            connections,
            dr.CONNECTION_NETWORK_MAC,
            getattr(hub, "mac_address", None),
        )
        if zigbee_address := _normalize_zigbee_address(
            getattr(hub, "hub_eui", None)
        ):
            connections.add((dr.CONNECTION_ZIGBEE, zigbee_address))

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

    if (zigbee := getattr(smartthings_device, "zigbee", None)) is not None:
        if zigbee_address := _normalize_zigbee_address(
            getattr(zigbee, "eui", None)
        ):
            connections.add((dr.CONNECTION_ZIGBEE, zigbee_address))

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

    device_status = _status_value(device, "samsungim.devicestatus", "status")
    if isinstance(device_status, dict):
        _add_mac_connection(
            connections,
            dr.CONNECTION_NETWORK_MAC,
            device_status.get("wifiMac"),
        )
        _add_mac_connection(
            connections,
            dr.CONNECTION_BLUETOOTH,
            device_status.get("btAddr"),
        )

    _add_mac_connection(
        connections,
        dr.CONNECTION_NETWORK_MAC,
        _status_value(device, "samsungvd.deviceInfoPrivate", "wifimac"),
    )
    _add_mac_connection(
        connections,
        dr.CONNECTION_BLUETOOTH,
        _status_value(device, "samsungvd.deviceInfoPrivate", "btmac")
        or _status_value(device, "samsungvd.deviceInfoPrivate", "blemac"),
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
        connections=connections,
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


def _add_mac_connection(
    connections: set[tuple[str, str]],
    connection_type: str,
    value: Any,
) -> None:
    if mac_address := _normalize_mac(value):
        connections.add((connection_type, mac_address))


def _normalize_mac(value: Any) -> str | None:
    address = _clean(value)
    if address is None:
        return None
    compact = re.sub(r"[^0-9A-Fa-f]", "", address)
    if len(compact) != 12:
        return None
    return ":".join(
        compact[index : index + 2].lower() for index in range(0, 12, 2)
    )


def _normalize_zigbee_address(value: Any) -> str | None:
    address = _clean(value)
    if address is None:
        return None
    compact = re.sub(r"[^0-9A-Fa-f]", "", address)
    if len(compact) != 16:
        return None
    return ":".join(
        compact[index : index + 2].lower() for index in range(0, 16, 2)
    )
