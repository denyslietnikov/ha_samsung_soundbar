"""Regression tests for starting an existing entry without SmartThings Cloud."""

from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.exceptions import HomeAssistantError

import custom_components.samsung_soundbar as integration
from custom_components.samsung_soundbar import (
    image as image_platform,
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
        self.services = patch.object(integration, "_async_register_services")
        for patcher in (
            self.cloud_auth,
            self.cloud_subscription,
            self.local_client,
            self.client_session,
            self.device_registry,
            self.services,
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

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
            "image": image_platform,
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
                if name == "sensor":
                    self.assertEqual(entities[1].native_value, "External Device")

    async def test_local_command_failure_cannot_use_cloud(self) -> None:
        await async_setup_entry(self.hass, self.entry)
        device = self.hass.data[DOMAIN].devices["existing-smartthings-id"].device
        self.rpc.power_on = AsyncMock(side_effect=LocalRpcError("offline"))

        with self.assertRaises(HomeAssistantError):
            await device.switch_on()
        with self.assertRaises(HomeAssistantError):
            integration._require_cloud_device(device)
        self.assertEqual(self.session.post.await_count, 0)
