"""Narrow presentation migrations scoped to the owning soundbar entry."""

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import CONF_ENTRY_DEVICE_ID, DOMAIN
from .models import SoundbarConfigEntry


@callback
def async_remove_woofer_buttons(
    hass: HomeAssistant, entry: SoundbarConfigEntry
) -> None:
    device_id = entry.data.get(CONF_ENTRY_DEVICE_ID)
    if not device_id:
        return
    unique_ids = {f"{device_id}_btn_woofer_plus", f"{device_id}_btn_woofer_minus"}
    registry = er.async_get(hass)
    for item in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            item.platform == DOMAIN
            and item.entity_id.startswith("button.")
            and item.unique_id in unique_ids
        ):
            registry.async_remove(item.entity_id)


@callback
def async_sync_eq_entity(
    hass: HomeAssistant, entry: SoundbarConfigEntry, enabled: bool
) -> None:
    device_id = entry.data.get(CONF_ENTRY_DEVICE_ID)
    if not device_id:
        return
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "select", DOMAIN, f"{device_id}_sw_eq_preset"
    )
    if entity_id is None:
        return
    item = registry.async_get(entity_id)
    if item is None or item.config_entry_id != entry.entry_id:
        return
    if not enabled and item.disabled_by is None:
        registry.async_update_entity(
            entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION
        )
    elif enabled and item.disabled_by is er.RegistryEntryDisabler.INTEGRATION:
        registry.async_update_entity(entity_id, disabled_by=None)
