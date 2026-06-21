from dataclasses import dataclass, field
from typing import Any

from pysmartthings import SmartThings

from .api_extension.SoundbarDevice import SoundbarDevice


@dataclass
class DeviceConfig:
    config: dict
    device: SoundbarDevice


@dataclass
class SoundbarConfig:
    api: SmartThings
    devices: dict
    auth_provider: Any | None = None
    subscriptions: dict[str, Any] = field(default_factory=dict)
