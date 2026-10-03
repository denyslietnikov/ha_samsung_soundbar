import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api_extension.SoundbarDevice import SoundbarDevice
from .device_info import build_device_info
from .entity_updates import register_device_update_listener
from .models import SoundbarConfigEntry

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: SoundbarConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    device = config_entry.runtime_data.device
    entities = []
    entities.append(VolumeSensor(device, "volume_level", "mdi:volume-high"))
    if not device.can_select_source:
        entities.append(
            InputSourceSensor(device, "input_preset", "mdi:video-input-hdmi")
        )
    if (
        device.hybrid_mode
        or device.has_status_capability("samsungvd.soundFrom")
        or device.has_status_capability("samsungvd.audioSoundFrom")
    ):
        entities.append(SoundFromSensor(device, "sound_from", "mdi:speaker"))
    register_device_update_listener(config_entry, device, entities)
    async_add_entities(entities)


class VolumeSensor(SensorEntity):
    _soundbar_cloud_capabilities = ("audioVolume",)

    def __init__(self, device: SoundbarDevice, append_unique_id: str, icon_string: str):
        self.__device = device
        self._attr_unique_id = f"{device.device_id}_sw_{append_unique_id}"
        self.__base_icon = icon_string
        self._attr_device_info = build_device_info(self.__device)
        self.__append_unique_id = append_unique_id
        self._attr_name = "Volume Level"
        self._attr_native_unit_of_measurement = PERCENTAGE

    @property
    def icon(self) -> str | None:
        return self.__base_icon

    @property
    def native_value(self) -> int | None:
        """Return the current soundbar volume."""
        if self.__device.coordinator is not None or self.__device.hybrid_mode:
            level = self.__device.volume_level
            return None if level is None else round(level * self.__device.volume_scale)
        return self.__device.device.status.volume


class InputSourceSensor(SensorEntity):
    _soundbar_cloud_capabilities = ("mediaInputSource", "samsungvd.audioInputSource")

    def __init__(self, device: SoundbarDevice, append_unique_id: str, icon_string: str):
        self.__device = device
        self._attr_unique_id = f"{device.device_id}_sensor_{append_unique_id}"
        self.__base_icon = icon_string
        self._attr_device_info = build_device_info(self.__device)
        self._attr_name = "Input Preset"

    @property
    def icon(self) -> str | None:
        return self.__base_icon

    @property
    def native_value(self) -> str | None:
        """Return the current soundbar input source."""
        return self.__device.input_source

    @property
    def extra_state_attributes(self) -> dict[str, list[str]]:
        """Return supported input sources as diagnostic attributes."""
        return {"supported_sources": self.__device.supported_input_sources}


class SoundFromSensor(SensorEntity):
    _soundbar_cloud_capabilities = ("samsungvd.soundFrom", "samsungvd.audioSoundFrom")

    def __init__(self, device: SoundbarDevice, append_unique_id: str, icon_string: str):
        self.__device = device
        self._attr_unique_id = f"{device.device_id}_sensor_{append_unique_id}"
        self.__base_icon = icon_string
        self._attr_device_info = build_device_info(self.__device)
        self._attr_name = "Sound From"

    @property
    def icon(self) -> str | None:
        return self.__base_icon

    @property
    def native_value(self) -> str | None:
        """Return the current Samsung sound source detail name."""
        return self.__device.sound_from_detail_name

    async def async_update(self) -> None:
        """Refresh the soundbar before reading the sound source detail."""
        if self.__device.coordinator is not None:
            await self.__device.coordinator.async_request_refresh()
            return
        elif self.__device.hybrid_mode:
            await self.__device.update_local_input_source()
            return
        await self.__device.update()

    @property
    def extra_state_attributes(self) -> dict[str, int | None]:
        return {"mode": self.__device.sound_from_mode}
