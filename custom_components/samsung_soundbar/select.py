import logging

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity

from .api_extension.SoundbarDevice import SoundbarDevice
from .const import (
    CONF_ENTRY_DEVICE_ID,
    DOMAIN,
)
from .models import DeviceConfig

_LOGGER = logging.getLogger(__name__)
_INVALID_RESTORED_STATES = {STATE_UNAVAILABLE, STATE_UNKNOWN, None}

SELECT_ENTITY_NAMES = {
    "eq_preset": "EQ Preset",
    "sound_mode_preset": "Sound Mode",
    "input_preset": "Input Preset",
}


def _select_options(options: list[str] | None, current: str | None) -> list[str]:
    """Return select options with the current value included."""
    values = list(options or [])
    if current and current not in values:
        values.append(current)
    return values


async def async_setup_entry(hass, config_entry, async_add_entities):
    domain_data = hass.data[DOMAIN]
    entities = []
    for key in domain_data.devices:
        device_config: DeviceConfig = domain_data.devices[key]
        device = device_config.device
        if device.device_id == config_entry.data.get(CONF_ENTRY_DEVICE_ID):
            if device.can_select_equalizer_preset:
                entities.append(
                    EqPresetSelectEntity(device, "eq_preset", "mdi:tune-vertical")
                )
            if device.can_select_sound_mode:
                entities.append(
                    SoundModeSelectEntity(
                        device, "sound_mode_preset", "mdi:surround-sound"
                    )
                )

            if device.can_select_source:
                entities.append(
                    InputSelectEntity(device, "input_preset", "mdi:video-input-hdmi")
                )
    async_add_entities(entities)
    return True


class EqPresetSelectEntity(SelectEntity):
    def __init__(
        self,
        device: SoundbarDevice,
        append_unique_id: str,
        icon_string: str,
    ):
        self.entity_description = SelectEntityDescription(
            key=append_unique_id,
        )
        self.__base_icon = icon_string
        self.__device = device
        self._attr_unique_id = f"{device.device_id}_sw_{append_unique_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.__device.device_id)},
            name=self.__device.device_name,
            manufacturer=self.__device.manufacturer,
            model=self.__device.model,
            sw_version=self.__device.firmware_version,
        )
        self.__append_unique_id = append_unique_id

    # ---------- GENERAL ---------------

    @property
    def name(self):
        return SELECT_ENTITY_NAMES.get(self.__append_unique_id, self.__append_unique_id)

    @property
    def options(self) -> list[str]:
        return _select_options(
            self.__device.supported_equalizer_presets,
            self.current_option,
        )

    @property
    def icon(self) -> str | None:
        return self.__base_icon

    # ------ STATE FUNCTIONS --------

    @property
    def current_option(self) -> str | None:
        """Get the current status of the select entity from device_status."""
        return self.__device.active_equalizer_preset

    async def async_select_option(self, option: str) -> None:
        """Set the option."""

        await self.__device.set_equalizer_preset(option)


class SoundModeSelectEntity(SelectEntity, RestoreEntity):
    def __init__(
        self,
        device: SoundbarDevice,
        append_unique_id: str,
        icon_string: str,
    ):
        self.entity_description = SelectEntityDescription(
            key=append_unique_id,
        )
        self.__base_icon = icon_string
        self.__device = device
        self._attr_unique_id = f"{device.device_id}_sw_{append_unique_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.__device.device_id)},
            name=self.__device.device_name,
            manufacturer=self.__device.manufacturer,
            model=self.__device.model,
            sw_version=self.__device.firmware_version,
        )
        self.__append_unique_id = append_unique_id

    # ---------- GENERAL ---------------

    @property
    def name(self):
        return SELECT_ENTITY_NAMES.get(self.__append_unique_id, self.__append_unique_id)

    @property
    def options(self) -> list[str]:
        return _select_options(
            self.__device.supported_soundmodes,
            self.current_option,
        )

    @property
    def icon(self) -> str | None:
        return self.__base_icon

    # ------ STATE FUNCTIONS --------

    async def async_added_to_hass(self) -> None:
        """Restore the last selected sound mode before live readback is available."""
        if (last_state := await self.async_get_last_state()) is None:
            return
        if last_state.state in _INVALID_RESTORED_STATES:
            return
        self.__device.remember_sound_mode(last_state.state)

    @property
    def current_option(self) -> str | None:
        """Get the current status of the select entity from device_status."""
        return self.__device.sound_mode

    async def async_select_option(self, option: str) -> None:
        """Set the option."""

        await self.__device.select_sound_mode(option)
        self.async_write_ha_state()


class InputSelectEntity(SelectEntity, RestoreEntity):
    def __init__(
        self,
        device: SoundbarDevice,
        append_unique_id: str,
        icon_string: str,
    ):
        self.entity_description = SelectEntityDescription(
            key=append_unique_id,
        )
        self.__base_icon = icon_string
        self.__device = device
        self._attr_unique_id = f"{device.device_id}_sw_{append_unique_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.__device.device_id)},
            name=self.__device.device_name,
            manufacturer=self.__device.manufacturer,
            model=self.__device.model,
            sw_version=self.__device.firmware_version,
        )
        self.__append_unique_id = append_unique_id

    # ---------- GENERAL ---------------

    @property
    def name(self):
        return SELECT_ENTITY_NAMES.get(self.__append_unique_id, self.__append_unique_id)

    @property
    def options(self) -> list[str]:
        return _select_options(
            self.__device.supported_input_sources,
            self.current_option,
        )

    @property
    def icon(self) -> str | None:
        return self.__base_icon

    # ------ STATE FUNCTIONS --------

    async def async_added_to_hass(self) -> None:
        """Restore the last selected input before live readback is available."""
        if (last_state := await self.async_get_last_state()) is None:
            return
        if last_state.state in _INVALID_RESTORED_STATES:
            return
        self.__device.remember_input_source(last_state.state)

    @property
    def current_option(self) -> str | None:
        """Get the current status of the select entity from device_status."""
        return self.__device.input_source

    async def async_select_option(self, option: str) -> None:
        """Set the option."""

        await self.__device.select_source(option)
        self.async_write_ha_state()
