"""Regression tests for starting an existing entry without SmartThings Cloud."""

import asyncio
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.exceptions import HomeAssistantError

import custom_components.samsung_soundbar as integration
from custom_components.samsung_soundbar import (
    media_player as media_player_platform,
    number as number_platform,
    select as select_platform,
    sensor as sensor_platform,
    switch as switch_platform,
)
from custom_components.samsung_soundbar import (
    async_setup_entry,
    async_unload_entry,
)
from custom_components.samsung_soundbar.const import (
    CONF_CONTROL_MODE,
    CONF_ENTRY_DEVICE_ID,
    CONF_ENTRY_DEVICE_NAME,
    CONF_LOCAL_HOST,
    CONTROL_MODE_LOCAL_ONLY,
    DOMAIN,
)
from custom_components.samsung_soundbar.config_flow import (
    SamsungSoundbarOptionsFlowHandler,
)
from custom_components.samsung_soundbar.entry_options import DEFAULT_ENTRY_OPTIONS
from custom_components.samsung_soundbar.local_rpc import LocalRpcError
from custom_components.samsung_soundbar.coordinator import SoundbarCoordinator, LOCAL_INTERVAL


LOCAL_STATUS = {
    "power": "powerOn",
    "volume": 8,
    "mute": False,
    "input_source": "E_ARC",
    "sound_mode": "GAME",
    "codec": "PCM",
    "identifier": "22_AV_HW-Q800F",
}


class TestLocalOnlyStartup(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.hass = MagicMock()
        self.hass.loop = asyncio.get_event_loop()
        self.hass.is_stopping = False
        self.hass.async_create_task.side_effect = lambda coro, *args, **kwargs: asyncio.create_task(coro)
        self.hass.async_run_hass_job.side_effect = lambda job, **kwargs: asyncio.create_task(job.target())
        self.hass.data = {}
        self.hass.config_entries.async_forward_entry_setups = AsyncMock()
        self.hass.config_entries.async_unload_platforms = AsyncMock(
            return_value=True
        )
        self.hass.services.has_service.return_value = True

        self.entry = MagicMock()
        self.entry.data = {
            CONF_ENTRY_DEVICE_ID: "existing-smartthings-id",
            CONF_ENTRY_DEVICE_NAME: "Soundbar Q800F",
        }
        self.entry.options = {
            CONF_CONTROL_MODE: CONTROL_MODE_LOCAL_ONLY,
            CONF_LOCAL_HOST: "192.0.2.26",
        }
        self.rpc = MagicMock()
        self.rpc.status = AsyncMock(return_value=LOCAL_STATUS)

        self.cloud_auth = patch.object(
            integration.SmartThingsAuthProvider,
            "async_create",
            new=AsyncMock(side_effect=AssertionError("OAuth must not start")),
        )
        self.cloud_subscription = patch.object(
            integration,
            "async_setup_subscription",
            new=AsyncMock(side_effect=AssertionError("Cloud subscription must not start")),
        )
        self.local_client = patch.object(
            integration, "LocalSoundbarRpcClient", return_value=self.rpc
        )
        self.session = MagicMock()
        self.session.get = AsyncMock(
            side_effect=AssertionError("SmartThings HTTP GET must not start")
        )
        self.session.post = AsyncMock(
            side_effect=AssertionError("SmartThings HTTP POST must not start")
        )
        self.client_session = patch.object(
            integration, "async_get_clientsession", return_value=self.session
        )
        self.device_registry = patch.object(
            integration, "async_unmerge_official_smartthings_device"
        )
        self.entity_registry = MagicMock()
        self.entity_registry.async_get_entity_id.return_value = None
        self.entity_registry.async_get.return_value.config_entry_id = self.entry.entry_id
        self.entity_registry_get = patch.object(
            integration.er, "async_get", return_value=self.entity_registry
        )
        self.services = patch.object(integration, "_async_register_services")
        self.timer = patch.object(
            SoundbarCoordinator, "_schedule_refresh"
        )
        for patcher in (
            self.cloud_auth,
            self.cloud_subscription,
            self.local_client,
            self.client_session,
            self.device_registry,
            self.entity_registry_get,
            self.services,
            self.timer,
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.timer_mock = SoundbarCoordinator._schedule_refresh

    async def test_new_tokenless_entry_starts_and_reloads_without_cloud(self) -> None:
        self.entry.data = {
            CONF_ENTRY_DEVICE_ID: "local:wifi_mac:94:e6:ba:89:bd:ba",
            CONF_ENTRY_DEVICE_NAME: "Soundbar Q800F",
            CONF_CONTROL_MODE: CONTROL_MODE_LOCAL_ONLY,
            CONF_LOCAL_HOST: "192.0.2.26",
            "local_identity": {"wifi_mac": "94:e6:ba:89:bd:ba"},
        }
        self.entry.options = {}
        self.assertTrue(await async_setup_entry(self.hass, self.entry))
        device = self.hass.data[DOMAIN].devices[
            "local:wifi_mac:94:e6:ba:89:bd:ba"
        ].device
        self.assertEqual(device.sound_mode, "Game Pro")
        self.assertTrue(await async_unload_entry(self.hass, self.entry))
        self.assertTrue(await async_setup_entry(self.hass, self.entry))
        self.assertEqual(self.rpc.status.await_count, 2)
        self.assertEqual(self.session.get.await_count, 0)
        self.assertEqual(self.session.post.await_count, 0)

    async def test_setup_and_reload_do_not_require_oauth_or_cloud(self) -> None:
        self.assertTrue(await async_setup_entry(self.hass, self.entry))
        device = self.hass.data[DOMAIN].devices["existing-smartthings-id"].device
        self.assertEqual(device.device_id, "existing-smartthings-id")
        self.assertTrue(device.available)
        self.assertEqual(device.sound_mode, "Game Pro")
        self.assertEqual(device.volume_level, 0.08)
        self.assertTrue(await async_unload_entry(self.hass, self.entry))

        self.assertTrue(await async_setup_entry(self.hass, self.entry))
        self.assertEqual(self.rpc.status.await_count, 2)
        self.assertEqual(self.session.get.await_count, 0)
        self.assertEqual(self.session.post.await_count, 0)
        self.assertEqual(device.coordinator.update_interval, LOCAL_INTERVAL)
        self.assertNotIn("image", integration.PLATFORMS)

    async def test_removes_only_legacy_artwork_entity(self) -> None:
        self.entity_registry.async_get_entity_id.return_value = "image.soundbar_artwork"

        await async_setup_entry(self.hass, self.entry)

        self.entity_registry.async_get_entity_id.assert_called_once_with(
            "image", DOMAIN, "existing-smartthings-id_sw_Image URL"
        )
        self.entity_registry.async_remove.assert_called_once_with(
            "image.soundbar_artwork"
        )

    async def test_does_not_remove_artwork_from_another_entry(self) -> None:
        self.entity_registry.async_get_entity_id.return_value = "image.soundbar_artwork"
        self.entity_registry.async_get.return_value.config_entry_id = "other-entry"

        await async_setup_entry(self.hass, self.entry)

        self.entity_registry.async_remove.assert_not_called()

    async def test_local_failure_recovers_without_cloud_fallback(self) -> None:
        self.rpc.status.side_effect = [LocalRpcError("offline"), LOCAL_STATUS]
        self.assertTrue(await async_setup_entry(self.hass, self.entry))
        device = self.hass.data[DOMAIN].devices["existing-smartthings-id"].device
        self.assertFalse(device.available)
        listener = MagicMock()
        device.add_update_listener(listener)

        await device.update()
        self.assertTrue(device.available)
        self.assertEqual(device.input_source, "TV ARC/eARC")
        listener.assert_called_once()
        self.assertEqual(self.session.get.await_count, 0)
        self.assertEqual(self.session.post.await_count, 0)

    async def test_lan_loss_marks_fresh_local_only_state_unavailable_and_recovers(self) -> None:
        await async_setup_entry(self.hass, self.entry)
        device = self.hass.data[DOMAIN].devices["existing-smartthings-id"].device
        listener = MagicMock()
        device.add_update_listener(listener)
        self.rpc.status.side_effect = LocalRpcError("offline")

        await device.update_local_status(min_age=None)
        self.assertFalse(device.available)
        listener.assert_called_once()
        await device.update_local_status(min_age=None)
        listener.assert_called_once()
        self.rpc.status.side_effect = None
        await device.update_local_status(min_age=None)
        self.assertTrue(device.available)
        self.assertEqual(device.volume_level, 0.08)
        self.assertEqual(listener.call_count, 2)
        self.assertEqual(self.session.get.await_count, 0)
        self.assertEqual(self.session.post.await_count, 0)

    async def test_fast_poll_outage_recovers_without_oauth(self) -> None:
        await async_setup_entry(self.hass, self.entry)
        device = self.hass.data[DOMAIN].devices["existing-smartthings-id"].device
        self.rpc.power_state = AsyncMock(side_effect=LocalRpcError("offline"))
        self.rpc.input_source = AsyncMock(return_value="E_ARC")
        self.rpc.volume = AsyncMock(return_value=8)
        self.rpc.is_muted = AsyncMock(return_value=False)
        self.rpc.sound_mode = AsyncMock(return_value="GAME")
        await device.update_local_input_source(min_age=None)
        self.assertFalse(device.available)
        self.rpc.power_state.side_effect = None
        self.rpc.power_state.return_value = "powerOn"
        await device.update_local_input_source(min_age=None)
        self.assertTrue(device.available)
        self.assertEqual(device.input_source, "TV ARC/eARC")
        self.assertEqual(self.session.post.await_count, 0)

    async def test_streaming_fixtures_keep_wifi_and_clear_stale_sound_from(self) -> None:
        await async_setup_entry(self.hass, self.entry)
        device = self.hass.data[DOMAIN].devices["existing-smartthings-id"].device
        for source, name in (
            ("WIFI_AIRPLAY", "AirPlay"), ("WIFI_GOOGLE", "Google Cast"),
            ("WIFI_ROON", "Roon"), ("CD", "Roon"),
            ("WIFI_SPOTIFY", "Spotify"), ("WIFI_IDLE", "External Device"),
        ):
            with self.subTest(source=source):
                self.rpc.status.return_value = {**LOCAL_STATUS, "input_source": source}
                await device.update_local_status(min_age=None)
                self.assertEqual(device.input_source, "WIFI")
                self.assertEqual(device.sound_from_detail_name, name)
        self.rpc.status.return_value = {**LOCAL_STATUS, "mute": True}
        await device.update_local_status(min_age=None)
        self.assertTrue(device.volume_muted)
        self.assertEqual(device.input_source, "TV ARC/eARC")
        self.assertEqual(device.sound_from_detail_name, "External Device")
        self.rpc.status.return_value = {**LOCAL_STATUS, "power": "powerOff"}
        await device.update_local_status(min_age=None)
        self.assertEqual(device.state, "off")
        self.assertEqual(self.session.get.await_count, 0)
        self.assertEqual(self.session.post.await_count, 0)

    async def test_expired_stored_oauth_token_is_ignored(self) -> None:
        self.entry.data["token"] = {"access_token": "expired"}
        self.assertTrue(await async_setup_entry(self.hass, self.entry))
        self.assertEqual(self.session.get.await_count, 0)
        self.assertEqual(self.session.post.await_count, 0)

    def test_options_schema_accepts_local_only(self) -> None:
        options = {**DEFAULT_ENTRY_OPTIONS, CONF_CONTROL_MODE: CONTROL_MODE_LOCAL_ONLY}
        validated = SamsungSoundbarOptionsFlowHandler._options_schema(options)(
            options
        )
        self.assertEqual(validated[CONF_CONTROL_MODE], CONTROL_MODE_LOCAL_ONLY)

    async def test_local_only_platforms_do_not_expose_cloud_entities(self) -> None:
        await async_setup_entry(self.hass, self.entry)
        platforms = {
            "media_player": media_player_platform,
            "select": select_platform,
            "sensor": sensor_platform,
            "switch": switch_platform,
            "number": number_platform,
        }
        with (
            patch.object(media_player_platform, "addServices"),
            patch.object(
                media_player_platform,
                "async_get_clientsession",
                return_value=self.session,
            ),
        ):
            for name, platform in platforms.items():
                add_entities = MagicMock()
                await platform.async_setup_entry(self.hass, self.entry, add_entities)
                entities = add_entities.call_args.args[0]
                expected_count = {"media_player": 1, "select": 2, "sensor": 2}
                self.assertEqual(len(entities), expected_count.get(name, 0), name)
                self.assertTrue(all(not entity.should_poll for entity in entities))
                if name == "sensor":
                    self.assertEqual(entities[1].native_value, "External Device")

    async def test_one_local_poll_refreshes_shared_state(self) -> None:
        await async_setup_entry(self.hass, self.entry)
        self.rpc.power_state = AsyncMock(return_value="powerOn")
        self.rpc.input_source = AsyncMock(return_value="WIFI_AIRPLAY")
        self.rpc.volume = AsyncMock(return_value=9)
        self.rpc.is_muted = AsyncMock(return_value=False)
        self.rpc.sound_mode = AsyncMock(return_value="GAME")
        device = self.hass.data[DOMAIN].devices["existing-smartthings-id"].device
        listener = MagicMock()
        device.add_update_listener(listener)

        await device.coordinator.async_refresh()

        self.assertEqual(device.volume_level, 0.09)
        self.assertEqual(device.sound_from_detail_name, "AirPlay")
        player = media_player_platform.SmartThingsSoundbarMediaPlayer(
            device, self.session
        )
        input_select = select_platform.InputSelectEntity(
            device, "input_preset", "mdi:video-input-hdmi"
        )
        mode_select = select_platform.SoundModeSelectEntity(
            device, "sound_mode_preset", "mdi:surround-sound"
        )
        sound_from = sensor_platform.SoundFromSensor(
            device, "sound_from", "mdi:speaker"
        )
        self.assertEqual(player.source, input_select.current_option)
        self.assertEqual(input_select.current_option, "WIFI")
        self.assertEqual(player.sound_mode, mode_select.current_option)
        self.assertEqual(mode_select.current_option, "Game Pro")
        self.assertEqual(sound_from.native_value, "AirPlay")
        listener.assert_called_once()
        self.assertEqual(self.rpc.status.await_count, 1)
        self.assertEqual(self.session.get.await_count, 0)
        self.entry.async_on_unload.assert_any_call(device.coordinator.async_shutdown)

    async def test_overlapping_local_ticks_do_not_duplicate_poll(self) -> None:
        await async_setup_entry(self.hass, self.entry)
        device = self.hass.data[DOMAIN].devices["existing-smartthings-id"].device
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_refresh(*, min_age):
            started.set()
            await release.wait()

        device.update_local_input_source = AsyncMock(side_effect=slow_refresh)
        first = asyncio.create_task(device.coordinator._async_update_data())
        await started.wait()
        second = asyncio.create_task(device.coordinator._async_update_data())
        await asyncio.sleep(0)
        release.set()
        await first
        await second
        device.update_local_input_source.assert_awaited_once_with(min_age=None)

    async def test_local_entities_do_not_start_private_timers(self) -> None:
        await async_setup_entry(self.hass, self.entry)
        device = self.hass.data[DOMAIN].devices["existing-smartthings-id"].device
        player = media_player_platform.SmartThingsSoundbarMediaPlayer(
            device, self.session
        )
        sound_from = sensor_platform.SoundFromSensor(
            device, "sound_from", "mdi:speaker"
        )
        self.assertFalse(hasattr(player, "_async_update_local_input_source"))
        self.assertFalse(hasattr(sound_from, "_async_update_local_input_source"))

    async def test_local_command_failure_cannot_use_cloud(self) -> None:
        await async_setup_entry(self.hass, self.entry)
        device = self.hass.data[DOMAIN].devices["existing-smartthings-id"].device
        self.rpc.power_on = AsyncMock(side_effect=LocalRpcError("offline"))

        with self.assertRaises(HomeAssistantError):
            await device.switch_on()
        with self.assertRaises(HomeAssistantError):
            integration._require_cloud_device(device)
        self.assertEqual(self.session.post.await_count, 0)
