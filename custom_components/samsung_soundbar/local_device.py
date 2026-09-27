"""Minimal device facade for entries that have no SmartThings transport."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class LocalStatus:
    """Empty cloud status; live values come from local JSON-RPC."""

    attributes: dict[str, Any] = field(default_factory=dict)
    ocf_manufacturer_name: str = "Samsung Electronics"
    ocf_model_number: str | None = None
    ocf_firmware_version: str | None = None
    playback_status: str | None = None
    switch: bool = False
    volume: int = 0
    mute: bool = False
    input_source: str | None = None
    supported_input_sources: list[str] = field(default_factory=list)
    sound_from_detail_name: str | None = None
    sound_from_mode: int | None = None

    def has_capability(self, capability: str) -> bool:
        return False

    def attribute_value(
        self, capability: str, attribute: str, default: Any = None
    ) -> Any:
        return default


@dataclass
class LocalDevice:
    """Retain the existing entry device ID without constructing a cloud client."""

    device_id: str
    status: LocalStatus = field(default_factory=LocalStatus)
