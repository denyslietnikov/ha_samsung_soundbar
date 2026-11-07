"""Q800F equalizer routing, wire schema and shared preflight regressions."""

from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, MagicMock

import test_coordinator as fixtures
from homeassistant.exceptions import HomeAssistantError

from custom_components.samsung_soundbar.const import (
    CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
    CONTROL_MODE_LOCAL_ONLY,
    CONTROL_MODE_SMARTTHINGS_CLOUD,
)
from custom_components.samsung_soundbar.local_ocf import OcfError, OcfWriteResult
from custom_components.samsung_soundbar.local_rpc import LocalRpcError
from custom_components.samsung_soundbar.ocf_profiles import parse_eq_metadata

P = "x.com.samsung.networkaudio."
EQ = "/sec/networkaudio/eq"
MODE = "/sec/networkaudio/currentEQMode"
ADVANCED = "/sec/networkaudio/advancedaudio"
OPTIONS = ["NONE", "POP", "JAZZ", "CLASSIC", "CUSTOM"]


class TestEqualizerSchema(TestCase):
    def payload(self):
        return {
            P + "EQname": "NONE",
            P + "supportedList": list(OPTIONS),
            P + "action": "setEQmode",
            P + "EQband": ["0"] * 7,
        }

    def test_wire_strings_are_normalized_without_guessed_frequencies(self):
        values = parse_eq_metadata(self.payload())
        self.assertIs(values["equalizer_schema_valid"], True)
        self.assertEqual(values["equalizer_band_levels"], [0] * 7)
        self.assertNotIn("frequencies", values)

    def test_unknown_option_is_filtered_but_not_allowed_as_active_preset(self):
        payload = self.payload()
        payload[P + "supportedList"].append("PRIVATE_OR_FUTURE")
        self.assertTrue(parse_eq_metadata(payload)["equalizer_schema_valid"])
        self.assertEqual(
            parse_eq_metadata(payload)["supported_equalizer_presets"], OPTIONS
        )
        payload[P + "EQname"] = "PRIVATE_OR_FUTURE"
        self.assertFalse(parse_eq_metadata(payload)["equalizer_schema_valid"])
        self.assertNotIn("PRIVATE_OR_FUTURE", repr(parse_eq_metadata(payload)))

    def test_schema_changes_do_not_authorize_a_write(self):
        for bands in (
            ["0"] * 6,
            [0] * 7,
            [True] * 7,
            ["7"] * 7,
            ["+0"] * 7,
            ["0.0"] * 7,
        ):
            with self.subTest(bands=bands):
                payload = self.payload()
                payload[P + "EQband"] = bands
                self.assertFalse(parse_eq_metadata(payload)["equalizer_schema_valid"])
        for field in ("EQname", "supportedList", "action", "EQband"):
            payload = self.payload()
            payload.pop(P + field)
            self.assertFalse(parse_eq_metadata(payload)["equalizer_schema_valid"])


class TestOcfEqualizer(IsolatedAsyncioTestCase):
    setUp = fixtures.TestCoordinator.setUp
    make_device = fixtures.TestCoordinator.make_device

    def prepare(self, mode=CONTROL_MODE_LOCAL_ONLY):
        self.rpc.power_state.return_value = "powerOn"
        self.rpc.sound_mode.return_value = "STANDARD"
        self.rpc.sound_mode.side_effect = None
        self.rpc.status.return_value = {
            **fixtures.LOCAL_STATUS,
            "sound_mode": "STANDARD",
        }
        device, coordinator = self.make_device(mode)
        self.payloads = {
            EQ: {
                P + "EQname": "NONE",
                P + "supportedList": list(OPTIONS),
                P + "action": "setEQmode",
                P + "EQband": ["0"] * 7,
            },
            MODE: {P + "currentEQMode": "usereq"},
            ADVANCED: {P + "bassboost": 0},
        }
        client = MagicMock()
        client.available = client.validated = True
        client.async_close = AsyncMock()
        client.diagnostics.return_value = {"configured": True, "available": True}
        client.async_read = AsyncMock(
            side_effect=lambda href: dict(self.payloads.get(href, {}))
        )

        async def write(href, payload, **kwargs):
            self.payloads[href].update(payload)
            return OcfWriteResult(True, True, False, 68)

        client.async_write = AsyncMock(side_effect=write)
        coordinator.configure_ocf(client)
        return device, coordinator, client

    async def test_real_uppercase_schema_is_not_unknown_and_stays_local(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        self.assertEqual(device.supported_equalizer_presets, OPTIONS)
        self.assertEqual(device.active_equalizer_preset, "NONE")
        self.assertEqual(device.equalizer_bands, [0] * 7)
        self.assertIsNotNone(device.ocf_equalizer.profile)
        self.assertFalse(device.can_select_equalizer_preset)
        await device.set_equalizer_preset("POP")
        self.assertEqual(
            client.async_write.call_args.args,
            (EQ, {P + "EQname": "POP", P + "action": "setEQmode"}),
        )
        self.assertEqual(device.active_equalizer_preset, "POP")
        self.cloud.status.refresh.assert_not_awaited()

    async def test_unknown_preset_is_rejected_without_io(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        for option in ("ROCK", "pop", None):
            with self.subTest(option=option), self.assertRaises(HomeAssistantError):
                await device.set_equalizer_preset(option)
        client.async_write.assert_not_awaited()

    async def test_preset_preflight_rechecks_context_and_does_not_change_mode(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        self.payloads[MODE][P + "currentEQMode"] = "tone"
        with self.assertRaises(HomeAssistantError):
            await device.set_equalizer_preset("JAZZ")
        client.async_write.assert_not_awaited()
        self.rpc.set_sound_mode.assert_not_awaited()

    async def test_failed_readback_does_not_claim_success_or_optimistic_preset(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        client.async_write.side_effect = None
        client.async_write.return_value = OcfWriteResult(True, False, False, 68)
        with self.assertRaises(HomeAssistantError):
            await device.set_equalizer_preset("POP")
        self.assertEqual(device.active_equalizer_preset, "NONE")
        client.async_write.assert_awaited_once()
        self.assertNotIn("active_equalizer_preset", coordinator.state.pending)

    async def test_schema_break_or_permission_loss_never_replays_to_cloud(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        self.assertIsNotNone(device.ocf_equalizer.profile)
        self.assertFalse(device.can_select_equalizer_preset)
        self.payloads[EQ][P + "EQband"] = ["0"] * 6
        with self.assertRaises(HomeAssistantError):
            await device.set_equalizer_preset("POP")
        client.async_write.assert_not_awaited()
        self.cloud.status.refresh.assert_not_awaited()
        client.available = False
        client.async_read.side_effect = OcfError("auth")
        await coordinator.async_refresh_all()
        with self.assertRaises(HomeAssistantError):
            await device.set_equalizer_preset("CUSTOM")

    async def test_unsupported_or_unknown_model_does_not_create_eq_control(self):
        device, coordinator, client = self.prepare()
        self.payloads[EQ][P + "isSupported"] = 0
        await coordinator.async_refresh_all()
        self.assertFalse(device.can_select_equalizer_preset)
        client.async_write.assert_not_awaited()

    async def test_hybrid_failed_eq_write_has_no_cloud_replay(self):
        device, coordinator, client = self.prepare(
            CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS
        )
        await coordinator.async_refresh_all()
        device.set_custom_execution_data = AsyncMock()
        client.async_write.side_effect = OcfError("transport")
        with self.assertRaises(HomeAssistantError):
            await device.set_equalizer_preset("POP")
        client.async_write.assert_awaited_once()
        device.set_custom_execution_data.assert_not_awaited()

    async def test_cloud_only_preserves_legacy_preset_route(self):
        device, coordinator, client = self.prepare(CONTROL_MODE_SMARTTHINGS_CLOUD)
        await coordinator.async_refresh_all()
        device.set_custom_execution_data = AsyncMock()
        await device.set_equalizer_preset("POP")
        device.set_custom_execution_data.assert_awaited_once_with(
            href=EQ, property=P + "EQname", value="POP"
        )
        client.async_write.assert_not_awaited()

    async def test_cloud_selector_still_requires_explicit_option_and_readable_state(self):
        device, coordinator, _client = self.prepare(CONTROL_MODE_SMARTTHINGS_CLOUD)
        await coordinator.async_refresh_all()
        device._SoundbarDevice__enable_eq = True
        device._SoundbarDevice__equalizer_supported = True
        device._SoundbarDevice__supported_eq_presets = ["NONE", "CUSTOM"]
        self.assertTrue(device.can_select_equalizer_preset)
        device._SoundbarDevice__enable_eq = False
        self.assertFalse(device.can_select_equalizer_preset)

    async def test_preflight_requires_new_rpc_power_and_mode_not_cached_values(self):
        for failure in ("power", "mode", "missing", "error"):
            with self.subTest(failure=failure):
                device, coordinator, client = self.prepare()
                await coordinator.async_refresh_all()
                if failure == "power":
                    self.rpc.power_state.return_value = "powerOff"
                elif failure == "mode":
                    self.rpc.sound_mode.return_value = "GAME"
                elif failure == "missing":
                    self.rpc.sound_mode.return_value = None
                else:
                    self.rpc.sound_mode.side_effect = LocalRpcError("offline")
                with self.assertRaises(HomeAssistantError):
                    await device.set_equalizer_preset("POP")
                client.async_write.assert_not_awaited()
