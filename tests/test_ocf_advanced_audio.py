"""Local audio switch profiles, exact single-field writes and authoritative state."""

import asyncio
from copy import deepcopy
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock

import test_coordinator as fixtures
from homeassistant.exceptions import HomeAssistantError

from custom_components.samsung_soundbar import switch
from custom_components.samsung_soundbar.const import (
    CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
    CONTROL_MODE_LOCAL_ONLY,
    CONTROL_MODE_SMARTTHINGS_CLOUD,
)
from custom_components.samsung_soundbar.local_ocf import OcfError, OcfWriteResult

BASE = "/sec/networkaudio/"
PREFIX = "x.com.samsung.networkaudio."
ADVANCED = BASE + "advancedaudio"
SPACEFIT = BASE + "spacefitSound"
SETTINGS = (
    ("night_mode", "night_mode", "set_night_mode", "nightmode", ADVANCED, "nightmode"),
    ("bass_mode", "bass_mode", "set_bass_mode", "bassmode", ADVANCED, "bassboost"),
    (
        "voice_amplifier",
        "voice_amplifier",
        "set_voice_amplifier",
        "voice_amplifier",
        ADVANCED,
        "voiceamplifier",
    ),
    (
        "virtual_sound",
        "virtual_sound",
        "set_virtual_sound",
        "virtual",
        ADVANCED,
        "virtual",
    ),
    (
        "moderate_bass",
        "moderate_bass",
        "set_moderate_bass",
        "moderate_bass",
        ADVANCED,
        "moderatebass",
    ),
    (
        "spacefit_sound",
        "space_fit_sound",
        "set_space_fit_sound",
        "space_fit_sound",
        SPACEFIT,
        "spacefitSound",
    ),
)


class TestOcfAdvancedAudio(IsolatedAsyncioTestCase):
    setUp = fixtures.TestCoordinator.setUp
    make_device = fixtures.TestCoordinator.make_device

    def prepare(self, mode=CONTROL_MODE_LOCAL_ONLY):
        device, coordinator = self.make_device(mode)
        self.payloads = {
            ADVANCED: {
                PREFIX + raw: 0
                for _, _, _, _, href, raw in SETTINGS
                if href == ADVANCED
            },
            SPACEFIT: {PREFIX + "spacefitSound": 1},
        }
        client = MagicMock()
        client.async_close = AsyncMock()
        client.available = client.validated = True
        client.diagnostics.return_value = {"configured": True, "available": True}
        client.async_read = AsyncMock(
            side_effect=lambda href: deepcopy(self.payloads.get(href, {}))
        )

        async def write(href, payload, **kwargs):
            self.payloads[href].update(payload)
            return OcfWriteResult(True, True, False, 68)

        client.async_write = AsyncMock(side_effect=write)
        coordinator.configure_ocf(client)
        return device, coordinator, client

    async def entities(self, device):
        added = MagicMock()
        self.entry.runtime_data.device = device
        await switch.async_setup_entry(self.hass, self.entry, added)
        return added

    async def test_six_switches_keep_existing_ids_and_observed_states(self):
        device, coordinator, _client = self.prepare()
        await coordinator.async_refresh_all()
        added = await self.entities(device)
        entities = added.call_args.args[0]
        self.assertEqual(
            {entity.unique_id for entity in entities},
            {"soundbar-id_sw_" + suffix for _, _, _, suffix, _, _ in SETTINGS},
        )
        self.assertEqual(sum(entity.is_on is True for entity in entities), 1)
        self.assertTrue(all(entity.assumed_state is False for entity in entities))

    async def test_each_switch_writes_exact_integer_property_and_reads_actual_value(
        self,
    ):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        for field, getter, setter, _, href, raw in SETTINGS:
            with self.subTest(field=field):
                before = deepcopy(self.payloads)
                value = not bool(before[href][PREFIX + raw])
                await getattr(device, setter)(value)
                self.assertEqual(
                    client.async_write.call_args.args,
                    (href, {PREFIX + raw: int(value)}),
                )
                self.assertIs(
                    type(client.async_write.call_args.args[1][PREFIX + raw]), int
                )
                self.assertIs(getattr(device, getter), value)
                self.assertEqual(coordinator.state.resolve(field).source, "ocf")
                self.assertNotIn(field, coordinator.state.pending)
                self.payloads[href][PREFIX + raw] = before[href][PREFIX + raw]
                self.assertEqual(self.payloads, before)
                await coordinator.async_refresh_all()
        self.cloud.status.refresh.assert_not_awaited()

    async def test_non_boolean_input_is_rejected_without_io(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        for _, _, setter, _, _, _ in SETTINGS:
            for value in (0, 1, "on", None):
                with (
                    self.subTest(setter=setter, value=value),
                    self.assertRaises(HomeAssistantError),
                ):
                    await getattr(device, setter)(value)
        client.async_write.assert_not_awaited()

    async def test_spacefit_support_zero_does_not_disable_other_switches(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        added = await self.entities(device)
        entities = {entity.unique_id: entity for entity in added.call_args.args[0]}
        self.payloads[SPACEFIT] = {PREFIX + "isSupported": 0}
        await coordinator.async_refresh_all()
        self.assertFalse(entities["soundbar-id_sw_space_fit_sound"].available)
        self.assertIsNone(entities["soundbar-id_sw_space_fit_sound"].is_on)
        self.assertTrue(entities["soundbar-id_sw_bassmode"].available)
        self.assertEqual(added.call_count, 1)
        with self.assertRaises(HomeAssistantError):
            await device.set_space_fit_sound(False)
        client.async_write.assert_not_awaited()

    async def test_external_app_change_and_ocf_failure_do_not_use_cloud(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        added = await self.entities(device)
        self.payloads[ADVANCED][PREFIX + "moderatebass"] = 1
        await coordinator.async_refresh_all()
        self.assertIs(device.moderate_bass, True)
        client.available = False
        client.async_read.side_effect = OcfError("auth")
        await coordinator.async_refresh_all()
        self.assertTrue(device.local_available)
        self.assertTrue(all(not entity.available for entity in added.call_args.args[0]))
        with self.assertRaises(HomeAssistantError):
            await device.set_moderate_bass(False)
        client.async_write.assert_not_awaited()
        self.cloud.status.refresh.assert_not_awaited()

    async def test_missing_field_is_not_created_until_valid_readback_and_not_forced_off(
        self,
    ):
        device, coordinator, _client = self.prepare()
        del self.payloads[ADVANCED][PREFIX + "virtual"]
        await coordinator.async_refresh_all()
        added = await self.entities(device)
        self.assertEqual(len(added.call_args.args[0]), 5)
        self.assertIsNone(device.virtual_sound)
        self.payloads[ADVANCED][PREFIX + "virtual"] = 1
        await coordinator.async_refresh_all()
        created = [entity for call in added.call_args_list for entity in call.args[0]]
        self.assertEqual(len(created), 6)
        self.payloads[ADVANCED][PREFIX + "virtual"] = None
        self.now = 122
        await coordinator.async_refresh_all()
        virtual = next(
            entity for entity in created if entity.unique_id.endswith("_sw_virtual")
        )
        self.assertIsNone(virtual.is_on)
        self.assertFalse(virtual.available)

    async def test_rejected_or_ambiguous_write_does_not_show_success_or_replay(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        for result in (
            OcfWriteResult(True, False, False, 68),
            OcfWriteResult(False, None, True, None),
        ):
            client.async_write = AsyncMock(return_value=result)
            with self.assertRaises(HomeAssistantError):
                await device.set_moderate_bass(True)
            client.async_write.assert_awaited_once()
            self.assertIs(device.moderate_bass, False)
        self.cloud.status.refresh.assert_not_awaited()

    async def test_unverified_model_does_not_expose_local_switches(self):
        device, coordinator, client = self.prepare()
        self.rpc.status.return_value = {
            **fixtures.LOCAL_STATUS,
            "identifier": "unknown",
        }
        await coordinator.async_refresh_all()
        added = await self.entities(device)
        self.assertEqual(added.call_args.args[0], [])
        with self.assertRaises(HomeAssistantError):
            await device.set_moderate_bass(True)
        client.async_write.assert_not_awaited()

    async def test_cloud_only_legacy_commands_are_preserved_without_moderate_bass_guess(
        self,
    ):
        device, coordinator = self.make_device(CONTROL_MODE_SMARTTHINGS_CLOUD)
        await coordinator.async_refresh_all()
        device.set_custom_execution_data = AsyncMock()
        for _, _, setter, _, href, raw in SETTINGS:
            if raw == "moderatebass":
                with self.assertRaises(HomeAssistantError):
                    await getattr(device, setter)(True)
            else:
                await getattr(device, setter)(True)
                device.set_custom_execution_data.assert_awaited_with(
                    href=href, property=PREFIX + raw, value=1
                )
        self.assertEqual(device.set_custom_execution_data.await_count, 5)
        self.rpc.status.assert_not_awaited()

    async def test_new_ocf_write_supersedes_old_cloud_optimism(self):
        device, coordinator, _client = self.prepare()
        await coordinator.async_refresh_all()
        coordinator.state.expect({"bass_mode": True}, write_only=True, source="cloud")
        await device.set_bass_mode(False)
        self.assertIs(device.bass_mode, False)
        self.assertNotIn("bass_mode", coordinator.state.pending)
        self.assertEqual(coordinator.state.write_sources["bass_mode"], "ocf")

    async def test_queued_cloud_write_cannot_outlive_its_field_profile_transition(self):
        device, coordinator, client = self.prepare(
            CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS
        )
        started, release = asyncio.Event(), asyncio.Event()

        async def read(href):
            if href == ADVANCED:
                started.set()
                await release.wait()
            return deepcopy(self.payloads.get(href, {}))

        client.async_read.side_effect = read
        device.set_custom_execution_data = AsyncMock()
        await coordinator.async_refresh()
        await started.wait()
        task = asyncio.create_task(device.set_night_mode(True))
        await asyncio.sleep(0)
        release.set()
        with self.assertRaises(HomeAssistantError):
            await task
        device.set_custom_execution_data.assert_not_awaited()
        client.async_write.assert_not_awaited()

    async def test_missing_ocf_field_preserves_only_that_legacy_cloud_route(self):
        device, coordinator, client = self.prepare(
            CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS
        )
        del self.payloads[ADVANCED][PREFIX + "virtual"]
        await coordinator.async_refresh_all()
        device.set_custom_execution_data = AsyncMock()
        await device.set_virtual_sound(True)
        device.set_custom_execution_data.assert_awaited_once_with(
            href=ADVANCED, property=PREFIX + "virtual", value=1
        )
        client.async_write.assert_not_awaited()
        self.assertIsNotNone(device.ocf_audio_profile("night_mode"))
        self.assertIsNone(device.ocf_audio_profile("virtual_sound"))

    async def test_late_ocf_discovery_supersedes_contradictory_cloud_optimism(self):
        device, coordinator, _client = self.prepare(
            CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS
        )
        del self.payloads[ADVANCED][PREFIX + "bassboost"]
        await coordinator.async_refresh_all()
        device.set_custom_execution_data = AsyncMock()
        self.now = 2
        await device.set_bass_mode(True)
        self.assertIs(device.bass_mode, True)
        self.now = 3
        self.payloads[ADVANCED][PREFIX + "bassboost"] = 0
        await coordinator.async_refresh_all()
        self.assertIsNotNone(device.ocf_audio_profile("bass_mode"))
        self.assertIs(device.bass_mode, False)
        self.assertNotIn("bass_mode", coordinator.state.pending)

    async def test_pre_cloud_write_cache_cannot_establish_a_new_local_route(self):
        device, coordinator, _client = self.prepare(
            CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS
        )
        self.rpc.status.return_value = {
            **fixtures.LOCAL_STATUS,
            "identifier": "unknown",
        }
        await coordinator.async_refresh_all()
        device.set_custom_execution_data = AsyncMock()
        self.now = 2
        await device.set_bass_mode(True)
        coordinator.receive("rpc", {"local_identifier": "22_AV_HW-Q800F"})
        self.assertIsNone(device.ocf_audio_profile("bass_mode"))


if __name__ == "__main__":
    import unittest

    unittest.main()
