import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api_extension.SoundbarDevice import SoundbarDevice
from .device_info import build_device_info
from .entity_updates import register_device_update_listener
from .models import SoundbarConfigEntry
from .ocf_profiles import Q800F_AUDIO_SWITCHES

_LOGGER = logging.getLogger(__name__)

SWITCH_ENTITY_NAMES = {
    profile.suffix: profile.name for profile in Q800F_AUDIO_SWITCHES.values()
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: SoundbarConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    device = config_entry.runtime_data.device
    added = set()

    def discover():
        entities = []
        for field, profile in Q800F_AUDIO_SWITCHES.items():
            if field in added or not device.can_control_audio_switch(field):
                continue
            added.add(field)
            entities.append(
                SoundbarSwitchAdvancedAudio(
                    device,
                    profile.suffix,
                    lambda getter=profile.getter: getattr(device, getter),
                    getattr(device, profile.setter),
                    getattr(device, profile.setter),
                    profile.icon,
                    field=field,
                )
            )
        if entities:
            register_device_update_listener(config_entry, device, entities)
            async_add_entities(entities)

    discover()
    if not added:
        async_add_entities([])
    config_entry.async_on_unload(device.add_update_listener(discover))


class SoundbarSwitchAdvancedAudio(SwitchEntity):
    _soundbar_cloud_only = True
    _soundbar_cloud_capabilities = ("execute",)

    def __init__(
        self,
        device: SoundbarDevice,
        append_unique_id: str,
        state_function,
        on_function,
        off_function,
        icon_string: str = "mdi:toggle-switch-variant",
        *,
        field: str | None = None,
    ):
        self._soundbar_audio_field = field or next(
            (
                item.field
                for item in Q800F_AUDIO_SWITCHES.values()
                if item.suffix == append_unique_id
            ),
            None,
        )
        self.__device = device
        display_name = SWITCH_ENTITY_NAMES.get(append_unique_id, append_unique_id)
        self._name = f"{self.__device.device_name} {display_name}"
        self._attr_unique_id = f"{device.device_id}_sw_{append_unique_id}"
        self.__base_icon = icon_string
        self._attr_device_info = build_device_info(self.__device)

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
    def is_on(self) -> bool | None:
        if (
            self.__device.coordinator is not None
            or self.__device.has_advanced_audio_state
        ):
            value = self.__state_function()
            return None if value is None else bool(value)
        return bool(self.__state)

    async def async_turn_off(self):
        await self.__off_function(False)
        self.__state = False
        self.async_write_ha_state()

    async def async_turn_on(self):
        await self.__on_function(True)
        self.__state = True
        self.async_write_ha_state()
