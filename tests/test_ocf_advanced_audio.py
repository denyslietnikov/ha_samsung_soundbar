"""Local audio switch profiles, exact single-field writes and authoritative state."""

from copy import deepcopy
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock

import test_coordinator as fixtures
from homeassistant.exceptions import HomeAssistantError

from custom_components.samsung_soundbar import switch
from custom_components.samsung_soundbar.const import (
    CONTROL_MODE_HYBRID_LOCAL_OCF,
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

    def prepare(self, mode=CONTROL_MODE_HYBRID_LOCAL_OCF):
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


if __name__ == "__main__":
    import unittest

    unittest.main()
