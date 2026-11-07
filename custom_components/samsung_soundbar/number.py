import logging

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api_extension.SoundbarDevice import SoundbarDevice
from .device_info import build_device_info
from .entity_updates import register_device_update_listener
from .models import SoundbarConfigEntry
from .ocf_profiles import Q800F_TONE

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: SoundbarConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    device = config_entry.runtime_data.device
    added = False
    tone_added = set()

    def discover():
        nonlocal added
        entities = []
        if not added and device.ocf_woofer_profile is not None:
            added = True
            entities.append(SoundbarWooferNumberEntity(device, "woofer_level"))
        for field, profile in Q800F_TONE.items():
            if field not in tone_added and device.ocf_tone_profile(field) is not None:
                tone_added.add(field)
                entities.append(SoundbarToneNumberEntity(device, profile))
        if entities:
            register_device_update_listener(config_entry, device, entities)
            async_add_entities(entities)

    discover()
    if not added and not tone_added:
        async_add_entities([])
    config_entry.async_on_unload(device.add_update_listener(discover))


class SoundbarToneNumberEntity(NumberEntity):
    """A model-verified, independently discovered local Bass or Treble level."""

    _attr_has_entity_name = True

    def __init__(self, device: SoundbarDevice, profile):
        self._device = device
        self._soundbar_tone_field = profile.field
        self.entity_description = NumberEntityDescription(
            key=profile.field,
            translation_key=profile.field,
            native_min_value=profile.minimum,
            native_max_value=profile.maximum,
            native_step=profile.step,
            mode=NumberMode.SLIDER,
            icon="mdi:tune-vertical",
        )
        self._attr_unique_id = f"{device.device_id}_num_{profile.field}"
        self._attr_device_info = build_device_info(device)

    @property
    def native_value(self) -> float | None:
        return self._device.tone_level(self._soundbar_tone_field)

    async def async_set_native_value(self, value: float) -> None:
        await self._device.set_tone_level(self._soundbar_tone_field, value)


class SoundbarWooferNumberEntity(NumberEntity):
    _attr_has_entity_name = True
    _soundbar_woofer_absolute = True

    def __init__(
        self,
        device: SoundbarDevice,
        append_unique_id: str,
    ):
        self.entity_description = NumberEntityDescription(
            native_max_value=6,
            native_min_value=-12,
            mode=NumberMode.SLIDER,
            native_step=1,
            native_unit_of_measurement="dB",
            key=append_unique_id,
            translation_key="woofer_level",
        )
        self.__device = device
        self._attr_unique_id = f"{device.device_id}_sw_{append_unique_id}"
        self._attr_device_info = build_device_info(self.__device)
        self.__append_unique_id = append_unique_id

    # ---------- GENERAL ---------------

    # ------ STATE FUNCTIONS --------

    @property
    def native_value(self) -> float | None:
        return self.__device.woofer_level

    async def async_set_native_value(self, value: float):
        await self.__device.set_woofer(value)
