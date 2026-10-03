"""Exercise setup, registry preservation, services and unload on real HA."""

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from custom_components.samsung_soundbar.const import (
    CONF_CONTROL_MODE,
    CONF_LOCAL_HOST,
    CONTROL_MODE_LOCAL_ONLY,
    DOMAIN,
)
from custom_components.samsung_soundbar.local_rpc import LocalRpcError


def registered(hass, entry):
    return {
        item.unique_id: (item.entity_id, item.device_id)
        for item in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }


async def test_local_only_setup_reload_and_unload_preserve_ids(hass, entry, transports):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    before = registered(hass, entry)
    assert len(before) == 5  # media player, two selects, two sensors
    player_id = next(
        entity_id
        for entity_id, _ in before.values()
        if entity_id.startswith("media_player.")
    )
    assert hass.states.get(player_id).attributes["volume_level"] == 0.08
    assert hass.states.get(player_id).attributes["sound_mode"] == "Game Pro"
    assert (
        len(dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)) == 1
    )
    device = hass.data[DOMAIN].devices["existing-cloud-id"].device
    old_coordinator = device.coordinator

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert registered(hass, entry) == before
    assert old_coordinator._shutdown_requested
    assert not old_coordinator._listeners
    transports.auth.assert_not_awaited()
    transports.api.get_device.assert_not_awaited()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert hass.states.get(player_id).state == "unavailable"
    assert registered(hass, entry) == before


async def test_local_lan_failure_and_recovery_update_real_entity_state(
    hass, entry, transports
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    device = hass.data[DOMAIN].devices["existing-cloud-id"].device
    player_id = next(
        item[0]
        for item in registered(hass, entry).values()
        if item[0].startswith("media_player.")
    )
    transports.rpc.status.side_effect = LocalRpcError("offline")
    await device.coordinator.async_refresh_all()
    await hass.async_block_till_done()
    assert hass.states.get(player_id).state == "unavailable"
    transports.rpc.status.side_effect = None
    await device.coordinator.async_refresh_all()
    await hass.async_block_till_done()
    assert hass.states.get(player_id).state != "unavailable"
    assert hass.states.get(player_id).attributes["volume_level"] == 0.08
    transports.auth.assert_not_awaited()


async def test_dhcp_flow_updates_host_and_reloads_same_entities(
    hass, entry, transports
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    before = registered(hass, entry)
    old_coordinator = hass.data[DOMAIN].devices["existing-cloud-id"].device.coordinator
    info = DhcpServiceInfo(
        ip="192.0.2.42", hostname="soundbar", macaddress="94e6ba89bdba"
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "dhcp"}, data=info
    )
    assert result["reason"] == "dhcp_host_updated"
    await hass.async_block_till_done()
    assert entry.options[CONF_LOCAL_HOST] == "192.0.2.42"
    assert registered(hass, entry) == before
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert old_coordinator._shutdown_requested
    assert transports.factory.call_args.args[0] == "192.0.2.42"
    assert entry.state is ConfigEntryState.LOADED
    transports.auth.assert_not_awaited()


async def test_external_local_state_reaches_actual_ha_entities(hass, entry, transports):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    device = hass.data[DOMAIN].devices["existing-cloud-id"].device
    transports.rpc.status.return_value = {
        "power": "powerOn",
        "volume": 13,
        "mute": True,
        "input_source": "WIFI_AIRPLAY",
        "sound_mode": "SURROUND",
    }
    await device.coordinator.async_refresh_all()
    await device.coordinator.async_refresh_all()
    await hass.async_block_till_done()
    player = hass.states.get("media_player.soundbar_q800f")
    assert player.attributes["volume_level"] == 0.13
    assert player.attributes["is_volume_muted"] is True
    assert hass.states.get("select.soundbar_q800f_sound_mode").state == "Surround"
    assert hass.states.get("select.soundbar_q800f_input_preset").state == "WIFI"
    assert hass.states.get("sensor.soundbar_q800f_sound_from").state == "AirPlay"
    transports.auth.assert_not_awaited()


async def test_volume_service_uses_actual_local_readback(hass, entry, transports):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    transports.rpc.volume.return_value = 12
    await hass.services.async_call(
        "media_player",
        "volume_set",
        {"entity_id": "media_player.soundbar_q800f", "volume_level": 0.12},
        blocking=True,
    )
    await hass.async_block_till_done()
    transports.rpc.set_volume.assert_awaited_once_with(12)
    assert (
        hass.states.get("media_player.soundbar_q800f").attributes["volume_level"]
        == 0.12
    )
    transports.auth.assert_not_awaited()


async def test_new_local_only_flow_creates_loaded_entry_without_oauth(hass, transports):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONTROL_MODE: CONTROL_MODE_LOCAL_ONLY}
    )
    assert result["step_id"] == "local"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LOCAL_HOST: "192.0.2.26"}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    new_entry = result["result"]
    assert new_entry.state is ConfigEntryState.LOADED
    assert new_entry.data[CONF_CONTROL_MODE] == CONTROL_MODE_LOCAL_ONLY
    assert "token" not in new_entry.data
    assert len(registered(hass, new_entry)) == 5
    transports.auth.assert_not_awaited()
    transports.api.get_device.assert_not_awaited()
