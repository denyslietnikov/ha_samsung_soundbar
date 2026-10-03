"""Current HA APIs, reload ownership and per-entry runtime isolation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.config_entry_oauth2_flow import (
    ImplementationUnavailableError,
)
from homeassistant.setup import async_setup_component
from pysmartthings import Status
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsung_soundbar.auth import SmartThingsAuthProvider
from custom_components.samsung_soundbar.config_flow import SamsungSoundbarConfigFlow
from custom_components.samsung_soundbar.const import (
    CONF_ENTRY_DEVICE_ID,
    CONF_ENTRY_DEVICE_NAME,
    CONTROL_MODE_SMARTTHINGS_CLOUD,
    DOMAIN,
)
from custom_components.samsung_soundbar.models import SoundbarRuntimeData
from custom_components.samsung_soundbar.services import ENTITY_SERVICES

from .conftest import LOCAL_STATUS
from .test_cloud_repairs import cloud_mode
from .test_lifecycle import registered


@pytest.mark.parametrize("unchanged", [False, True])
async def test_successful_reauth_reloads_once_and_restores_cloud(
    hass, entry, transports, unchanged
):
    cloud_mode(hass, entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    old_runtime = entry.runtime_data
    before = registered(hass, entry)
    # Reauth must reset the stopped Cloud polling, not merely replace a token.
    old_runtime.coordinator._reauth_started = True
    data = {
        "token": dict(entry.data["token"]),
        CONF_ENTRY_DEVICE_ID: entry.data[CONF_ENTRY_DEVICE_ID],
        CONF_ENTRY_DEVICE_NAME: entry.data[CONF_ENTRY_DEVICE_NAME],
        "location_id": entry.data["location_id"],
    }
    if not unchanged:
        data["token"]["access_token"] = "new-token"
    assert (data == entry.data) is unchanged
    flow = SamsungSoundbarConfigFlow()
    flow.hass = hass
    flow.context = {"source": "reauth", "entry_id": entry.entry_id}
    transports.api.get_devices = AsyncMock(
        return_value=[
            SimpleNamespace(
                device_id=entry.data[CONF_ENTRY_DEVICE_ID],
                label=entry.title,
                location_id="test-location",
            )
        ]
    )
    with (
        patch(
            "custom_components.samsung_soundbar.config_flow.pysmartthings.SmartThings",
            return_value=transports.api,
        ),
        patch.object(
            hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
        ) as reload,
        patch(
            "homeassistant.config_entries.report_usage",
            side_effect=AssertionError("Deprecated config-flow API"),
        ),
    ):
        result = await flow.async_oauth_create_entry(data)
        assert result["reason"] == "reauth_successful"
        await hass.async_block_till_done()
        assert reload.await_count == 1
    assert entry.runtime_data is not old_runtime
    assert old_runtime.coordinator._shutdown_requested
    assert not entry.runtime_data.coordinator._reauth_started
    assert entry.runtime_data.device.cloud_available
    assert entry.state is ConfigEntryState.LOADED
    assert registered(hass, entry) == before


async def test_reauth_recovers_failed_setup_without_update_listener(
    hass, entry, transports
):
    cloud_mode(hass, entry)
    transports.auth.side_effect = ConfigEntryAuthFailed("revoked")
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert not entry.update_listeners
    transports.auth.side_effect = None
    flow = SamsungSoundbarConfigFlow()
    flow.hass = hass
    flow.context = {"source": "reauth", "entry_id": entry.entry_id}
    flow._devices = {entry.data[CONF_ENTRY_DEVICE_ID]: entry.title}
    flow._device_locations = {entry.data[CONF_ENTRY_DEVICE_ID]: "test-location"}
    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload:
        result = await flow._async_finish_reauth(
            {"token": {"access_token": "recovered"}}
        )
        await hass.async_block_till_done()
        assert reload.await_count == 1
    assert result["reason"] == "reauth_successful"
    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data, SoundbarRuntimeData)


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


@pytest.mark.parametrize(
    "error,expected",
    [
        (
            ImplementationUnavailableError("temporarily unavailable"),
            ConfigEntryNotReady,
        ),
        (ValueError("unknown implementation"), ConfigEntryAuthFailed),
    ],
)
async def test_oauth_implementation_errors_keep_retry_or_reauth_semantics(
    hass, entry, error, expected
):
    cloud_mode(hass, entry)
    api = MagicMock()
    provider = SmartThingsAuthProvider(hass, entry, None, api)
    with (
        patch(
            "custom_components.samsung_soundbar.auth.async_get_config_entry_implementation",
            new=AsyncMock(side_effect=error),
        ),
        pytest.raises(expected) as caught,
    ):
        await provider.async_get_access_token()
    if expected is ConfigEntryNotReady:
        assert not isinstance(caught.value, ConfigEntryAuthFailed)
    api.authenticate.assert_not_called()


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


async def test_subscription_id_rotation_does_not_reload_runtime(
    hass, entry, transports
):
    cloud_mode(hass, entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "subscription_id": "rotated"}
        )
        await hass.async_block_till_done()
        reload.assert_not_awaited()
    assert entry.runtime_data is runtime


async def test_cloud_clients_and_auth_providers_are_entry_scoped(
    hass, entry, transports
):
    cloud_mode(hass, entry, CONTROL_MODE_SMARTTHINGS_CLOUD)
    api2 = MagicMock()
    api2.get_device = AsyncMock(
        return_value=SimpleNamespace(
            device_id="other-cloud-id",
            location_id="other-location",
            room_id=None,
        )
    )
    api2.get_device_status = AsyncMock(
        return_value={
            "main": {
                "switch": {"switch": Status(value="on")},
                "audioVolume": {"volume": Status(value=22)},
                "audioMute": {"mute": Status(value="unmuted")},
            }
        }
    )
    provider2 = SimpleNamespace(
        api=api2, async_get_access_token=AsyncMock(return_value="other-token")
    )
    transports.auth.side_effect = [transports.provider, provider2]
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    other = MockConfigEntry(
        domain=DOMAIN,
        title="Other cloud soundbar",
        unique_id="other-cloud-id",
        version=1,
        data={
            CONF_ENTRY_DEVICE_ID: "other-cloud-id",
            CONF_ENTRY_DEVICE_NAME: "Other cloud soundbar",
            "location_id": "other-location",
            "token": {"access_token": "other-token"},
        },
        options=dict(entry.options),
    )
    other.add_to_hass(hass)
    assert await hass.config_entries.async_setup(other.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.api is transports.api
    assert other.runtime_data.api is api2
    assert entry.runtime_data.auth_provider is transports.provider
    assert other.runtime_data.auth_provider is provider2
    assert entry.runtime_data.device.volume_level == 0.08
    assert other.runtime_data.device.volume_level == 0.22
    assert await hass.config_entries.async_unload(other.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.api is transports.api
    assert not entry.runtime_data.coordinator._shutdown_requested
    transports.factory.assert_not_called()
