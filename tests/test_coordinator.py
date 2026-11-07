"""Shared local-device fixture and RPC/OCF coordinator regressions."""

import asyncio
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.samsung_soundbar.api_extension.SoundbarDevice import (
    SoundbarDevice,
)
from custom_components.samsung_soundbar.const import (
    CONTROL_MODE_HYBRID_LOCAL_OCF,
    CONTROL_MODE_LOCAL_ONLY,
)
from custom_components.samsung_soundbar.coordinator import SoundbarCoordinator
from custom_components.samsung_soundbar.entity_updates import (
    register_device_update_listener,
)
from custom_components.samsung_soundbar.local_device import LocalDevice
from custom_components.samsung_soundbar.local_rpc import (
    LocalRpcError,
    LocalRpcMethodNotFoundError,
)
from custom_components.samsung_soundbar.sensor import VolumeSensor
from custom_components.samsung_soundbar.state import SoundbarState

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
        self.state = SoundbarState(ocf=True, clock=lambda: self.now)

    def test_partial_merge_and_per_field_age(self):
        self.state.merge("rpc", {"volume_level": 0.08, "power": "on"})
        self.state.merge("ocf", {"bass_mode": True})
        self.now = 3
        self.state.merge("rpc", {"power": "on", "volume_level": None})
        self.assertEqual(self.state.diagnostics()["volume_level"]["age_seconds"], 2)
        self.assertEqual(self.state.diagnostics()["power"]["age_seconds"], 0)
        self.assertTrue(self.state.value("bass_mode"))

    def test_expired_rpc_does_not_fall_back_to_ocf(self):
        self.state.merge("rpc", {"volume_level": 0.08})
        self.now = 122
        self.state.merge("ocf", {"volume_level": 0.09, "bass_mode": False})
        self.assertIsNone(self.state.value("volume_level"))
        self.assertIs(self.state.value("bass_mode"), False)

    def test_write_guard_filters_pre_write_and_stale_readback(self):
        self.state.merge("rpc", {"sound_mode": "Standard"})
        self.now = 2
        self.state.expect({"sound_mode": "Game Pro"})
        self.state.merge("rpc", {"sound_mode": "Surround"}, observed_at=1)
        self.assertEqual(self.state.value("sound_mode"), "Game Pro")
        self.state.merge("rpc", {"sound_mode": "Standard"})
        self.now = 8
        self.assertEqual(self.state.value("sound_mode"), "Standard")

    def test_owner_confirms_rpc_guard(self):
        self.state.expect({"sound_mode": "Surround"})
        self.state.merge("ocf", {"sound_mode": "Surround"})
        self.assertIn("sound_mode", self.state.pending)
        self.state.merge("rpc", {"sound_mode": "Surround"})
        self.assertNotIn("sound_mode", self.state.pending)


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
        self.rpc.select_input = AsyncMock()
        self.rpc.power_on = AsyncMock()
        self.rpc.power_off = AsyncMock()
        self.local_device = LocalDevice("soundbar-id")
        self.schedule = patch.object(SoundbarCoordinator, "_schedule_refresh")
        self.schedule.start()
        self.addCleanup(self.schedule.stop)

    def make_device(self, mode=CONTROL_MODE_HYBRID_LOCAL_OCF):
        device = SoundbarDevice(
            self.local_device,
            MagicMock(),
            100,
            "Q800F",
            control_mode=mode,
            local_rpc=self.rpc,
        )
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
        self.assertTrue(all(not entity._attr_should_poll for entity in entities))
        self.now = 3
        await coordinator.async_refresh()
        self.rpc.status.assert_awaited_once()
        self.rpc.volume.assert_awaited_once()
        self.now = 62
        await coordinator.async_refresh()
        self.assertEqual(self.rpc.status.await_count, 2)

    async def test_mode_write_has_targeted_readback_and_shared_guard(self):
        device, coordinator = self.make_device()
        await coordinator.async_refresh()
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
        self.assertEqual(coordinator.state.diagnostics()["sound_mode"]["source"], "rpc")

    async def test_fast_field_error_preserves_other_values_without_refreshing_ttl(self):
        device, coordinator = self.make_device(CONTROL_MODE_LOCAL_ONLY)
        await coordinator.async_refresh()
        previous = coordinator.state.records["rpc"]["input_source"].observed_at
        self.rpc.input_source.side_effect = LocalRpcMethodNotFoundError(
            "unsupported", code=-32601
        )
        self.rpc.volume.return_value = 9
        self.now = 3
        await coordinator.async_refresh()
        self.assertTrue(device.available)
        self.assertEqual(device.volume_level, 0.09)
        self.assertEqual(
            coordinator.state.records["rpc"]["input_source"].observed_at, previous
        )
        self.now = 125
        await device.update_local_input_source(min_age=None)
        self.assertIsNone(coordinator.state.value("input_source"))
        self.assertEqual(device.volume_level, 0.09)

    async def test_rpc_outage_is_unavailable_without_discarding_fresh_state(self):
        device, coordinator = self.make_device()
        await coordinator.async_refresh()
        self.rpc.power_state.side_effect = LocalRpcError("offline")
        self.now = 3
        await coordinator.async_refresh()
        self.assertFalse(device.available)
        self.assertEqual(coordinator.state.value("volume_level"), 0.08)
        self.assertEqual(set(coordinator.diagnostics()["transports"]), {"rpc", "ocf"})

    async def test_expired_volume_is_unknown_not_zero_or_a_sensor_exception(self):
        device, coordinator = self.make_device(CONTROL_MODE_LOCAL_ONLY)
        await coordinator.async_refresh()
        self.now = 122
        coordinator.transport_changed("rpc", False)
        sensor = VolumeSensor(device, "volume_level", "mdi:volume-high")
        self.assertIsNone(device.volume_level)
        self.assertIsNone(sensor.native_value)
        self.assertFalse(device.available)

    async def test_input_command_reads_power_and_input_and_clears_stream_label(self):
        device, coordinator = self.make_device()
        self.rpc.status.return_value = {**LOCAL_STATUS, "input_source": "WIFI_AIRPLAY"}
        await coordinator.async_refresh()
        await device.select_source("TV ARC/eARC")
        self.assertEqual(device.input_source, "TV ARC/eARC")
        self.assertEqual(device.sound_from_detail_name, "External Device")
        self.rpc.power_state.assert_awaited_once()
        self.rpc.input_source.assert_awaited_once()
        self.rpc.volume.assert_not_awaited()

    async def test_poweroff_retains_selectors_without_freshening_their_records(self):
        device, coordinator = self.make_device()
        await coordinator.async_refresh()
        mode = coordinator.state.records["rpc"]["sound_mode"]
        self.rpc.status.return_value = {
            **LOCAL_STATUS,
            "power": "powerOff",
            "input_source": None,
            "sound_mode": None,
        }
        self.now = 62
        await coordinator.async_refresh()
        self.assertEqual(device.state, "off")
        self.assertEqual(device.sound_mode, "Game Pro")
        self.assertEqual(device.input_source, "TV ARC/eARC")
        self.assertIs(coordinator.state.records["rpc"]["sound_mode"], mode)

    async def test_cancelled_command_releases_transaction_and_next_command_works(self):
        device, coordinator = self.make_device()
        await coordinator.async_refresh()
        started = asyncio.Event()

        async def blocked(_mode):
            started.set()
            await asyncio.Event().wait()

        self.rpc.set_sound_mode.side_effect = blocked
        task = asyncio.create_task(device.select_sound_mode("Surround"))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn("sound_mode", coordinator.state.pending)
        self.assertIsNone(coordinator.state.value("sound_mode"))
        self.rpc.set_sound_mode.side_effect = None
        self.rpc.sound_mode.return_value = "STANDARD"
        await asyncio.wait_for(device.select_sound_mode("Standard"), 1)
        self.assertEqual(device.sound_mode, "Standard")

    async def test_shutdown_removes_listener_and_cancels_polling(self):
        _device, coordinator = self.make_device()
        await coordinator.async_refresh()
        listener = MagicMock()
        unsubscribe = coordinator.async_add_listener(listener)
        unsubscribe()
        coordinator.receive("rpc", {"volume_level": 0.09})
        listener.assert_not_called()
        await coordinator.async_shutdown()
        self.assertTrue(coordinator._shutdown_requested)
