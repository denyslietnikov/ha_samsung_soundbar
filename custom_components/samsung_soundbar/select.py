import datetime
import logging

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .api_extension.SoundbarDevice import SoundbarDevice
from .device_info import build_device_info
from .entity_migrations import async_sync_eq_entity
from .entity_updates import register_device_update_listener
from .models import SoundbarConfigEntry

_LOGGER = logging.getLogger(__name__)
_INVALID_RESTORED_STATES = {STATE_UNAVAILABLE, STATE_UNKNOWN, None}
SCAN_INTERVAL = datetime.timedelta(seconds=2)


def _select_options(options: list[str] | None, current: str | None) -> list[str]:
    """Return select options with the current value included."""
    values = list(options or [])
    if current and current not in values:
        values.append(current)
    return values


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: SoundbarConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    device = config_entry.runtime_data.device
    async_sync_eq_entity(hass, config_entry)
    added = set()

    def discover():
        entities = []
        if "sound_mode" not in added and device.can_select_sound_mode:
            added.add("sound_mode")
            entities.append(
                SoundModeSelectEntity(device, "sound_mode_preset", "mdi:surround-sound")
            )
        if "input" not in added and device.can_select_source:
            added.add("input")
            entities.append(
                InputSelectEntity(device, "input_preset", "mdi:video-input-hdmi")
            )
        if entities:
            register_device_update_listener(config_entry, device, entities)
            async_add_entities(entities)

    discover()
    if not added:
        async_add_entities([])
    config_entry.async_on_unload(device.add_update_listener(discover))


class SoundModeSelectEntity(SelectEntity, RestoreEntity):
    _soundbar_local_only = True
    _attr_has_entity_name = True

    def __init__(
        self,
        device: SoundbarDevice,
        append_unique_id: str,
        icon_string: str,
    ):
        self.entity_description = SelectEntityDescription(
            key=append_unique_id,
            translation_key="sound_mode_preset",
        )
        self.__base_icon = icon_string
        self.__device = device
        self._attr_unique_id = f"{device.device_id}_sw_{append_unique_id}"
        self._attr_device_info = build_device_info(self.__device)
        self.__append_unique_id = append_unique_id

    # ---------- GENERAL ---------------

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

    async def async_update(self) -> None:
        """Refresh local sound mode before publishing the select state."""
        if self.__device.coordinator is not None:
            await self.__device.coordinator.async_request_refresh()
        elif self.__device.local_rpc_enabled:
            await self.__device.update_local_input_source()

    async def async_select_option(self, option: str) -> None:
        """Set the option."""

        await self.__device.select_sound_mode(option)
        self.async_write_ha_state()


class InputSelectEntity(SelectEntity, RestoreEntity):
    _soundbar_local_only = True
    _attr_has_entity_name = True

    def __init__(
        self,
        device: SoundbarDevice,
        append_unique_id: str,
        icon_string: str,
    ):
        self.entity_description = SelectEntityDescription(
            key=append_unique_id,
            translation_key="input_preset",
        )
        self.__base_icon = icon_string
        self.__device = device
        self._attr_unique_id = f"{device.device_id}_sw_{append_unique_id}"
        self._attr_device_info = build_device_info(self.__device)
        self.__append_unique_id = append_unique_id

    # ---------- GENERAL ---------------

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

    async def async_update(self) -> None:
        """Refresh the local input source."""
        if self.__device.coordinator is not None:
            await self.__device.coordinator.async_request_refresh()
            return
        elif self.__device.local_rpc_enabled:
            await self.__device.update_local_input_source()
            return
        await self.__device.update()

    async def async_select_option(self, option: str) -> None:
        """Set the option."""

        await self.__device.select_source(option)
        self.async_write_ha_state()
