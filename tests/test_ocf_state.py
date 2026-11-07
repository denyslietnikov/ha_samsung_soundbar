"""OCF field ownership, partial reads, and shared scheduling contracts."""

import asyncio
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, MagicMock

import test_coordinator as fixtures

from custom_components.samsung_soundbar.const import CONTROL_MODE_HYBRID_LOCAL_OCF
from custom_components.samsung_soundbar.entity_updates import (
    register_device_update_listener,
)
from custom_components.samsung_soundbar.local_ocf import OcfError, OcfWriteResult
from custom_components.samsung_soundbar.state import SoundbarState
from custom_components.samsung_soundbar.switch import SoundbarSwitchAdvancedAudio

BASE = "/sec/networkaudio/"
PREFIX = "x.com.samsung.networkaudio."
WOOFER = BASE + "woofer"
ADVANCED = BASE + "advancedaudio"


class TestOcfFields(TestCase):
    def setUp(self):
        self.now = 1.0
        self.state = SoundbarState(local=True, ocf=True, clock=lambda: self.now)

    def test_rpc_core_and_ocf_settings_have_stable_owners(self):
        self.state.merge("local", {"volume_level": 0.08, "power": "on"})
        self.state.merge(
            "ocf", {"volume_level": 0.16, "power": "off", "bass_mode": True}
        )
        self.assertEqual(self.state.value("volume_level"), 0.08)
        self.assertTrue(self.state.value("bass_mode"))
        self.assertEqual(self.state.resolve("power").source, "rpc")
        self.assertIn("rpc", self.state.diagnostics()["power"]["readbacks"])

    def test_null_expiry_support_and_cloud_disabling_are_field_scoped(self):
        self.state.merge("ocf", {"bass_mode": True, "night_mode": False})
        self.now = 20
        self.state.merge("ocf", {"bass_mode": None})
        self.assertTrue(self.state.value("bass_mode"))
        self.assertEqual(self.state.diagnostics()["bass_mode"]["age_seconds"], 19)
        self.state.invalidate("ocf", {"night_mode"})
        self.assertIsNone(self.state.value("night_mode"))
        self.now = 122
        self.assertIsNone(self.state.value("bass_mode"))


class TestOcfCoordinator(IsolatedAsyncioTestCase):
    setUp = fixtures.TestCoordinator.setUp
    make_device = fixtures.TestCoordinator.make_device

    def attach_ocf(self, coordinator):
        client = MagicMock()
        client.async_close = AsyncMock()
        client.available = True
        client.diagnostics.return_value = {"configured": True, "available": True}
        self.payloads = {
            WOOFER: {PREFIX + "woofer": 1, PREFIX + "connection": "on"},
            ADVANCED: {PREFIX + "bassboost": 1, PREFIX + "nightmode": 0},
        }
        client.async_read = AsyncMock(
            side_effect=lambda href: dict(self.payloads.get(href, {}))
        )
        client.async_write = AsyncMock(
            return_value=OcfWriteResult(True, True, False, 68)
        )
        coordinator.configure_ocf(client)
        return client

    async def test_shared_poll_reads_ocf_without_changing_rpc_core_or_entities(self):
        device, coordinator = self.make_device(CONTROL_MODE_HYBRID_LOCAL_OCF)
        client = self.attach_ocf(coordinator)
        await coordinator.async_refresh_all()
        self.assertEqual(device.volume_level, 0.08)
        self.assertEqual(device.woofer_level, 1)
        self.assertTrue(device.bass_mode)
        self.assertEqual(coordinator.state.resolve("woofer_level").source, "ocf")
        count = client.async_read.await_count
        self.now = 3
        await coordinator.async_refresh()
        self.assertEqual(client.async_read.await_count, count)

    async def test_slow_ocf_does_not_block_rpc_and_shutdown_cancels_owned_poll(self):
        device, coordinator = self.make_device(CONTROL_MODE_HYBRID_LOCAL_OCF)
        client = self.attach_ocf(coordinator)
        started = asyncio.Event()

        async def read(href):
            started.set()
            await asyncio.Event().wait()

        client.async_read.side_effect = read
        await asyncio.wait_for(coordinator.async_refresh(), 1)
        await started.wait()
        self.now = 3
        self.rpc.volume.return_value = 9
        await asyncio.wait_for(coordinator.async_refresh(), 1)
        self.assertEqual(device.volume_level, 0.09)
        task = coordinator._ocf_task
        await coordinator.async_shutdown()
        self.assertTrue(task.done())

    async def test_external_change_and_failure_do_not_refresh_old_ocf_fields(self):
        device, coordinator = self.make_device(CONTROL_MODE_HYBRID_LOCAL_OCF)
        client = self.attach_ocf(coordinator)
        await coordinator.async_refresh_all()
        self.now = 40
        self.payloads[ADVANCED] = {PREFIX + "bassboost": 0}
        await coordinator.async_refresh_all()
        self.assertFalse(device.bass_mode)
        self.assertEqual(
            coordinator.state.diagnostics()["night_mode"]["age_seconds"], 39
        )
        client.async_read.side_effect = OcfError("auth")
        client.available = False
        self.now = 162
        await coordinator.async_refresh_all()
        self.assertIsNone(coordinator.state.value("bass_mode"))
        self.assertTrue(device.available)
        self.assertFalse(coordinator.state.snapshot().ocf_available)

    async def test_resource_support_zero_does_not_disable_other_ocf_fields(self):
        device, coordinator = self.make_device(CONTROL_MODE_HYBRID_LOCAL_OCF)
        self.attach_ocf(coordinator)
        await coordinator.async_refresh_all()
        self.payloads[WOOFER] = {PREFIX + "isSupported": 0, PREFIX + "woofer": 5}
        await coordinator.async_refresh_all()
        self.assertIsNone(device.woofer_level)
        self.assertTrue(device.bass_mode)
        self.assertTrue(coordinator.state.snapshot().ocf_available)

    async def test_resource_error_disables_only_its_fields_not_rpc_or_other_settings(
        self,
    ):
        device, coordinator = self.make_device(CONTROL_MODE_HYBRID_LOCAL_OCF)
        client = self.attach_ocf(coordinator)
        await coordinator.async_refresh_all()
        entity = SimpleNamespace(_soundbar_ocf_fields=("woofer_level",), hass=None)
        register_device_update_listener(self.entry, device, [entity])
        self.assertTrue(entity._attr_available)

        async def read(href):
            if href == WOOFER:
                raise OcfError("permission")
            return dict(self.payloads.get(href, {}))

        client.async_read.side_effect = read
        await coordinator.async_refresh_all()
        self.assertFalse(coordinator.ocf_field_available("woofer_level"))
        self.assertFalse(entity._attr_available)
        self.assertTrue(coordinator.ocf_field_available("bass_mode"))
        self.assertTrue(device.local_available)
        self.assertIsNone(device.virtual_sound)

    async def test_authoritative_channel_get_revokes_omitted_or_unsupported_rows(self):
        _device, coordinator = self.make_device(CONTROL_MODE_HYBRID_LOCAL_OCF)
        self.attach_ocf(coordinator)
        href = BASE + "channelVolume"
        self.payloads[href] = {
            PREFIX + "isSupported": 1,
            PREFIX + "channelVolume": [
                {"name": "Spk_Center", "value": 1, "status": 1},
                {"name": "Spk_Rear", "value": 2, "status": 1},
            ],
        }
        await coordinator.async_refresh_all()
        self.assertEqual(coordinator.state.value("channel_level_rear"), 2)
        self.now = 40
        self.payloads[href] = {
            PREFIX + "isSupported": 1,
            PREFIX + "channelVolume": [
                {"name": "Spk_Center", "value": 3, "status": 0},
            ],
        }
        await coordinator.async_refresh_all()
        self.assertIsNone(coordinator.state.value("channel_level_center"))
        self.assertIsNone(coordinator.state.value("channel_level_rear"))
        self.assertFalse(coordinator.ocf_field_available("channel_level_rear"))
        self.assertEqual(coordinator.state.value("channel_status_center"), 0)

    async def test_omitted_or_malformed_settings_never_become_false_or_fresh(self):
        device, coordinator = self.make_device(CONTROL_MODE_HYBRID_LOCAL_OCF)
        self.attach_ocf(coordinator)
        await coordinator.async_refresh_all()
        self.now = 40
        self.payloads[ADVANCED] = {
            PREFIX + "bassboost": None,
            PREFIX + "nightmode": "0",
        }
        await coordinator.async_refresh_all()
        self.assertTrue(device.bass_mode)
        self.assertEqual(
            coordinator.state.diagnostics()["bass_mode"]["age_seconds"], 39
        )
        self.now = 122
        self.assertIsNone(device.bass_mode)
        switch = SoundbarSwitchAdvancedAudio(
            device,
            "bassmode",
            lambda: device.bass_mode,
            device.set_bass_mode,
            device.set_bass_mode,
        )
        self.assertIsNone(switch.is_on)

    async def test_ocf_entity_availability_does_not_follow_rpc_or_cloud_health(self):
        device, coordinator = self.make_device(CONTROL_MODE_HYBRID_LOCAL_OCF)
        client = self.attach_ocf(coordinator)
        await coordinator.async_refresh_all()
        entity = SimpleNamespace(_soundbar_ocf_fields=("bass_mode",), hass=None)
        register_device_update_listener(self.entry, device, [entity])
        self.assertTrue(entity._attr_available)
        client.async_read.side_effect = OcfError("auth")
        client.available = False
        await coordinator.async_refresh_all()
        self.assertFalse(entity._attr_available)
        self.assertTrue(device.local_available)

    async def test_ocf_write_waits_for_shared_rpc_command_queue(self):
        _device, coordinator = self.make_device()
        client = self.attach_ocf(coordinator)
        await coordinator.async_refresh_all()
        started, release = asyncio.Event(), asyncio.Event()

        async def action():
            started.set()
            await release.wait()

        rpc = asyncio.create_task(coordinator.async_write(action, {}))
        await started.wait()
        ocf = asyncio.create_task(
            coordinator.async_write_ocf(WOOFER, {PREFIX + "woofer": 0})
        )
        await asyncio.sleep(0)
        client.async_write.assert_not_awaited()
        release.set()
        await asyncio.gather(rpc, ocf)
        client.async_write.assert_awaited_once()

    async def test_ocf_write_serialized_with_rpc_and_never_falls_back(self):
        device, coordinator = self.make_device()
        client = self.attach_ocf(coordinator)
        await coordinator.async_refresh_all()
        client.async_write.return_value = OcfWriteResult(False, False, True, None)
        result = await coordinator.async_write_ocf(WOOFER, {PREFIX + "woofer": 0})
        self.assertTrue(result.ambiguous)
        client.async_write.assert_awaited_once()
        self.assertEqual(device.woofer_level, 1)
        self.assertNotIn("woofer_level", coordinator.state.pending)
        self.rpc.remote_key.assert_not_called()

    async def test_safe_ocf_diagnostic_path_drops_unknown_private_fields(self):
        _device, coordinator = self.make_device(CONTROL_MODE_HYBRID_LOCAL_OCF)
        client = self.attach_ocf(coordinator)
        self.payloads[WOOFER]["private_key"] = "DO_NOT_EXPORT"
        self.payloads[WOOFER][PREFIX + "connection"] = "DO_NOT_EXPORT"
        result = await coordinator.async_ocf_snapshot()
        self.assertNotIn("DO_NOT_EXPORT", repr(result))
        self.assertEqual(result["values"]["woofer_level"], 1)
        before = client.async_read.await_count
        with self.assertRaises(OcfError):
            await coordinator.async_ocf_snapshot(("/oic/sec/cred",))
        self.assertEqual(client.async_read.await_count, before)

    async def test_failed_ocf_write_immediately_updates_only_ocf_health(self):
        device, coordinator = self.make_device(CONTROL_MODE_HYBRID_LOCAL_OCF)
        client = self.attach_ocf(coordinator)
        await coordinator.async_refresh_all()
        client.async_write.side_effect = OcfError("auth")
        client.available = False
        with self.assertRaises(OcfError):
            await coordinator.async_write_ocf(WOOFER, {PREFIX + "woofer": 0})
        self.assertFalse(coordinator.state.snapshot().ocf_available)
        self.assertTrue(device.local_available)
        client.async_write.assert_awaited_once()

    async def test_cache_only_snapshot_does_not_wait_for_live_poll(self):
        _device, coordinator = self.make_device(CONTROL_MODE_HYBRID_LOCAL_OCF)
        client = self.attach_ocf(coordinator)
        started = asyncio.Event()

        async def read(href):
            started.set()
            await asyncio.Event().wait()

        client.async_read.side_effect = read
        await coordinator.async_refresh()
        await started.wait()
        result = await asyncio.wait_for(coordinator.async_ocf_snapshot(()), 0.1)
        self.assertTrue(result["configured"])


if __name__ == "__main__":
    import unittest

    unittest.main()
