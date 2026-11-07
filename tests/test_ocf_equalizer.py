"""Model-scoped Bass/Treble numbers; other equalizer writes remain unverified."""

from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock

import test_coordinator as fixtures
from homeassistant.exceptions import HomeAssistantError

from custom_components.samsung_soundbar import number
from custom_components.samsung_soundbar.const import (
    CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
    CONTROL_MODE_LOCAL_ONLY,
)
from custom_components.samsung_soundbar.local_ocf import OcfError, OcfWriteResult

HREF = "/sec/networkaudio/tone"
PREFIX = "x.com.samsung.networkaudio."


class TestOcfTone(IsolatedAsyncioTestCase):
    setUp = fixtures.TestCoordinator.setUp
    make_device = fixtures.TestCoordinator.make_device

    def prepare(self, mode=CONTROL_MODE_LOCAL_ONLY):
        device, coordinator = self.make_device(mode)
        self.values = {PREFIX + "bass": 0, PREFIX + "treble": 1}
        client = MagicMock()
        client.available = client.validated = True
        client.async_close = AsyncMock()
        client.diagnostics.return_value = {"configured": True, "available": True}
        client.async_read = AsyncMock(
            side_effect=lambda href: dict(self.values) if href == HREF else {}
        )

        async def write(href, payload, *, tone_profile):
            self.assertEqual(href, HREF)
            self.assertEqual(set(payload), {tone_profile.property})
            self.values.update(payload)
            return OcfWriteResult(True, True, False, 68)

        client.async_write = AsyncMock(side_effect=write)
        coordinator.configure_ocf(client)
        return device, coordinator, client

    async def test_independent_numbers_are_created_late_with_native_scale(self):
        device, coordinator, _client = self.prepare()
        added = MagicMock()
        self.entry.runtime_data.device = device
        await number.async_setup_entry(self.hass, self.entry, added)
        self.assertEqual(added.call_args.args[0], [])
        await coordinator.async_refresh_all()
        self.assertTrue(device.ocf_tone_profile("bass_level"))
        entities = [entity for call in added.call_args_list for entity in call.args[0]]
        self.assertEqual(
            {entity.unique_id for entity in entities},
            {"soundbar-id_num_bass_level", "soundbar-id_num_treble_level"},
        )
        for entity in entities:
            self.assertEqual(
                (entity.native_min_value, entity.native_max_value, entity.native_step),
                (-6, 6, 1),
            )
            self.assertFalse(entity.should_poll)
            self.assertIsNone(entity.native_unit_of_measurement)

    async def test_single_property_write_has_actual_readback_and_no_cloud(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        await device.set_tone_level("bass_level", -2.0)
        self.assertEqual(
            client.async_write.call_args.args, (HREF, {PREFIX + "bass": -2})
        )
        self.assertIs(type(client.async_write.call_args.args[1][PREFIX + "bass"]), int)
        self.assertEqual(device.tone_level("bass_level"), -2)
        self.assertEqual(device.tone_level("treble_level"), 1)
        self.assertNotIn("bass_level", coordinator.state.pending)
        self.cloud.status.refresh.assert_not_awaited()

    async def test_invalid_values_are_rejected_without_io(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        for value in (-7, 7, 0.5, True, None, "0", float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(HomeAssistantError):
                await device.set_tone_level("bass_level", value)
        with self.assertRaises(HomeAssistantError):
            await device.set_tone_level("unknown", 0)
        client.async_write.assert_not_awaited()

    async def test_missing_field_only_creates_other_number(self):
        device, coordinator, client = self.prepare()
        self.values.pop(PREFIX + "treble")
        await coordinator.async_refresh_all()
        self.assertIsNotNone(device.ocf_tone_profile("bass_level"))
        self.assertIsNone(device.ocf_tone_profile("treble_level"))
        with self.assertRaises(HomeAssistantError):
            await device.set_tone_level("treble_level", 0)
        client.async_write.assert_not_awaited()

    async def test_off_and_auth_failure_do_not_wake_or_fallback(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        self.assertIsNotNone(device.ocf_tone_profile("bass_level"))
        self.rpc.status.return_value = {**fixtures.LOCAL_STATUS, "power": "powerOff"}
        await coordinator.async_refresh_all()
        self.assertFalse(device.tone_control_available("bass_level"))
        with self.assertRaises(HomeAssistantError):
            await device.set_tone_level("bass_level", 1)
        client.available = False
        client.async_read.side_effect = OcfError("auth")
        await coordinator.async_refresh_all()
        self.assertIsNotNone(device.ocf_tone_profile("bass_level"))
        with self.assertRaises(HomeAssistantError):
            await device.set_tone_level("bass_level", 1)
        client.async_write.assert_not_awaited()
        self.rpc.power_on.assert_not_called()
        self.cloud.status.refresh.assert_not_awaited()

    async def test_mismatch_is_not_success_or_optimistic_state(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        client.async_write.side_effect = None
        client.async_write.return_value = OcfWriteResult(True, False, False, 68)
        with self.assertRaises(HomeAssistantError):
            await device.set_tone_level("bass_level", 3)
        self.assertEqual(device.tone_level("bass_level"), 0)
        client.async_write.assert_awaited_once()

    async def test_read_before_write_cannot_use_missing_old_cache(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        self.assertIsNotNone(device.ocf_tone_profile("bass_level"))
        self.values.pop(PREFIX + "bass")
        with self.assertRaises(HomeAssistantError):
            await device.set_tone_level("bass_level", 2)
        client.async_write.assert_not_awaited()

    async def test_unverified_model_never_gets_local_tone_profile(self):
        device, coordinator, client = self.prepare()
        self.rpc.status.return_value = {**fixtures.LOCAL_STATUS, "identifier": "OTHER"}
        await coordinator.async_refresh_all()
        self.assertIsNone(device.ocf_tone_profile("bass_level"))
        with self.assertRaises(HomeAssistantError):
            await device.set_tone_level("bass_level", 0)
        client.async_write.assert_not_awaited()

    async def test_hybrid_transport_failure_never_replays_tone_over_cloud(self):
        device, coordinator, client = self.prepare(
            CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS
        )
        await coordinator.async_refresh_all()
        self.cloud.status.refresh.reset_mock()
        client.async_write.side_effect = OcfError("transport")
        with self.assertRaises(HomeAssistantError):
            await device.set_tone_level("bass_level", 1)
        client.async_write.assert_awaited_once()
        self.cloud.status.refresh.assert_not_awaited()
        self.assertEqual(device.tone_level("bass_level"), 0)

    async def test_invalid_and_expired_fields_do_not_invent_valid_zero(self):
        device, coordinator, _client = self.prepare()
        self.values[PREFIX + "bass"] = 7
        await coordinator.async_refresh_all()
        self.assertIsNone(device.ocf_tone_profile("bass_level"))
        self.assertIsNotNone(device.ocf_tone_profile("treble_level"))
        self.values[PREFIX + "bass"] = -1
        await coordinator.async_refresh_all()
        self.assertEqual(device.tone_level("bass_level"), -1)
        self.values.pop(PREFIX + "bass")
        self.now += 121
        await coordinator.async_refresh_all()
        self.assertIsNone(device.tone_level("bass_level"))
        self.assertFalse(device.tone_control_available("bass_level"))
        self.assertTrue(device.tone_control_available("treble_level"))
