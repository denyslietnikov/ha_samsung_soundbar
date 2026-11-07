"""Verified absolute Woofer route; never infer success or replay a write."""

import asyncio
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock

import test_coordinator as fixtures
from homeassistant.components.number import NumberMode
from homeassistant.exceptions import HomeAssistantError

from custom_components.samsung_soundbar import number
from custom_components.samsung_soundbar.const import (
    CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
    CONTROL_MODE_LOCAL_ONLY,
    CONTROL_MODE_SMARTTHINGS_CLOUD,
)
from custom_components.samsung_soundbar.entity_updates import (
    register_device_update_listener,
)
from custom_components.samsung_soundbar.local_ocf import OcfError, OcfWriteResult

HREF = "/sec/networkaudio/woofer"
FIELD = "x.com.samsung.networkaudio.woofer"
CONNECTION = "x.com.samsung.networkaudio.connection"


class TestOcfWoofer(IsolatedAsyncioTestCase):
    setUp = fixtures.TestCoordinator.setUp
    make_device = fixtures.TestCoordinator.make_device

    def prepare(self, mode=CONTROL_MODE_LOCAL_ONLY):
        device, coordinator = self.make_device(mode)
        self.level = -6
        self.connection = "on"
        client = MagicMock()
        client.async_close = AsyncMock()
        client.available = client.validated = True
        client.diagnostics.return_value = {"configured": True, "available": True}
        client.async_read = AsyncMock(
            side_effect=lambda href: (
                {FIELD: self.level, CONNECTION: self.connection} if href == HREF else {}
            )
        )

        async def write(href, payload, **kwargs):
            self.level = payload[FIELD]
            return OcfWriteResult(True, True, False, 68)

        client.async_write = AsyncMock(side_effect=write)
        coordinator.configure_ocf(client)
        return device, coordinator, client

    async def test_slider_keeps_unique_id_and_real_db_scale(self):
        device, coordinator, _client = self.prepare()
        await coordinator.async_refresh_all()
        self.assertTrue(device.can_control_woofer_level)
        entity = number.SoundbarWooferNumberEntity(device, "woofer_level")
        self.assertEqual(entity.unique_id, "soundbar-id_sw_woofer_level")
        self.assertEqual(entity.name, "Woofer")
        self.assertEqual(entity.mode, NumberMode.SLIDER)
        self.assertEqual(
            (entity.native_min_value, entity.native_max_value, entity.native_step),
            (-12, 6, 1),
        )
        self.assertEqual(entity.native_value, -6)

    async def test_single_ocf_write_has_targeted_readback_without_cloud(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        self.assertEqual(device.woofer_level, -6)
        await device.set_woofer(-7.0)
        self.assertEqual(client.async_write.await_count, 1)
        self.assertEqual(client.async_write.call_args.args, (HREF, {FIELD: -7}))
        self.assertEqual(device.woofer_level, -7)
        self.assertEqual(coordinator.state.resolve("woofer_level").source, "ocf")
        self.assertNotIn("woofer_level", coordinator.state.pending)
        self.cloud.status.refresh.assert_not_awaited()

    async def test_invalid_values_never_reach_any_transport(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        for value in (-13, 7, -6.5, True, float("nan"), float("inf"), "0", None):
            with (
                self.subTest(value=value),
                self.assertRaises((ValueError, HomeAssistantError)),
            ):
                await device.set_woofer(value)
        client.async_write.assert_not_awaited()
        self.cloud.status.refresh.assert_not_awaited()

    async def test_disconnect_or_off_disables_number_without_disabling_rpc(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        entity = number.SoundbarWooferNumberEntity(device, "woofer_level")
        register_device_update_listener(self.entry, device, [entity])
        self.assertTrue(entity.available)
        self.connection = "off"
        await coordinator.async_refresh_all()
        self.assertFalse(entity.available)
        with self.assertRaises(HomeAssistantError):
            await device.set_woofer(1)
        self.connection = "on"
        self.rpc.status.return_value = {**fixtures.LOCAL_STATUS, "power": "powerOff"}
        await coordinator.async_refresh_all()
        self.assertFalse(entity.available)
        client.async_write.assert_not_awaited()
        self.assertTrue(device.local_available)

    async def test_read_before_write_checks_connection_again_under_shared_lock(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        self.connection = "off"
        with self.assertRaises(HomeAssistantError):
            await device.set_woofer(1)
        client.async_write.assert_not_awaited()

    async def test_mismatch_or_timeout_never_falls_back_or_publishes_requested_level(
        self,
    ):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        for result in (
            OcfWriteResult(True, False, False, 68),
            OcfWriteResult(False, None, True, None),
        ):
            client.async_write = AsyncMock(return_value=result)
            with self.assertRaises(HomeAssistantError):
                await device.set_woofer(1)
            client.async_write.assert_awaited_once()
            self.assertEqual(device.woofer_level, -6)
        self.rpc.remote_key.assert_not_called()
        self.cloud.status.refresh.assert_not_awaited()

    async def test_ocf_outage_retains_absolute_route_and_never_uses_cloud(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        client.available = False
        client.async_read.side_effect = OcfError("transport")
        await coordinator.async_refresh_all()
        self.assertTrue(device.can_control_woofer_level)
        with self.assertRaises(HomeAssistantError):
            await device.set_woofer(1)
        client.async_write.assert_not_awaited()
        self.cloud.status.refresh.assert_not_awaited()

    async def test_number_is_discovered_once_after_first_ocf_read(self):
        device, coordinator, _client = self.prepare()
        added = MagicMock()
        self.entry.runtime_data.device = device
        await number.async_setup_entry(self.hass, self.entry, added)
        self.assertEqual(added.call_args.args[0], [])
        await coordinator.async_refresh_all()
        created = [entity for call in added.call_args_list for entity in call.args[0]]
        self.assertEqual(len(created), 1)
        await coordinator.async_refresh_all()
        self.assertEqual(
            len([entity for call in added.call_args_list for entity in call.args[0]]), 1
        )

    async def test_unverified_model_or_unauthenticated_session_does_not_create_slider(
        self,
    ):
        device, coordinator, client = self.prepare()
        self.rpc.status.return_value = {
            **fixtures.LOCAL_STATUS,
            "identifier": "unknown",
        }
        await coordinator.async_refresh_all()
        self.assertFalse(device.can_control_woofer_level)
        client.validated = False
        self.rpc.status.return_value = fixtures.LOCAL_STATUS
        await coordinator.async_refresh_all()
        self.assertFalse(device.can_control_woofer_level)

    async def test_missing_pre_write_field_cannot_reuse_old_cached_confirmation(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        client.async_read.side_effect = lambda href: {FIELD: -6} if href == HREF else {}
        with self.assertRaises(HomeAssistantError):
            await device.set_woofer(1)
        client.async_write.assert_not_awaited()

    async def test_out_of_profile_readback_disables_the_existing_slider(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        self.assertTrue(device.woofer_control_available)
        self.level = 100
        await coordinator.async_refresh_all()
        self.assertFalse(device.woofer_control_available)
        with self.assertRaises(HomeAssistantError):
            await device.set_woofer(1)
        client.async_write.assert_not_awaited()

    async def test_pre_write_permission_error_keeps_its_error_category(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        self.assertTrue(device.woofer_control_available)
        client.async_read.side_effect = OcfError("permission")
        with self.assertRaisesRegex(HomeAssistantError, "permission"):
            await device.set_woofer(1)
        client.async_write.assert_not_awaited()

    async def test_cancelled_write_marks_woofer_unconfirmed_without_replay(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        entity = number.SoundbarWooferNumberEntity(device, "woofer_level")
        register_device_update_listener(self.entry, device, [entity])
        started = asyncio.Event()

        async def write(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        client.async_write.side_effect = write
        task = asyncio.create_task(device.set_woofer(1))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(entity.available)
        client.async_write.assert_awaited_once()
        self.cloud.status.refresh.assert_not_awaited()

    async def test_power_changed_while_command_queued_is_checked_before_post(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        async with coordinator._write_lock:
            task = asyncio.create_task(device.set_woofer(1))
            await asyncio.sleep(0)
            coordinator.receive("rpc", {"power": "off"})
        with self.assertRaises(HomeAssistantError):
            await task
        client.async_write.assert_not_awaited()

    async def test_post_write_missing_connection_cannot_confirm_success(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        reads = 0

        def read(href):
            nonlocal reads
            if href != HREF:
                return {}
            reads += 1
            return {FIELD: self.level, **({CONNECTION: "on"} if reads == 1 else {})}

        client.async_read.side_effect = read
        with self.assertRaises(HomeAssistantError):
            await device.set_woofer(1)
        client.async_write.assert_awaited_once()

    async def _check_queued_legacy_route_transition(self, relative):
        device, coordinator, client = self.prepare(
            CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS
        )
        started, release = asyncio.Event(), asyncio.Event()

        async def read(href):
            if href == HREF:
                started.set()
                await release.wait()
                return {FIELD: -6, CONNECTION: "on"}
            return {}

        client.async_read.side_effect = read
        self.rpc.remote_key = AsyncMock()
        device.set_custom_execution_data = AsyncMock()
        await coordinator.async_refresh()
        await started.wait()
        self.assertIsNone(device.ocf_woofer_profile)
        task = asyncio.create_task(
            device.adjust_local_woofer(True) if relative else device.set_woofer(-7)
        )
        await asyncio.sleep(0)
        release.set()
        with self.assertRaises(HomeAssistantError):
            await task
        self.rpc.remote_key.assert_not_awaited()
        device.set_custom_execution_data.assert_not_awaited()
        client.async_write.assert_not_awaited()

    async def test_queued_cloud_write_is_rejected_if_ocf_profile_appears(self):
        await self._check_queued_legacy_route_transition(False)

    async def test_queued_relative_write_is_rejected_if_ocf_profile_appears(self):
        await self._check_queued_legacy_route_transition(True)

    async def test_cloud_only_legacy_woofer_route_is_preserved(self):
        device, coordinator = self.make_device(CONTROL_MODE_SMARTTHINGS_CLOUD)
        await coordinator.async_refresh_all()
        device.set_custom_execution_data = AsyncMock()
        await device.set_woofer(-7)
        device.set_custom_execution_data.assert_awaited_once_with(
            href=HREF, property=FIELD, value=-7
        )
        self.rpc.status.assert_not_awaited()


if __name__ == "__main__":
    import unittest

    unittest.main()
