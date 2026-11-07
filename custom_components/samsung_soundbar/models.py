"""Typed runtime state owned by a single config entry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry

if TYPE_CHECKING:
    from .api_extension.SoundbarDevice import SoundbarDevice
    from .coordinator import SoundbarCoordinator
    from .local_ocf import LocalSoundbarOcfClient


@dataclass(slots=True)
class SoundbarRuntimeData:
    """Clients, state and reload snapshot for this soundbar only."""

    device: SoundbarDevice
    coordinator: SoundbarCoordinator
    config: dict[str, Any]
    options: dict[str, Any]
    ocf: LocalSoundbarOcfClient | None = field(default=None, repr=False)


type SoundbarConfigEntry = ConfigEntry[SoundbarRuntimeData]
