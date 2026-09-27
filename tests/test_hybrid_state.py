"""Regression tests for shared Hybrid local and cloud readback."""

import asyncio
import datetime
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.exceptions import ConfigEntryNotReady

import custom_components.samsung_soundbar as integration
from custom_components.samsung_soundbar.api_extension.SoundbarDevice import SoundbarDevice
from custom_components.samsung_soundbar.const import (
    CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
    CONTROL_MODE_SMARTTHINGS_CLOUD,
)
from custom_components.samsung_soundbar.entity_updates import register_device_update_listener
from custom_components.samsung_soundbar.local_device import LocalDevice
from custom_components.samsung_soundbar.local_rpc import LocalRpcError
from custom_components.samsung_soundbar.switch import SoundbarSwitchAdvancedAudio


class TestHybridState(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.rpc = MagicMock()
        self.rpc.power_state = AsyncMock(return_value="powerOn")
        self.rpc.input_source = AsyncMock(return_value="E_ARC")
        self.rpc.volume = AsyncMock(return_value=8)
        self.rpc.is_muted = AsyncMock(return_value=False)
        self.rpc.sound_mode = AsyncMock(return_value="STANDARD")
        cloud = LocalDevice("soundbar-id")
        cloud.status.volume = 24
        cloud.status.input_source = "D.IN"
        cloud.status.sound_from_detail_name = "AirPlay"
        cloud.status._attributes = {}
        cloud.status.refresh = AsyncMock()
        self.device = SoundbarDevice(
            cloud,
            session=MagicMock(),
            max_volume=100,
            device_name="Soundbar Q800F",
            control_mode=CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
            local_rpc=self.rpc,
        )

    async def test_one_local_poll_publishes_all_changed_fields(self) -> None:
        listener = MagicMock()
        self.device.add_update_listener(listener)
        await self.device.update_local_input_source(min_age=None)
        self.assertEqual(self.device.volume_level, 0.08)
        self.assertEqual(self.device.input_source, "TV ARC/eARC")
        self.assertEqual(self.device.sound_mode, "Standard")
        self.assertEqual(self.device.sound_from_detail_name, "External Device")
        listener.assert_called_once()

        self.rpc.volume.return_value = 9
        self.rpc.is_muted.return_value = True
        self.rpc.input_source.return_value = "WIFI_AIRPLAY"
        await self.device.update_local_input_source(min_age=None)
        self.assertEqual(self.device.volume_level, 0.09)
        self.assertTrue(self.device.volume_muted)
        self.assertEqual(self.device.input_source, "WIFI")
        self.assertEqual(self.device.sound_from_detail_name, "AirPlay")
        self.assertEqual(listener.call_count, 2)

        self.rpc.input_source.return_value = "E_ARC"
        await self.device.update_local_input_source(min_age=None)
        self.assertEqual(self.device.sound_from_detail_name, "External Device")
        self.assertEqual(listener.call_count, 3)
        await self.device.update_local_input_source(min_age=None)
        self.assertEqual(listener.call_count, 3)

    async def test_external_sound_mode_is_confirmed_without_ha_write(self) -> None:
        await self.device.update_local_input_source(min_age=None)
        listener = MagicMock()
        self.device.add_update_listener(listener)
        self.rpc.sound_mode.return_value = "GAME"
        await self.device.update_local_input_source(min_age=None)
        self.assertEqual(self.device.sound_mode, "Standard")
        listener.assert_not_called()
        await self.device.update_local_input_source(min_age=None)
        self.assertEqual(self.device.sound_mode, "Game Pro")
        listener.assert_called_once()

    async def test_partial_local_readback_and_short_outage_keep_cache(self) -> None:
        await self.device.update_local_input_source(min_age=None)
        self.rpc.volume.return_value = None
        self.rpc.input_source.return_value = None
        await self.device.update_local_input_source(min_age=None)
        self.assertEqual(self.device.volume_level, 0.08)
        self.assertEqual(self.device.input_source, "TV ARC/eARC")

        self.rpc.power_state.side_effect = LocalRpcError("offline")
        await self.device.update_local_input_source(min_age=None)
        self.assertTrue(self.device.local_available)
        self.assertEqual(self.device.volume_level, 0.08)

        self.device._SoundbarDevice__local_status_updated_at = (
            datetime.datetime.now() - datetime.timedelta(minutes=3)
        )
        await self.device.update_local_input_source(min_age=None)
        self.assertFalse(self.device.local_available)
        self.assertEqual(self.device.volume_level, 0.24)

        self.rpc.power_state.side_effect = None
        self.rpc.volume.return_value = 10
        listener = MagicMock()
        self.device.add_update_listener(listener)
        await self.device.update_local_input_source(min_age=None)
        self.assertTrue(self.device.local_available)
        self.assertEqual(self.device.volume_level, 0.10)
        listener.assert_called_once()

    async def test_cloud_refresh_does_not_request_local_state(self) -> None:
        await self.device.update_local_input_source(min_age=None)
        self.device._update_media = AsyncMock()
        listener = MagicMock()
        self.device.add_update_listener(listener)
        self.rpc.power_state.reset_mock()
        await self.device.update_cloud_status()
        self.device.device.status.refresh.assert_awaited_once()
        self.rpc.power_state.assert_not_awaited()
        self.assertEqual(self.device.volume_level, 0.08)
        self.assertEqual(self.device.sound_from_detail_name, "External Device")
        listener.assert_called_once()

    async def test_full_readback_merges_partial_values(self) -> None:
        await self.device.update_local_input_source(min_age=None)
        self.rpc.status = AsyncMock(return_value={"volume": None, "input_source": "WIFI_GOOGLE"})
        await self.device.update_local_status(min_age=None)
        self.assertEqual(self.device.volume_level, 0.08)
        self.assertEqual(self.device.input_source, "WIFI")
        self.assertEqual(self.device.sound_from_detail_name, "Google Cast")

    async def test_entities_share_push_and_advanced_audio_stays_optimistic(self) -> None:
        entry = MagicMock()
        entity = MagicMock()
        entity.hass = object()
        register_device_update_listener(entry, self.device, [entity])
        self.assertFalse(entity._attr_should_poll)
        await self.device.update_local_input_source(min_age=None)
        entity.async_write_ha_state.assert_called_once()

        switch = SoundbarSwitchAdvancedAudio(
            self.device, "Bassmode", lambda: False, AsyncMock(), AsyncMock()
        )
        switch.async_write_ha_state = MagicMock()
        await switch.async_turn_on()
        self.assertTrue(switch.is_on)
        self.device._update_media = AsyncMock()
        await self.device.update_cloud_status()
        self.assertTrue(switch.is_on)

    def test_cloud_only_keeps_entity_polling_and_cloud_sound_from(self) -> None:
        cloud_device = SoundbarDevice(
            self.device.device,
            session=MagicMock(),
            max_volume=100,
            device_name="Soundbar Q800F",
            control_mode=CONTROL_MODE_SMARTTHINGS_CLOUD,
        )
        entity = MagicMock()
        entity._attr_should_poll = True
        register_device_update_listener(MagicMock(), cloud_device, [entity])
        self.assertTrue(entity._attr_should_poll)
        self.assertEqual(cloud_device.sound_from_detail_name, "AirPlay")


class TestHybridPolling(IsolatedAsyncioTestCase):
    async def test_one_timer_per_transport_and_overlapping_ticks_coalesce(self) -> None:
        hass = MagicMock()
        entry = MagicMock()
        device = MagicMock()
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_local(*, min_age):
            started.set()
            await release.wait()

        device.update_local_input_source = AsyncMock(side_effect=slow_local)
        device.update_cloud_status = AsyncMock()
        with patch.object(integration, "async_track_time_interval") as timer:
            integration._async_schedule_local_polling(hass, entry, device)
            integration._async_schedule_hybrid_cloud_polling(hass, entry, device)
        self.assertEqual(timer.call_count, 2)
        self.assertEqual(timer.call_args_list[0].args[2], integration.LOCAL_ONLY_POLL_INTERVAL)
        self.assertEqual(timer.call_args_list[1].args[2], integration.HYBRID_CLOUD_POLL_INTERVAL)
        local_tick = timer.call_args_list[0].args[1]
        first = asyncio.create_task(local_tick(None))
        await started.wait()
        await local_tick(None)
        release.set()
        await first
        device.update_local_input_source.assert_awaited_once_with(min_age=None)
        await timer.call_args_list[1].args[1](None)
        device.update_cloud_status.assert_awaited_once()
        entry.async_on_unload.assert_any_call(timer.return_value)

    async def test_cloud_outage_does_not_start_local_reauth(self) -> None:
        hass = MagicMock()
        entry = MagicMock()
        device = MagicMock()
        device.update_cloud_status = AsyncMock(
            side_effect=ConfigEntryNotReady("offline")
        )
        with patch.object(integration, "async_track_time_interval") as timer:
            integration._async_schedule_hybrid_cloud_polling(hass, entry, device)
        await timer.call_args.args[1](None)
        device.handle_smartthings_availability.assert_called_once_with(False)
        entry.async_start_reauth.assert_not_called()
