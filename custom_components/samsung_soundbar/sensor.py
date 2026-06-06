import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import PERCENTAGE
from homeassistant.helpers.entity import DeviceInfo

from .api_extension.SoundbarDevice import SoundbarDevice
from .const import CONF_ENTRY_DEVICE_ID, DOMAIN
from .models import DeviceConfig

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    domain_data = hass.data[DOMAIN]
    entities = []
    for key in domain_data.devices:
        device_config: DeviceConfig = domain_data.devices[key]
        device = device_config.device

        if device.device_id == config_entry.data.get(CONF_ENTRY_DEVICE_ID):
            entities.append(VolumeSensor(device, "volume_level", "mdi:volume-high"))
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
