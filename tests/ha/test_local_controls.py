"""Local control services, relative safety and partial field TTL on real HA."""

from unittest.mock import AsyncMock

from custom_components.samsung_soundbar.local_rpc import (
    LocalRpcMethodNotFoundError,
)

from .test_lifecycle import registered


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
