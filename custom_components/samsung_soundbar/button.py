"""Local relative Woofer controls; no absolute level is inferred."""

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api_extension.SoundbarDevice import SoundbarDevice
from .device_info import build_device_info
from .entity_updates import register_device_update_listener
from .models import SoundbarConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: SoundbarConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    device = config_entry.runtime_data.device
    added = False

    def discover() -> None:
        nonlocal added
        if added or not device.can_control_local_woofer:
            return
        added = True
        entities = [
            SoundbarWooferButton(device, True),
            SoundbarWooferButton(device, False),
        ]
        register_device_update_listener(config_entry, device, entities)
        async_add_entities(entities)

    discover()
    if not added and device.hybrid_mode:
        async_add_entities([])
        config_entry.async_on_unload(device.add_update_listener(discover))
    elif not added:
        async_add_entities([])


class SoundbarWooferButton(ButtonEntity):
    _soundbar_woofer_relative = True
    _attr_has_entity_name = True
    _soundbar_local_only = True
    _soundbar_requires_on = True

    def __init__(self, device: SoundbarDevice, increase: bool) -> None:
        self._device = device
        self._increase = increase
        suffix = "plus" if increase else "minus"
        self._attr_unique_id = f"{device.device_id}_btn_woofer_{suffix}"
        self._attr_name = "Woofer +" if increase else "Woofer -"
        self._attr_icon = "mdi:plus" if increase else "mdi:minus"
        self._attr_device_info = build_device_info(device)

    async def async_press(self) -> None:
        await self._device.adjust_local_woofer(self._increase)
