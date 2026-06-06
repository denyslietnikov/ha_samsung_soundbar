"""Compatibility helpers for modern pysmartthings device models."""

from __future__ import annotations

from typing import Any


class SmartThingsStatusCompat:
    """Expose old DeviceEntity status helpers on top of current pysmartthings."""

    def __init__(self, api: Any, device: Any) -> None:
        """Initialize the status adapter."""
        self._api = api
        self._device = device
        self._components: dict[str, Any] = {}
        self._attributes: dict[str, Any] = {}

    async def refresh(self) -> None:
        """Refresh device status from SmartThings."""
        self._components = await self._api.get_device_status(self._device.device_id)
        self._attributes = self._flatten_attributes()

    def _flatten_attributes(self) -> dict[str, Any]:
        """Flatten SmartThings component/capability status by attribute name."""
        attributes: dict[str, Any] = {}
        for capabilities in self._components.values():
            for capability_status in capabilities.values():
                for attribute, status in capability_status.items():
                    attributes[self._key_name(attribute)] = status
        return attributes

    @staticmethod
    def _key_name(key: Any) -> str:
        """Return a stable string name for enum or string status keys."""
        return str(getattr(key, "value", key))

    def _status(
        self, capability: str, attribute: str, component: str = "main"
    ) -> Any | None:
        """Return a SmartThings Status object for a capability attribute."""
        capabilities = self._components.get(component, {})
        for capability_key, capability_status in capabilities.items():
            if self._key_name(capability_key) != capability:
                continue
            for attribute_key, status in capability_status.items():
                if self._key_name(attribute_key) == attribute:
                    return status
        return None

    def _value(
        self, capability: str, attribute: str, default: Any = None
    ) -> Any:
        """Return a SmartThings status value."""
        status = self._status(capability, attribute)
        return default if status is None else status.value

    @property
    def attributes(self) -> dict[str, Any]:
        """Return flattened attributes."""
        return self._attributes

    @property
    def ocf_manufacturer_name(self) -> str | None:
        """Return manufacturer name."""
        ocf = getattr(self._device, "ocf", None)
        return getattr(ocf, "manufacturer_name", None)

    @property
    def ocf_model_number(self) -> str | None:
        """Return model number."""
        ocf = getattr(self._device, "ocf", None)
        return getattr(ocf, "model_number", None) or getattr(
            self._device, "device_type_name", None
        )

    @property
    def ocf_firmware_version(self) -> str | None:
        """Return firmware version."""
        ocf = getattr(self._device, "ocf", None)
        return getattr(ocf, "firmware_version", None)

    @property
    def switch(self) -> bool:
        """Return switch state."""
        return self._value("switch", "switch") == "on"

    @property
    def playback_status(self) -> str | None:
        """Return media playback state."""
        return self._value("mediaPlayback", "playbackStatus")

    @property
    def volume(self) -> int:
        """Return audio volume."""
        return int(self._value("audioVolume", "volume", 0) or 0)

    @property
    def mute(self) -> bool:
        """Return mute state."""
        value = self._value("audioMute", "mute")
        return value in (True, "muted", "on")

    @property
    def input_source(self) -> str | None:
        """Return active input source."""
        return self._value("mediaInputSource", "inputSource")

    @property
    def supported_input_sources(self) -> list[str]:
        """Return supported input sources."""
        sources = self._value("mediaInputSource", "supportedInputSources", [])
        return sources if isinstance(sources, list) else []


class SmartThingsDeviceCompat:
    """Expose old DeviceEntity methods on top of current pysmartthings."""

    def __init__(self, api: Any, device: Any) -> None:
        """Initialize the device adapter."""
        self._api = api
        self._device = device
        self.status = SmartThingsStatusCompat(api, device)

    @property
    def device_id(self) -> str:
        """Return device id."""
        return self._device.device_id

    async def command(
        self,
        component: str,
        capability: str,
        command: str,
        argument: Any | None = None,
    ) -> bool:
        """Execute a SmartThings command."""
        await self._api.execute_device_command(
            self.device_id,
            capability,
            command,
            component=component,
            argument=argument,
        )
        return True

    async def switch_off(self, wait: bool = False) -> bool:
        """Switch the device off."""
        return await self.command("main", "switch", "off")

    async def switch_on(self, wait: bool = False) -> bool:
        """Switch the device on."""
        return await self.command("main", "switch", "on")

    async def set_volume(self, volume: int, wait: bool = False) -> bool:
        """Set audio volume."""
        return await self.command("main", "audioVolume", "setVolume", volume)

    async def mute(self, wait: bool = False) -> bool:
        """Mute audio."""
        return await self.command("main", "audioMute", "mute")

    async def unmute(self, wait: bool = False) -> bool:
        """Unmute audio."""
        return await self.command("main", "audioMute", "unmute")

    async def volume_up(self, wait: bool = False) -> bool:
        """Increase audio volume."""
        return await self.command("main", "audioVolume", "volumeUp")

    async def volume_down(self, wait: bool = False) -> bool:
        """Decrease audio volume."""
        return await self.command("main", "audioVolume", "volumeDown")

    async def set_input_source(self, source: str, wait: bool = False) -> bool:
        """Set input source."""
        return await self.command("main", "mediaInputSource", "setInputSource", source)

    async def play(self, wait: bool = False) -> bool:
        """Start playback."""
        return await self.command("main", "mediaPlayback", "play")

    async def pause(self, wait: bool = False) -> bool:
        """Pause playback."""
        return await self.command("main", "mediaPlayback", "pause")

    async def stop(self, wait: bool = False) -> bool:
        """Stop playback."""
        return await self.command("main", "mediaPlayback", "stop")


def ensure_device_entity(api: Any, device: Any) -> Any:
    """Return a device object compatible with the old integration code."""
    if hasattr(device, "status"):
        return device
    return SmartThingsDeviceCompat(api, device)
