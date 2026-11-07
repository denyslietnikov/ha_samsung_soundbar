"""Local control services, relative safety and partial field TTL on real HA."""

from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.samsung_soundbar.const import (
    CONF_CONTROL_MODE,
    CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
    CONTROL_MODE_SMARTTHINGS_CLOUD,
)
from custom_components.samsung_soundbar.local_rpc import (
    LocalRpcMethodNotFoundError,
    LocalRpcParseError,
    LocalRpcTransportError,
)

from .test_lifecycle import registered


async def test_woofer_services_send_single_keys_and_keep_other_state(
    hass, entry, transports
):
    transports.rpc.remote_key = AsyncMock()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = registered(hass, entry)
    for suffix, key in (("plus", "WOOFER_PLUS"), ("minus", "WOOFER_MINUS")):
        entity_id = registry[f"existing-cloud-id_btn_woofer_{suffix}"][0]
        assert hass.states.get(entity_id).state != "unavailable"
        await hass.services.async_call(
            "button", "press", {"entity_id": entity_id}, blocking=True
        )
        transports.rpc.remote_key.assert_awaited_with(key)
    assert transports.rpc.remote_key.await_count == 2
    transports.auth.assert_not_awaited()


async def test_woofer_error_does_not_repeat_or_use_cloud(hass, entry, transports):
    transports.rpc.remote_key = AsyncMock(
        side_effect=LocalRpcParseError("Parse error", code=-32700)
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = registered(hass, entry)["existing-cloud-id_btn_woofer_plus"][0]
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "button", "press", {"entity_id": entity_id}, blocking=True
        )
    transports.rpc.remote_key.assert_awaited_once_with("WOOFER_PLUS")
    transports.api.execute_device_command.assert_not_called()


async def test_woofer_requires_lan_even_when_hybrid_cloud_is_healthy(
    hass, entry, transports
):
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_CONTROL_MODE: CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
        },
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    transports.rpc.status.side_effect = LocalRpcTransportError("offline")
    await entry.runtime_data.coordinator.async_refresh_all()
    await hass.async_block_till_done()
    assert entry.runtime_data.coordinator.state.snapshot().cloud_available
    for unique_id, (entity_id, _) in registered(hass, entry).items():
        if "_btn_woofer_" in unique_id:
            assert hass.states.get(entity_id).state == "unavailable"


async def test_woofer_transport_failure_immediately_disables_buttons(
    hass, entry, transports
):
    transports.rpc.remote_key = AsyncMock(side_effect=LocalRpcTransportError("offline"))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = registered(hass, entry)["existing-cloud-id_btn_woofer_plus"][0]
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "button", "press", {"entity_id": entity_id}, blocking=True
        )
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unavailable"
    assert entry.runtime_data.device.local_last_error == "offline"


async def test_cloud_only_does_not_create_local_woofer_buttons(hass, entry, transports):
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "token": {"access_token": "test-token"}}
    )
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_CONTROL_MODE: CONTROL_MODE_SMARTTHINGS_CLOUD,
        },
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert not any("_btn_woofer_" in key for key in registered(hass, entry))


async def test_late_q800f_model_adds_buttons_without_reload(hass, entry, transports):
    original = dict(transports.rpc.status.return_value)
    transports.rpc.status.return_value = {**original, "identifier": None}
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert not any("_btn_woofer_" in key for key in registered(hass, entry))
    transports.rpc.status.return_value = original
    await entry.runtime_data.coordinator.async_refresh_all()
    await hass.async_block_till_done()
    assert len([key for key in registered(hass, entry) if "_btn_woofer_" in key]) == 2


async def test_failed_fast_getter_does_not_refresh_its_old_ttl(hass, entry, transports):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    device = entry.runtime_data.device
    state = device.coordinator.state
    old = state.records["rpc"]["input_source"]
    transports.rpc.input_source.side_effect = LocalRpcMethodNotFoundError(
        "unsupported", code=-32601
    )
    transports.rpc.volume.return_value = 9
    await device.update_local_input_source(min_age=None)
    assert device.available
    assert device.volume_level == 0.09
    assert state.records["rpc"]["input_source"] == old
    now = state.clock()
    state.clock = lambda: now + 125
    assert state.value("input_source") is None
    state.clock = lambda: now


async def test_mute_service_does_not_skip_matching_cached_target(
    hass, entry, transports
):
    transports.rpc.set_mute = AsyncMock()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = next(
        entity
        for entity, _ in registered(hass, entry).values()
        if entity.startswith("media_player.")
    )
    await hass.services.async_call(
        "media_player",
        "volume_mute",
        {
            "entity_id": entity_id,
            "is_volume_muted": False,
        },
        blocking=True,
    )
    transports.rpc.set_mute.assert_awaited_once_with(False)
