import logging
import datetime

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import PERCENTAGE
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval

from .api_extension.SoundbarDevice import SoundbarDevice
from .const import CONF_ENTRY_DEVICE_ID, DOMAIN
from .models import DeviceConfig

_LOGGER = logging.getLogger(__name__)
LOCAL_REFRESH_INTERVAL = datetime.timedelta(seconds=2)


async def async_setup_entry(hass, config_entry, async_add_entities):
    domain_data = hass.data[DOMAIN]
    entities = []
    for key in domain_data.devices:
        device_config: DeviceConfig = domain_data.devices[key]
        device = device_config.device

        if device.device_id == config_entry.data.get(CONF_ENTRY_DEVICE_ID):
            entities.append(VolumeSensor(device, "volume_level", "mdi:volume-high"))
            if not device.can_select_source:
                entities.append(
                    InputSourceSensor(device, "input_preset", "mdi:video-input-hdmi")
                )
            if device.has_status_capability("samsungvd.soundFrom"):
                entities.append(SoundFromSensor(device, "sound_from", "mdi:speaker"))
    async_add_entities(entities)
    return True


class VolumeSensor(SensorEntity):
    def __init__(self, device: SoundbarDevice, append_unique_id: str, icon_string: str):
        self.__device = device
        self._attr_unique_id = f"{device.device_id}_sw_{append_unique_id}"
        self.__base_icon = icon_string
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.__device.device_id)},
            name=self.__device.device_name,
            manufacturer=self.__device.manufacturer,
            model=self.__device.model,
            sw_version=self.__device.firmware_version,
        )
        self.__append_unique_id = append_unique_id
        self._attr_name = "Volume Level"
        self._attr_native_unit_of_measurement = PERCENTAGE

    @property
    def icon(self) -> str | None:
        return self.__base_icon

    @property
    def native_value(self) -> int | None:
        """Return the current soundbar volume."""
        return self.__device.device.status.volume


class InputSourceSensor(SensorEntity):
    def __init__(self, device: SoundbarDevice, append_unique_id: str, icon_string: str):
        self.__device = device
        self._attr_unique_id = f"{device.device_id}_sensor_{append_unique_id}"
        self.__base_icon = icon_string
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.__device.device_id)},
            name=self.__device.device_name,
            manufacturer=self.__device.manufacturer,
            model=self.__device.model,
            sw_version=self.__device.firmware_version,
        )
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
    def __init__(self, device: SoundbarDevice, append_unique_id: str, icon_string: str):
        self.__device = device
        self._attr_unique_id = f"{device.device_id}_sensor_{append_unique_id}"
        self.__base_icon = icon_string
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.__device.device_id)},
            name=self.__device.device_name,
            manufacturer=self.__device.manufacturer,
            model=self.__device.model,
            sw_version=self.__device.firmware_version,
        )
        self._attr_name = "Sound From"

    @property
    def icon(self) -> str | None:
        return self.__base_icon

    @property
    def native_value(self) -> str | None:
        """Return the current Samsung sound source detail name."""
        return self.__device.sound_from_detail_name

    async def async_added_to_hass(self) -> None:
        """Register a fast local readback loop for hybrid streaming labels."""
        if not self.__device.hybrid_mode:
            return

        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_update_local_input_source,
                LOCAL_REFRESH_INTERVAL,
            )
        )

    async def _async_update_local_input_source(self, now) -> None:
        """Refresh local input source and write updated Sound From state."""
        await self.__device.update_local_input_source()
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Refresh the soundbar before reading the sound source detail."""
        if self.__device.hybrid_mode:
            await self.__device.update_local_input_source()
            return
        await self.__device.update()

    @property
    def extra_state_attributes(self) -> dict[str, int | None]:
        return {"mode": self.__device.sound_from_mode}
