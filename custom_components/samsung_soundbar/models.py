from dataclasses import dataclass, field
from typing import Any

from pysmartthings import SmartThings

from .api_extension.SoundbarDevice import SoundbarDevice


@dataclass
class DeviceConfig:
    config: dict
    device: SoundbarDevice
    options: dict | None = None


@dataclass
class SoundbarConfig:
    api: SmartThings | None
    devices: dict
    auth_provider: Any | None = None
    subscriptions: dict[str, Any] = field(default_factory=dict)
