"""Stable local device identity and optional display metadata."""

from dataclasses import dataclass, field


@dataclass
class LocalStatus:
    """Optional display metadata; live state comes from the local transports."""

    ocf_manufacturer_name: str = "Samsung Electronics"
    ocf_model_number: str | None = None
    ocf_firmware_version: str | None = None


@dataclass
class LocalDevice:
    """Retain the existing entry device ID independently of display metadata."""

    device_id: str
    manufacturer: str | None = None
    model: str | None = None
    firmware: str | None = None
    status: LocalStatus = field(default_factory=LocalStatus)
