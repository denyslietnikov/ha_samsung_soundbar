"""Shared state authority, poll tiers, write settling and lifecycle regressions."""

import asyncio
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError

from custom_components.samsung_soundbar.api_extension.smartthings_compat import (
    SmartThingsStatusCompat,
)
from custom_components.samsung_soundbar.api_extension.SoundbarDevice import (
    SoundbarDevice,
)
from custom_components.samsung_soundbar.const import (
    CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
    CONTROL_MODE_LOCAL_ONLY,
    CONTROL_MODE_SMARTTHINGS_CLOUD,
)
from custom_components.samsung_soundbar.coordinator import SoundbarCoordinator
from custom_components.samsung_soundbar.entity_updates import (
    register_device_update_listener,
)
from custom_components.samsung_soundbar.local_device import LocalDevice
from custom_components.samsung_soundbar.local_rpc import LocalRpcError
from custom_components.samsung_soundbar.sensor import VolumeSensor
from custom_components.samsung_soundbar.state import SoundbarState
from custom_components.samsung_soundbar.switch import SoundbarSwitchAdvancedAudio

LOCAL_STATUS = {
    "power": "powerOn",
    "volume": 8,
    "mute": False,
    "input_source": "E_ARC",
    "sound_mode": "GAME",
    "codec": "PCM",
    "identifier": "22_AV_HW-Q800F",
}


class TestFieldState(TestCase):
    def setUp(self):
        self.now = 1.0
        self.state = SoundbarState(local=True, cloud=True, clock=lambda: self.now)

    def test_priority_partial_merge_and_per_field_age(self):
        self.state.merge(
            "local", {"volume_level": 0.08, "power": "on", "sound_mode": "Game Pro"}
        )
        self.now = 2
        self.state.merge(
            "cloud", {"volume_level": 0.16, "power": "off", "bass_mode": True}
        )
        self.assertEqual(self.state.value("volume_level"), 0.08)
        self.assertEqual(self.state.value("power"), "on")
        self.now = 3
        self.state.merge("local", {"power": "on", "volume_level": None})
        self.assertEqual(self.state.diagnostics()["volume_level"]["age_seconds"], 2)
        self.assertEqual(self.state.diagnostics()["power"]["age_seconds"], 0)
        self.assertTrue(self.state.value("bass_mode"))

    def test_stale_local_field_falls_back_only_to_fresh_cloud(self):
        self.state.merge("local", {"volume_level": 0.08})
        self.now = 122
        self.state.merge("cloud", {"volume_level": 0.09})
        self.assertEqual(self.state.value("volume_level"), 0.09)
        self.now = 183
        self.assertIsNone(self.state.value("volume_level"))

    def test_streaming_field_has_shorter_ttl_than_input(self):
        self.state.merge(
            "local", {"input_source": "WIFI", "sound_from_detail_name": "AirPlay"}
        )
        self.now = 17
        self.assertIsNone(self.state.value("sound_from_detail_name"))
        self.assertEqual(self.state.value("input_source"), "WIFI")

    def test_static_model_identifier_does_not_expire_with_core_status(self):
        self.state.merge("local", {"local_identifier": "22_AV_HW-Q800F", "power": "on"})
        self.now = 200
        self.assertEqual(self.state.value("local_identifier"), "22_AV_HW-Q800F")
        self.assertIsNone(self.state.value("power"))
        self.assertIsNone(self.state.diagnostics()["local_identifier"]["ttl_seconds"])

    def test_write_guard_filters_pre_write_and_stale_readback(self):
        self.state.merge("local", {"sound_mode": "Standard"})
        self.now = 2
        self.state.expect({"sound_mode": "Game Pro"})
        self.state.merge("local", {"sound_mode": "Surround"}, observed_at=1)
        self.assertEqual(self.state.value("sound_mode"), "Game Pro")
        self.state.merge("local", {"sound_mode": "Standard"})
        self.assertEqual(self.state.value("sound_mode"), "Game Pro")
        self.now = 8
        self.assertEqual(self.state.value("sound_mode"), "Standard")

    def test_cloud_cannot_acknowledge_local_write_and_unmask_old_local_state(self):
        self.state.merge("local", {"sound_mode": "Standard"})
        self.now = 2
        self.state.expect({"sound_mode": "Surround"})
        self.state.merge("cloud", {"sound_mode": "Surround"})
        self.assertEqual(self.state.value("sound_mode"), "Surround")
        self.state.merge("local", {"sound_mode": "Surround"})
        self.assertNotIn("sound_mode", self.state.pending)

    def test_write_only_switch_does_not_revert_to_pre_write_state(self):
        self.state.merge("cloud", {"bass_mode": False})
        self.now = 2
        self.state.expect({"bass_mode": True}, write_only=True)
        self.now = 8
        self.assertTrue(self.state.value("bass_mode"))
        self.assertEqual(self.state.diagnostics()["bass_mode"]["source"], "optimistic")
        self.state.merge("cloud", {"bass_mode": False})
        self.assertFalse(self.state.value("bass_mode"))
        self.now = 100
        self.assertIsNone(self.state.value("bass_mode"))

    def test_local_only_never_accepts_cloud_and_outage_is_unavailable(self):
        state = SoundbarState(local=True, cloud=False)
        state.merge("cloud", {"volume_level": 0.16})
        self.assertIsNone(state.value("volume_level"))
        state.merge("local", {"power": "on"})
        state.transport_available["local"] = True
        self.assertTrue(state.snapshot().available)
        state.transport_available["local"] = False
        self.assertFalse(state.snapshot().available)

    def test_older_poll_cannot_overwrite_newer_push(self):
        self.state.merge("cloud", {"volume_level": 0.10}, observed_at=2)
        self.state.merge("cloud", {"volume_level": 0.08}, observed_at=1)
        self.assertEqual(self.state.records["cloud"]["volume_level"].value, 0.10)

    def test_contradictory_read_during_guard_does_not_resurrect_old_optimism(self):
        self.state.expect({"bass_mode": True}, write_only=True)
        self.now = 2
        self.state.merge("cloud", {"bass_mode": False})
        self.assertTrue(self.state.value("bass_mode"))
        self.now = 8
        self.assertFalse(self.state.value("bass_mode"))
        self.now = 100
        self.assertIsNone(self.state.value("bass_mode"))


class TestCoordinator(IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 1.0
        self.hass = MagicMock()
        self.hass.loop = asyncio.get_event_loop()
        self.hass.is_stopping = False
        self.hass.async_create_task.side_effect = lambda coro, *args, **kwargs: (
            asyncio.create_task(coro)
        )
        self.hass.async_run_hass_job.side_effect = lambda job, **kwargs: (
            asyncio.create_task(job.target())
        )
        self.entry = MagicMock()
        self.entry.pref_disable_polling = False
        self.rpc = MagicMock()
        self.rpc.status = AsyncMock(return_value=LOCAL_STATUS)
        self.rpc.power_state = AsyncMock(return_value="powerOn")
        self.rpc.volume = AsyncMock(return_value=8)
        self.rpc.is_muted = AsyncMock(return_value=False)
        self.rpc.input_source = AsyncMock(return_value="E_ARC")
        self.rpc.sound_mode = AsyncMock(return_value="GAME")
        self.rpc.set_sound_mode = AsyncMock()
        self.rpc.set_volume = AsyncMock()
        self.cloud = LocalDevice("soundbar-id")
        self.cloud.status._attributes = {}
        self.cloud.status.switch = False
        self.cloud.status.volume = 16
        self.cloud.status.input_source = "D.IN"
        self.cloud.status.refresh = AsyncMock()
        self.schedule = patch.object(SoundbarCoordinator, "_schedule_refresh")
        self.schedule.start()
        self.addCleanup(self.schedule.stop)

    def make_device(self, mode=CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS):
        device = SoundbarDevice(
            self.cloud,
            MagicMock(),
            100,
            "Q800F",
            control_mode=mode,
            local_rpc=self.rpc if mode != CONTROL_MODE_SMARTTHINGS_CLOUD else None,
            enable_advanced_audio=mode != CONTROL_MODE_LOCAL_ONLY,
        )
        device._update_advanced_audio = AsyncMock()
        coordinator = SoundbarCoordinator(
            self.hass, self.entry, device, clock=lambda: self.now
        )
        self.addAsyncCleanup(coordinator.async_shutdown)
        return device, coordinator

    async def test_poll_tiers_and_no_duplicate_rpc_for_entities(self):
        device, coordinator = self.make_device()
        await coordinator.async_refresh()
        self.assertEqual(device.volume_level, 0.08)
        self.assertEqual(device.state, "on")
        self.assertEqual(device.sound_mode, "Game Pro")
        entities = [MagicMock() for _ in range(20)]
        register_device_update_listener(self.entry, device, entities)
        self.rpc.status.assert_awaited_once()
        self.cloud.status.refresh.assert_awaited_once()
        self.assertTrue(all(not entity._attr_should_poll for entity in entities))
        self.now = 3
        await coordinator.async_refresh()
        self.rpc.status.assert_awaited_once()
        self.rpc.volume.assert_awaited_once()
        self.cloud.status.refresh.assert_awaited_once()
        for entity in entities:
            entity.async_write_ha_state.assert_not_called()
        self.now = 17
        await coordinator.async_refresh()
        self.assertEqual(self.cloud.status.refresh.await_count, 2)
        self.now = 62
        await coordinator.async_refresh()
        self.assertEqual(self.rpc.status.await_count, 2)

    async def test_mode_write_has_targeted_readback_and_shared_guard(self):
        device, coordinator = self.make_device()
        await coordinator.async_refresh()
        self.rpc.sound_mode.return_value = "GAME"
        await device.select_sound_mode("Surround")
        self.rpc.set_sound_mode.assert_awaited_once_with("SURROUND")
        self.rpc.sound_mode.assert_awaited_once()
        self.rpc.volume.assert_not_awaited()
        self.rpc.status.assert_awaited_once()
        self.assertEqual(device.sound_mode, "Surround")
        self.rpc.sound_mode.return_value = "SURROUND"
        self.now = 3
        await coordinator.async_refresh()
        self.now = 5
        await coordinator.async_refresh()
        self.assertEqual(device.sound_mode, "Surround")
        self.assertEqual(
            coordinator.state.diagnostics()["sound_mode"]["source"], "local"
        )

    async def test_successive_commands_and_poll_cannot_interleave(self):
        device, coordinator = self.make_device()
        await coordinator.async_refresh()
        started, release = asyncio.Event(), asyncio.Event()
        writes = []

        async def set_mode(mode):
            writes.append(mode)
            if len(writes) == 1:
                started.set()
                await release.wait()
            self.rpc.sound_mode.return_value = mode

        self.rpc.set_sound_mode.side_effect = set_mode
        first = asyncio.create_task(device.select_sound_mode("Surround"))
        await started.wait()
        second = asyncio.create_task(device.select_sound_mode("Standard"))
        poll = asyncio.create_task(coordinator.async_refresh())
        await asyncio.sleep(0)
        self.assertEqual(writes, ["SURROUND"])
        release.set()
        await asyncio.gather(first, second, poll)
        self.assertEqual(writes, ["SURROUND", "STANDARD"])
        self.assertEqual(device.sound_mode, "Standard")

    async def test_failed_command_does_not_publish_success_and_locks_release(self):
        device, coordinator = self.make_device()
        await coordinator.async_refresh()
        self.rpc.set_volume.side_effect = ValueError("rejected")
        with self.assertRaises(HomeAssistantError):
            await device.set_volume(0.10)
        self.assertEqual(device.volume_level, 0.08)
        self.assertNotIn("volume_level", coordinator.state.pending)
        self.rpc.set_volume.side_effect = None
        self.rpc.volume.return_value = 10
        await asyncio.wait_for(device.set_volume(0.10), 1)
        self.assertEqual(device.volume_level, 0.10)

    async def test_local_only_polling_during_outage_and_recovery_never_calls_cloud(
        self,
    ):
        device, coordinator = self.make_device(CONTROL_MODE_LOCAL_ONLY)
        await coordinator.async_refresh()
        self.rpc.power_state.side_effect = LocalRpcError("offline")
        self.now = 3
        await coordinator.async_refresh()
        self.assertFalse(device.available)
        self.rpc.power_state.side_effect = None
        self.now = 5
        await coordinator.async_refresh()
        self.assertTrue(device.available)
        self.cloud.status.refresh.assert_not_awaited()
        self.entry.async_start_reauth.assert_not_called()

    async def test_cloud_only_uses_same_coordinator_without_local_calls(self):
        device, coordinator = self.make_device(CONTROL_MODE_SMARTTHINGS_CLOUD)
        await coordinator.async_refresh()
        self.assertEqual(device.volume_level, 0.16)
        self.assertEqual(coordinator.update_interval.total_seconds(), 15)
        self.rpc.status.assert_not_awaited()
        entity = MagicMock()
        register_device_update_listener(self.entry, device, [entity])
        self.assertFalse(entity._attr_should_poll)

    async def test_cloud_auth_failure_starts_reauth_once_but_local_polling_continues(
        self,
    ):
        device, coordinator = self.make_device()
        self.cloud.status.refresh.side_effect = ConfigEntryAuthFailed("expired")
        await coordinator.async_refresh()
        self.now = 20
        await coordinator.async_refresh()
        self.assertTrue(device.available)
        self.assertEqual(device.volume_level, 0.08)
        self.entry.async_start_reauth.assert_called_once_with(self.hass)
        self.cloud.status.refresh.assert_awaited_once()
        self.rpc.volume.assert_awaited_once()

    async def test_cloud_switch_shared_optimism_and_transport_availability(self):
        device, coordinator = self.make_device()
        await coordinator.async_refresh()
        device.set_custom_execution_data = AsyncMock()
        switch = SoundbarSwitchAdvancedAudio(
            device,
            "bassmode",
            lambda: device.bass_mode,
            device.set_bass_mode,
            device.set_bass_mode,
        )
        switch.async_write_ha_state = MagicMock()
        register_device_update_listener(self.entry, device, [switch])
        await device.set_bass_mode(True)
        self.now = 10
        self.assertTrue(switch.is_on)
        self.assertEqual(
            coordinator.state.diagnostics()["bass_mode"]["source"], "optimistic"
        )
        device.handle_smartthings_availability(False)
        self.assertTrue(device.available)
        self.assertFalse(switch.available)

    async def test_shutdown_removes_listener_and_cancels_polling(self):
        _device, coordinator = self.make_device()
        await coordinator.async_refresh()
        listener = MagicMock()
        unsubscribe = coordinator.async_add_listener(listener)
        unsubscribe()
        coordinator.receive("local", {"volume_level": 0.09})
        listener.assert_not_called()
        await coordinator.async_shutdown()
        self.assertTrue(coordinator._shutdown_requested)

    async def test_expired_volume_is_unknown_not_zero_or_a_sensor_exception(self):
        device, coordinator = self.make_device(CONTROL_MODE_LOCAL_ONLY)
        await coordinator.async_refresh()
        self.now = 122
        coordinator.transport_changed("local", False)
        sensor = VolumeSensor(device, "volume_level", "mdi:volume-high")
        self.assertIsNone(device.volume_level)
        self.assertIsNone(sensor.native_value)
        self.assertFalse(device.available)
        self.assertTrue(
            coordinator.diagnostics()["fields"]["volume_level"]["readbacks"]["local"][
                "stale"
            ]
        )

    async def test_partial_cloud_payload_does_not_invent_zero_or_refresh_absent_fields(
        self,
    ):
        device, coordinator = self.make_device(CONTROL_MODE_SMARTTHINGS_CLOUD)
        self.cloud.status = SmartThingsStatusCompat(MagicMock(), MagicMock())
        self.cloud.status.refresh = AsyncMock()
        coordinator.receive("cloud", {"power": "on", "volume_level": 0.08})
        coordinator.receive("cloud", {"media_title": "Track", "media_artist": "Artist"})
        self.now = 3
        await coordinator.async_refresh()
        self.assertEqual(device.volume_level, 0.08)
        self.assertEqual(device.state, "on")
        self.assertEqual(device.media_title, "Track")
        self.assertEqual(
            coordinator.diagnostics()["fields"]["media_title"]["age_seconds"], 2
        )
        self.assertEqual(
            coordinator.diagnostics()["fields"]["volume_level"]["age_seconds"], 2
        )

    async def test_input_command_reads_power_and_input_and_clears_stream_label(self):
        device, coordinator = self.make_device()
        self.rpc.status.return_value = {**LOCAL_STATUS, "input_source": "WIFI_AIRPLAY"}
        await coordinator.async_refresh()
        self.rpc.select_input = AsyncMock()
        self.rpc.input_source.return_value = "E_ARC"
        await device.select_source("TV ARC/eARC")
        self.assertEqual(device.input_source, "TV ARC/eARC")
        self.assertEqual(device.sound_from_detail_name, "External Device")
        self.rpc.power_state.assert_awaited_once()
        self.rpc.input_source.assert_awaited_once()
        self.rpc.volume.assert_not_awaited()

    async def test_missing_virtual_readback_does_not_confirm_false(self):
        device, coordinator = self.make_device()
        coordinator.state.expect({"virtual_sound": True}, write_only=True)
        self.now = 10
        device.async_request_execute_payload = AsyncMock(
            return_value={
                "x.com.samsung.networkaudio.nightmode": 0,
                "x.com.samsung.networkaudio.bassboost": 1,
                "x.com.samsung.networkaudio.voiceamplifier": 0,
            }
        )
        await SoundbarDevice._update_advanced_audio(device)
        self.assertTrue(device.virtual_sound)
        self.assertTrue(device.bass_mode)
        self.assertEqual(
            coordinator.diagnostics()["fields"]["virtual_sound"]["source"], "optimistic"
        )

    async def test_cancelled_command_releases_transaction_and_next_command_works(self):
        device, coordinator = self.make_device()
        await coordinator.async_refresh()
        started = asyncio.Event()

        async def blocked(mode):
            started.set()
            await asyncio.Event().wait()

        self.rpc.set_sound_mode.side_effect = blocked
        task = asyncio.create_task(device.select_sound_mode("Surround"))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn("sound_mode", coordinator.state.pending)
        self.rpc.set_sound_mode.side_effect = None
        self.rpc.sound_mode.return_value = "STANDARD"
        await asyncio.wait_for(device.select_sound_mode("Standard"), 1)
        self.assertEqual(device.sound_mode, "Standard")

    async def test_local_only_discovery_snapshot_has_sources_without_cloud_sections(
        self,
    ):
        device, coordinator = self.make_device(CONTROL_MODE_LOCAL_ONLY)
        snapshot = await device.async_dump_discovery_snapshot(include_raw_status=True)
        self.assertFalse(snapshot["cloud_enabled"])
        self.assertEqual(snapshot["resolved"]["volume"]["level"], 0.08)
        self.assertEqual(
            snapshot["coordinator"]["fields"]["volume_level"]["source"], "local"
        )
        self.assertNotIn("execute_status", snapshot)
        self.assertEqual(snapshot["raw_status"], {})
        self.cloud.status.refresh.assert_not_awaited()
        self.assertEqual(coordinator.update_interval.total_seconds(), 2)

    async def test_cloud_event_refreshes_only_its_field_and_rejects_old_events(self):
        device, coordinator = self.make_device(CONTROL_MODE_SMARTTHINGS_CLOUD)
        await coordinator.async_refresh()
        self.cloud.status.apply_event = MagicMock()
        self.now = 3
        self.cloud.status.volume = 10
        event = MagicMock()
        event.attribute.value = "volume"
        event.event_time = None
        await device.async_handle_smartthings_event(event)
        self.assertEqual(device.volume_level, 0.10)
        self.assertEqual(coordinator.state.diagnostics()["power"]["age_seconds"], 2)
        self.cloud.status.volume = 7
        with patch(
            "custom_components.samsung_soundbar.api_extension.SoundbarDevice.time",
            return_value=1000,
        ):
            event.event_time = 990
            await device.async_handle_smartthings_event(event)
        self.assertEqual(device.volume_level, 0.10)

    async def test_local_command_transport_error_marks_local_unavailable(self):
        device, coordinator = self.make_device(CONTROL_MODE_LOCAL_ONLY)
        await coordinator.async_refresh()
        self.rpc.set_volume.side_effect = LocalRpcError("offline")
        with self.assertRaises(HomeAssistantError):
            await device.set_volume(0.10)
        self.assertFalse(device.available)
        self.assertFalse(coordinator.state.snapshot().local_available)
        self.cloud.status.refresh.assert_not_awaited()

    async def test_missing_post_write_readback_does_not_start_cloud_fallback(self):
        device, coordinator = self.make_device()
        await coordinator.async_refresh()
        self.rpc.volume.side_effect = LocalRpcError("offline")
        await device.set_volume(0.10)
        self.assertEqual(device.volume_level, 0.10)
        self.assertFalse(device.local_available)
        self.assertEqual(device.local_last_error, "offline")
        self.cloud.status.refresh.assert_awaited_once()
        self.assertTrue(
            coordinator.state.diagnostics()["volume_level"]["pending_write"]
        )

    async def test_cloud_only_entity_starts_unavailable_when_only_lan_works(self):
        device, coordinator = self.make_device()
        self.cloud.status.refresh.side_effect = ConfigEntryAuthFailed("expired")
        await coordinator.async_refresh()
        switch = SoundbarSwitchAdvancedAudio(
            device,
            "bassmode",
            lambda: device.bass_mode,
            device.set_bass_mode,
            device.set_bass_mode,
        )
        register_device_update_listener(self.entry, device, [switch])
        self.assertTrue(device.available)
        self.assertFalse(switch.available)
