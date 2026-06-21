import logging

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)

from .api_extension.SoundbarDevice import SoundbarDevice
from .const import CONF_ENTRY_DEVICE_ID, DOMAIN
from .device_info import build_device_info
from .entity_updates import register_device_update_listener
from .models import DeviceConfig

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    domain_data = hass.data[DOMAIN]

    entities = []
    for key in domain_data.devices:
        device_config: DeviceConfig = domain_data.devices[key]
        device = device_config.device
        if device.device_id == config_entry.data.get(
            CONF_ENTRY_DEVICE_ID
        ) and device.can_control_woofer_level:
            entities.append(
                SoundbarWooferNumberEntity(
                    device,
                    "woofer_level",
                )
            )
            register_device_update_listener(config_entry, device, entities)
    async_add_entities(entities)
    return True


class SoundbarWooferNumberEntity(NumberEntity):
    def __init__(
        self,
        device: SoundbarDevice,
        append_unique_id: str,
    ):
        self.entity_description = NumberEntityDescription(
            native_max_value=6,
            native_min_value=-12,
            mode=NumberMode.BOX,
            native_step=1,
            native_unit_of_measurement="dB",
            key=append_unique_id,
        )
        self.__device = device
        self._attr_unique_id = f"{device.device_id}_sw_{append_unique_id}"
        self._attr_device_info = build_device_info(self.__device)
        self.__append_unique_id = append_unique_id

    # ---------- GENERAL ---------------

    @property
    def name(self):
        return self.__append_unique_id

    # ------ STATE FUNCTIONS --------

    @property
    def native_value(self) -> float | None:
        return self.__device.woofer_level

    async def async_set_native_value(self, value: float):
        await self.__device.set_woofer(int(value))
