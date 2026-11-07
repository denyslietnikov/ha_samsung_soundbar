"""Current HA APIs, reload ownership and per-entry runtime isolation."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.exceptions import (
    HomeAssistantError,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsung_soundbar.const import (
    CONF_ENTRY_DEVICE_ID,
    CONF_ENTRY_DEVICE_NAME,
    DOMAIN,
)
from custom_components.samsung_soundbar.services import ENTITY_SERVICES

from .conftest import LOCAL_STATUS


async def test_actions_exist_before_any_config_entry(hass):
    assert await async_setup_component(hass, DOMAIN, {})
    for name, _, _ in ENTITY_SERVICES:
        assert hass.services.has_service(DOMAIN, name)
    assert hass.services.has_service(DOMAIN, "dump_local_rpc")
    with pytest.raises(HomeAssistantError, match="No Samsung Soundbar"):
        await hass.services.async_call(
            DOMAIN, "dump_discovery_snapshot", {}, blocking=True, return_response=True
        )


async def test_entity_actions_work_and_survive_last_unload(hass, entry, transports):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call(
        DOMAIN,
        "select_soundmode",
        {"entity_id": "media_player.soundbar_q800f", "sound_mode": "Surround"},
        blocking=True,
    )
    transports.rpc.set_sound_mode.assert_awaited_once_with("SURROUND")
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "set_woofer_level",
            {"entity_id": "media_player.soundbar_q800f", "level": 20},
            blocking=True,
        )
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not hasattr(entry, "runtime_data")
    for name, _, _ in ENTITY_SERVICES:
        assert hass.services.has_service(DOMAIN, name)
    assert hass.services.has_service(DOMAIN, "dump_discovery_snapshot")


async def test_two_entries_have_independent_runtime_and_registry_service_targets(
    hass, entry, transports
):
    other = MockConfigEntry(
        domain=DOMAIN,
        title="Other soundbar",
        unique_id="other-id",
        version=1,
        data={
            CONF_ENTRY_DEVICE_ID: "other-id",
            CONF_ENTRY_DEVICE_NAME: "Other soundbar",
        },
        options=dict(entry.options),
    )
    rpc2 = MagicMock()
    rpc2.status = AsyncMock(return_value={**LOCAL_STATUS, "volume": 22})
    rpc2.set_sound_mode = AsyncMock()
    rpc2.sound_mode = AsyncMock(return_value="SURROUND")
    transports.factory.side_effect = [transports.rpc, rpc2]
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    other.add_to_hass(hass)
    assert await hass.config_entries.async_setup(other.entry_id)
    await hass.async_block_till_done()
    runtime1, runtime2 = entry.runtime_data, other.runtime_data
    assert runtime1 is not runtime2
    assert runtime1.coordinator is not runtime2.coordinator
    assert runtime1.device.volume_level == 0.08
    assert runtime2.device.volume_level == 0.22
    assert DOMAIN not in hass.data
    with pytest.raises(HomeAssistantError, match="device_id is required"):
        await hass.services.async_call(
            DOMAIN, "dump_discovery_snapshot", {}, blocking=True, return_response=True
        )
    registry = dr.async_get(hass)
    device1 = dr.async_entries_for_config_entry(registry, entry.entry_id)[0]
    device2 = dr.async_entries_for_config_entry(registry, other.entry_id)[0]
    assert device1.id != device2.id
    for device_entry, expected_volume in ((device1, 0.08), (device2, 0.22)):
        result = await hass.services.async_call(
            DOMAIN,
            "dump_discovery_snapshot",
            {"device_id": device_entry.id},
            blocking=True,
            return_response=True,
        )
        assert result["resolved"]["volume"]["level"] == expected_volume
    await hass.services.async_call(
        DOMAIN,
        "select_soundmode",
        {"device_id": device2.id, "sound_mode": "Surround"},
        blocking=True,
    )
    rpc2.set_sound_mode.assert_awaited_once_with("SURROUND")
    transports.rpc.set_sound_mode.assert_not_awaited()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert runtime1.coordinator._shutdown_requested
    assert not runtime2.coordinator._shutdown_requested
    assert other.runtime_data is runtime2
    assert (
        hass.states.get("media_player.other_soundbar").attributes["volume_level"]
        == 0.22
    )
    result = await hass.services.async_call(
        DOMAIN, "dump_discovery_snapshot", {}, blocking=True, return_response=True
    )
    assert result["device_id"] == "other-id"


async def test_scoped_registry_preserves_official_smartthings_device(
    hass, entry, transports
):
    official = MockConfigEntry(domain="smartthings", title="Official", data={})
    official.add_to_hass(hass)
    registry = dr.async_get(hass)
    original = registry.async_get_or_create(
        config_entry_id=official.entry_id,
        identifiers={("smartthings", entry.data[CONF_ENTRY_DEVICE_ID])},
        name="Official Soundbar",
    )
    with patch(
        "homeassistant.helpers.device_registry.report_usage",
        side_effect=AssertionError("Deprecated registry API"),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    own = registry.async_get_device_by_identifier(
        (DOMAIN, entry.data[CONF_ENTRY_DEVICE_ID]), entry.entry_id
    )
    assert own.id != original.id
    assert registry.async_get(original.id) == original
    assert original.config_entry_id == official.entry_id
    assert own.config_entry_id == entry.entry_id


async def test_failed_platform_unload_keeps_runtime(hass, entry, transports):
    from custom_components.samsung_soundbar import async_unload_entry

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    with patch.object(
        hass.config_entries, "async_unload_platforms", new=AsyncMock(return_value=False)
    ):
        assert not await async_unload_entry(hass, entry)
    assert entry.runtime_data is runtime
    assert not runtime.coordinator._shutdown_requested
