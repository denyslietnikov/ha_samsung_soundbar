"""Typed runtime state owned by a single config entry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from pysmartthings import SmartThings

if TYPE_CHECKING:
    from .api_extension.SoundbarDevice import SoundbarDevice
    from .auth import SmartThingsAuthProvider
    from .coordinator import SoundbarCoordinator
    from .subscription import SmartThingsSubscriptionRuntime


@dataclass(slots=True)
class SoundbarRuntimeData:
    """Clients, state and reload snapshot for this soundbar only."""

    device: SoundbarDevice
    coordinator: SoundbarCoordinator
    config: dict[str, Any]
    options: dict[str, Any]
    api: SmartThings | None = None
    auth_provider: SmartThingsAuthProvider | None = None
    subscription: SmartThingsSubscriptionRuntime | None = None
    reauth_pending: bool = False


type SoundbarConfigEntry = ConfigEntry[SoundbarRuntimeData]
