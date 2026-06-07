import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.entity import DeviceInfo

from .api_extension.SoundbarDevice import SoundbarDevice
from .const import CONF_ENTRY_DEVICE_ID, DOMAIN
from .models import DeviceConfig

_LOGGER = logging.getLogger(__name__)

SWITCH_ENTITY_NAMES = {
    "bassmode": "Bassmode",
    "nightmode": "Nightmode",
    "voice_amplifier": "Voice Amplifier",
}


async def async_setup_entry(hass, config_entry, async_add_entities):
    domain_data = hass.data[DOMAIN]

    entities = []
    for key in domain_data.devices:
        device_config: DeviceConfig = domain_data.devices[key]
        device = device_config.device
        if device.device_id == config_entry.data.get(
            CONF_ENTRY_DEVICE_ID
        ) and device.can_control_advanced_audio:
            entities.append(
                SoundbarSwitchAdvancedAudio(
                    device,
                    "nightmode",
                    lambda: device.night_mode,
                    device.set_night_mode,
                    device.set_night_mode,
                    "mdi:weather-night",
                )
            )
            entities.append(
                SoundbarSwitchAdvancedAudio(
                    device,
                    "bassmode",
                    lambda: device.bass_mode,
                    device.set_bass_mode,
                    device.set_bass_mode,
                    "mdi:speaker-wireless",
                )
            )
            entities.append(
                SoundbarSwitchAdvancedAudio(
                    device,
                    "voice_amplifier",
                    lambda: device.voice_amplifier,
                    device.set_voice_amplifier,
                    device.set_voice_amplifier,
                    "mdi:account-voice",
                )
            )
    async_add_entities(entities)
    return True


class SoundbarSwitchAdvancedAudio(SwitchEntity):
    def __init__(
        self,
        device: SoundbarDevice,
        append_unique_id: str,
        state_function,
        on_function,
        off_function,
        icon_string: str = "mdi:toggle-switch-variant",
    ):
        self.__device = device
        display_name = SWITCH_ENTITY_NAMES.get(append_unique_id, append_unique_id)
        self._name = f"{self.__device.device_name} {display_name}"
        self._attr_unique_id = f"{device.device_id}_sw_{append_unique_id}"
        self.__base_icon = icon_string
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.__device.device_id)},
            name=self.__device.device_name,
            manufacturer=self.__device.manufacturer,
            model=self.__device.model,
            sw_version=self.__device.firmware_version,
        )

        self.__state_function = state_function
        self.__state = False
        self.__on_function = on_function
        self.__off_function = off_function

    # ---------- GENERAL ---------------

    @property
    def name(self):
        return self._name

    async def async_update(self):
        if self.__device.has_advanced_audio_state:
            self.__state = self.__state_function()

    @property
    def icon(self) -> str | None:
        return self.__base_icon

    # ------ STATE FUNCTIONS --------
    @property
    def is_on(self) -> bool:
        return bool(self.__state)

    async def async_turn_off(self):
        await self.__off_function(False)
        self.__state = False
        self.async_write_ha_state()

    async def async_turn_on(self):
        await self.__on_function(True)
        self.__state = True
        self.async_write_ha_state()
