"""Q800F channel/AudioSync discovery, guarded writes and typed readback."""

from copy import deepcopy
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock

import test_coordinator as fixtures
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.samsung_soundbar.const import CONTROL_MODE_LOCAL_ONLY
from custom_components.samsung_soundbar.local_ocf import OcfError, OcfWriteResult
from custom_components.samsung_soundbar.local_rpc import LocalRpcError
from custom_components.samsung_soundbar.ocf_level_profiles import Q800F_LEVELS

P = "x.com.samsung.networkaudio."
CHANNEL = "/sec/networkaudio/channelVolume"
SYNC = "/sec/networkaudio/audiosync"
CENTER = "channel_level_center"
RAW_NAMES = (
    "Spk_Center",
    "Spk_Side",
    "Spk_Wide",
    "Spk_Front_Top",
    "Spk_Rear",
    "Spk_Rear_Top",
    "Spk_Rear_Side",
)


class TestOcfLevels(IsolatedAsyncioTestCase):
    setUp = fixtures.TestCoordinator.setUp
    make_device = fixtures.TestCoordinator.make_device

    def prepare(self, mode=None):
        device, coordinator = (
            self.make_device() if mode is None else self.make_device(mode)
        )
        self.payloads = {
            CHANNEL: {
                P + "isSupported": 1,
                P + "channelVolume": [
                    {
                        "name": name,
                        "value": 4 if name == "Spk_Rear" else 0,
                        "status": -1 if name in ("Spk_Wide", "Spk_Rear_Side") else 1,
                    }
                    for name in RAW_NAMES
                ],
            },
            SYNC: {P + "isSupported": 1, P + "audiosync": 0},
        }
        client = MagicMock()
        client.available = client.validated = True
        client.async_close = AsyncMock()
        client.diagnostics.return_value = {"configured": True, "available": True}
        client.async_read = AsyncMock(
            side_effect=lambda href: deepcopy(self.payloads.get(href, {}))
        )

        async def write(href, payload, *, setting_profile):
            self.assertIs(setting_profile, Q800F_LEVELS[setting_profile.field])
            if href == CHANNEL:
                row = payload[P + "channelVolume"][0]
                next(
                    item
                    for item in self.payloads[CHANNEL][P + "channelVolume"]
                    if item["name"] == row["name"]
                )["value"] = row["value"]
            else:
                self.payloads[href].update(payload)
            return OcfWriteResult(True, True, False, 68)

        client.async_write = AsyncMock(side_effect=write)
        coordinator.configure_ocf(client)
        return device, coordinator, client

    async def test_only_verified_supported_profiles_are_discovered(self):
        device, coordinator, _client = self.prepare()
        self.assertIsNone(device.ocf_level_profile(CENTER))
        await coordinator.async_refresh_all()
        for field in (
            CENTER,
            "channel_level_side",
            "channel_level_frontTop",
            "channel_level_rear",
            "channel_level_rearTop",
            "audio_sync",
        ):
            with self.subTest(field=field):
                self.assertIs(device.ocf_level_profile(field), Q800F_LEVELS[field])
                self.assertTrue(device.ocf_level_available(field))
                self.assertIs(type(device.ocf_level_value(field)), int)
        self.assertEqual(device.ocf_level_value("channel_level_rear"), 4)
        for field in ("channel_level_wide", "channel_level_rearSide", "unknown"):
            self.assertIsNone(device.ocf_level_profile(field))
            self.assertIsNone(device.ocf_level_value(field))
            self.assertFalse(device.ocf_level_available(field))

    async def test_model_or_rpc_identity_required_and_local_only_never_grants(self):
        for failure in ("identity", "unvalidated", "local_only"):
            with self.subTest(failure=failure):
                device, coordinator, client = self.prepare(
                    CONTROL_MODE_LOCAL_ONLY if failure == "local_only" else None
                )
                self.rpc.status.return_value = {
                    **fixtures.LOCAL_STATUS,
                    "identifier": "OTHER"
                    if failure == "identity"
                    else "22_AV_HW-Q800F",
                }
                client.validated = failure != "unvalidated"
                await coordinator.async_refresh_all()
                self.assertIsNone(device.ocf_level_profile(CENTER))
                self.assertIsNone(device.ocf_level_profile("audio_sync"))

    async def test_model_identity_alone_can_authorize_verified_schema(self):
        device, coordinator, _client = self.prepare()
        self.local_device.status.ocf_model_number = "HW-Q800F"
        self.rpc.status.return_value = {**fixtures.LOCAL_STATUS, "identifier": "OTHER"}
        await coordinator.async_refresh_all()
        self.assertIsNotNone(device.ocf_level_profile(CENTER))

    async def test_no_profile_without_schema_support_status_and_typed_in_range_target(
        self,
    ):
        for failure in ("schema", "support", "status", "missing", "type", "range"):
            with self.subTest(failure=failure):
                device, coordinator, _client = self.prepare()
                rows = self.payloads[CHANNEL][P + "channelVolume"]
                if failure == "schema":
                    rows[0]["name"] = "center"
                elif failure == "support":
                    self.payloads[CHANNEL].pop(P + "isSupported")
                elif failure == "status":
                    rows[0]["status"] = 0
                elif failure == "missing":
                    rows.pop(0)
                elif failure == "type":
                    rows[0]["value"] = True
                else:
                    rows[0]["value"] = 7
                await coordinator.async_refresh_all()
                self.assertIsNone(device.ocf_level_profile(CENTER))
                self.assertIsNone(device.ocf_level_value(CENTER))

    async def test_sync_requires_explicit_support_and_typed_in_range_target(self):
        for support, value in (
            (None, 0),
            (0, 0),
            (True, 0),
            (1, True),
            (1, 0.0),
            (1, -1),
            (1, 1),
            (1, 26),
            (1, 301),
        ):
            with self.subTest(support=support, value=value):
                device, coordinator, _client = self.prepare()
                self.payloads[SYNC] = {
                    P + "isSupported": support,
                    P + "audiosync": value,
                }
                await coordinator.async_refresh_all()
                self.assertIsNone(device.ocf_level_profile("audio_sync"))
                self.assertIsNone(device.ocf_level_value("audio_sync"))

    async def test_profiles_survive_staleness_auth_loss_and_detach_until_reload(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        profiles = {
            field: device.ocf_level_profile(field) for field in (CENTER, "audio_sync")
        }
        self.now += 121
        for field, profile in profiles.items():
            self.assertIs(device.ocf_level_profile(field), profile)
            self.assertIsNone(device.ocf_level_value(field))
            self.assertFalse(device.ocf_level_available(field))
        client.validated = client.available = False
        for field, profile in profiles.items():
            self.assertIs(device.ocf_level_profile(field), profile)
            self.assertFalse(device.ocf_level_available(field))
        await coordinator.async_detach_ocf()
        for field, profile in profiles.items():
            self.assertIs(device.ocf_level_profile(field), profile)
            self.assertIsNone(device.ocf_level_value(field))
            self.assertFalse(device.ocf_level_available(field))

    async def test_new_profiles_cannot_be_established_from_stale_or_superseded_fields(
        self,
    ):
        for failure in ("stale", "superseded"):
            with self.subTest(failure=failure):
                device, coordinator, _client = self.prepare()
                await coordinator.async_refresh_all()
                if failure == "stale":
                    self.now += 121
                else:
                    self.now += 1
                    coordinator.state.begin_write((CENTER, "audio_sync"), source="ocf")
                self.assertIsNone(device.ocf_level_profile(CENTER))
                self.assertIsNone(device.ocf_level_profile("audio_sync"))

    async def test_cached_profiles_do_not_mask_schema_or_support_loss(self):
        device, coordinator, _client = self.prepare()
        await coordinator.async_refresh_all()
        self.assertIsNotNone(device.ocf_level_profile(CENTER))
        self.assertIsNotNone(device.ocf_level_profile("audio_sync"))
        self.payloads[CHANNEL][P + "channelVolume"][0]["status"] = -1
        self.payloads[SYNC][P + "isSupported"] = 0
        await coordinator.async_refresh_all()
        for field in (CENTER, "audio_sync"):
            self.assertIsNotNone(device.ocf_level_profile(field))
            self.assertIsNone(device.ocf_level_value(field))
            self.assertFalse(device.ocf_level_available(field))

    async def test_cached_profile_never_authorizes_a_changed_model_identity(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        profile = device.ocf_level_profile(CENTER)
        coordinator.receive("rpc", {"local_identifier": "OTHER"})
        self.assertIs(device.ocf_level_profile(CENTER), profile)
        self.assertFalse(device.ocf_level_available(CENTER))
        with self.assertRaises(HomeAssistantError):
            await device.set_ocf_level(CENTER, 1)
        client.async_write.assert_not_awaited()

    async def test_fresh_target_does_not_hide_stale_support_or_status(self):
        for stale in (
            "channel_schema_valid",
            "channel_status_center",
            "audio_sync_supported",
        ):
            with self.subTest(stale=stale):
                device, coordinator, _client = self.prepare()
                self.now = 1
                await coordinator.async_refresh_all()
                field = "audio_sync" if stale == "audio_sync_supported" else CENTER
                profile = device.ocf_level_profile(field)
                self.now += 121
                for key in (
                    field,
                    "channel_schema_valid",
                    "channel_status_center",
                    "audio_sync_supported",
                ):
                    if key != stale:
                        coordinator.receive(
                            "ocf",
                            {
                                key: 0
                                if key == field
                                else 1
                                if key.startswith("channel_status")
                                else True
                            },
                        )
                self.assertIs(device.ocf_level_profile(field), profile)
                self.assertIsNone(device.ocf_level_value(field))
                self.assertFalse(device.ocf_level_available(field))

    async def test_failed_resource_or_auth_hides_fresh_cached_values(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        for field in (CENTER, "audio_sync"):
            self.assertIsNotNone(device.ocf_level_profile(field))
        client.async_read.side_effect = OcfError("malformed")
        await coordinator.async_refresh_all()
        for field in (CENTER, "audio_sync"):
            self.assertIsNotNone(device.ocf_level_profile(field))
            self.assertIsNone(device.ocf_level_value(field))
            self.assertFalse(device.ocf_level_available(field))

    async def test_exact_target_payload_and_readback_keep_all_neighbors_unsent(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        before = deepcopy(self.payloads[CHANNEL][P + "channelVolume"])
        client.async_read.reset_mock()
        await device.set_ocf_level(CENTER, 1)
        self.assertEqual(
            client.async_write.call_args.args,
            (CHANNEL, {P + "channelVolume": [{"name": "Spk_Center", "value": 1}]}),
        )
        self.assertEqual(self.payloads[CHANNEL][P + "channelVolume"][1:], before[1:])
        self.assertEqual(device.ocf_level_value(CENTER), 1)
        self.assertEqual(client.async_read.await_count, 2)
        self.rpc.power_state.assert_awaited_once()
        self.rpc.sound_mode.assert_not_awaited()
        self.rpc.power_on.assert_not_awaited()
        self.rpc.power_off.assert_not_awaited()
        self.rpc.set_sound_mode.assert_not_awaited()
        self.assertNotIn(CENTER, coordinator.state.pending)

    async def test_every_granted_channel_uses_its_actual_wire_name(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        for field, wire in (
            ("channel_level_side", "Spk_Side"),
            ("channel_level_frontTop", "Spk_Front_Top"),
            ("channel_level_rear", "Spk_Rear"),
            ("channel_level_rearTop", "Spk_Rear_Top"),
        ):
            with self.subTest(field=field):
                await device.set_ocf_level(field, -2)
                self.assertEqual(
                    client.async_write.call_args.args,
                    (CHANNEL, {P + "channelVolume": [{"name": wire, "value": -2}]}),
                )
                self.assertEqual(device.ocf_level_value(field), -2)

    async def test_sync_uses_raw_integer_milliseconds_without_scaling_or_neighbors(
        self,
    ):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        before = deepcopy(self.payloads[CHANNEL])
        await device.set_ocf_level("audio_sync", 25.0)
        self.assertEqual(
            client.async_write.call_args.args, (SYNC, {P + "audiosync": 25})
        )
        self.assertIs(type(client.async_write.call_args.args[1][P + "audiosync"]), int)
        self.assertEqual(device.ocf_level_value("audio_sync"), 25)
        self.assertEqual(self.payloads[CHANNEL], before)

    async def test_sync_accepts_the_verified_25_ms_grid_without_quantization(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        for value in range(0, 301, 25):
            with self.subTest(value=value):
                await device.set_ocf_level("audio_sync", value)
                self.assertEqual(
                    client.async_write.call_args.args,
                    (SYNC, {P + "audiosync": value}),
                )
                self.assertEqual(device.ocf_level_value("audio_sync"), value)

    async def test_invalid_values_and_unknown_profiles_raise_translated_validation_before_io(
        self,
    ):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        client.async_read.reset_mock()
        for field, value in (
            (CENTER, True),
            (CENTER, False),
            (CENTER, -7),
            (CENTER, 7),
            (CENTER, 1.5),
            (CENTER, "1"),
            (CENTER, None),
            (CENTER, float("nan")),
            (CENTER, float("inf")),
            ("audio_sync", -1),
            ("audio_sync", 1),
            ("audio_sync", 24),
            ("audio_sync", 26),
            ("audio_sync", 299),
            ("audio_sync", 301),
            ("audio_sync", 0.5),
            ("channel_level_wide", 0),
            ("unknown", 0),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ServiceValidationError) as error:
                    await device.set_ocf_level(field, value)
                self.assertEqual(error.exception.translation_key, "ocf_level_invalid")
        client.async_read.assert_not_awaited()
        client.async_write.assert_not_awaited()
        self.rpc.power_state.assert_not_awaited()

    async def test_off_or_rpc_outage_is_unavailable_without_any_wake_or_write(self):
        for failure in ("off", "offline", "stale"):
            with self.subTest(failure=failure):
                device, coordinator, client = self.prepare()
                await coordinator.async_refresh_all()
                if failure == "off":
                    coordinator.receive("rpc", {"power": "off"})
                elif failure == "offline":
                    coordinator.transport_changed("rpc", False)
                else:
                    self.now += 121
                with self.assertRaises(HomeAssistantError) as error:
                    await device.set_ocf_level(CENTER, 1)
                self.assertEqual(
                    error.exception.translation_key, "ocf_level_unavailable"
                )
                client.async_write.assert_not_awaited()
                self.rpc.power_on.assert_not_awaited()

    async def test_preflight_requires_new_rpc_power_not_cached_on(self):
        for failure in ("off", "missing", "error"):
            with self.subTest(failure=failure):
                device, coordinator, client = self.prepare()
                await coordinator.async_refresh_all()
                self.rpc.power_state.side_effect = None
                if failure == "off":
                    self.rpc.power_state.return_value = "powerOff"
                elif failure == "missing":
                    self.rpc.power_state.return_value = None
                else:
                    self.rpc.power_state.side_effect = LocalRpcError("offline")
                with self.assertRaises(HomeAssistantError):
                    await device.set_ocf_level(CENTER, 1)
                client.async_write.assert_not_awaited()
                self.rpc.power_on.assert_not_awaited()

    async def test_preflight_requires_new_schema_status_target_and_audio_support(self):
        for failure in ("schema", "status", "target", "sync_support", "sync_target"):
            with self.subTest(failure=failure):
                device, coordinator, client = self.prepare()
                self.rpc.power_state.side_effect = None
                self.rpc.power_state.return_value = "powerOn"
                await coordinator.async_refresh_all()
                client.async_read.reset_mock()
                if failure == "schema":
                    self.payloads[CHANNEL][P + "isSupported"] = 0
                elif failure == "status":
                    self.payloads[CHANNEL][P + "channelVolume"][0]["status"] = 0
                elif failure == "target":
                    self.payloads[CHANNEL][P + "channelVolume"].pop(0)
                elif failure == "sync_support":
                    self.payloads[SYNC].pop(P + "isSupported")
                else:
                    self.payloads[SYNC].pop(P + "audiosync")
                with self.assertRaises(HomeAssistantError):
                    await device.set_ocf_level(
                        "audio_sync" if failure.startswith("sync") else CENTER,
                        25 if failure.startswith("sync") else 1,
                    )
                client.async_read.assert_awaited_once()
                client.async_write.assert_not_awaited()

    async def test_unavailable_center_preflight_preserves_healthy_side_without_post(
        self,
    ):
        for failure in ("status", "missing"):
            with self.subTest(failure=failure):
                device, coordinator, client = self.prepare()
                await coordinator.async_refresh_all()
                self.assertTrue(device.ocf_level_available(CENTER))
                self.assertTrue(device.ocf_level_available("channel_level_side"))
                rows = self.payloads[CHANNEL][P + "channelVolume"]
                if failure == "status":
                    rows[0]["status"] = 0
                else:
                    rows.pop(0)
                with self.assertRaises(HomeAssistantError):
                    await device.set_ocf_level(CENTER, 1)
                client.async_write.assert_not_awaited()
                self.assertIsNone(device.ocf_level_value(CENTER))
                self.assertFalse(device.ocf_level_available(CENTER))
                self.assertEqual(device.ocf_level_value("channel_level_side"), 0)
                self.assertTrue(device.ocf_level_available("channel_level_side"))

    async def test_missing_required_tone_invalidates_only_omitted_field(self):
        device, coordinator, client = self.prepare()
        tone = "/sec/networkaudio/tone"
        self.payloads[tone] = {P + "bass": 0, P + "treble": 1}
        await coordinator.async_refresh_all()
        self.assertTrue(device.tone_control_available("bass_level"))
        self.assertTrue(device.tone_control_available("treble_level"))
        self.payloads[tone].pop(P + "bass")
        with self.assertRaises(HomeAssistantError):
            await device.set_tone_level("bass_level", 1)
        client.async_write.assert_not_awaited()
        self.assertIsNone(device.tone_level("bass_level"))
        self.assertFalse(device.tone_control_available("bass_level"))
        self.assertEqual(device.tone_level("treble_level"), 1)
        self.assertTrue(device.tone_control_available("treble_level"))

    async def test_preflight_transport_and_protocol_errors_still_fail_channel_resource(
        self,
    ):
        for kind in ("transport", "malformed", "permission"):
            with self.subTest(kind=kind):
                device, coordinator, client = self.prepare()
                await coordinator.async_refresh_all()
                self.assertTrue(device.ocf_level_available("channel_level_side"))
                client.async_read.side_effect = OcfError(kind)
                with self.assertRaises(HomeAssistantError):
                    await device.set_ocf_level(CENTER, 1)
                client.async_write.assert_not_awaited()
                self.assertFalse(device.ocf_level_available(CENTER))
                self.assertFalse(device.ocf_level_available("channel_level_side"))

    async def test_readback_mismatch_is_never_optimistic_success_or_retry(self):
        for matches in (False, True):
            with self.subTest(matches=matches):
                device, coordinator, client = self.prepare()
                await coordinator.async_refresh_all()
                client.async_write.side_effect = None
                client.async_write.return_value = OcfWriteResult(
                    True, matches, False, 68
                )
                with self.assertRaises(HomeAssistantError) as error:
                    await device.set_ocf_level(CENTER, 1)
                self.assertEqual(
                    error.exception.translation_key, "ocf_level_unconfirmed"
                )
                self.assertEqual(device.ocf_level_value(CENTER), 0)
                self.assertNotIn(CENTER, coordinator.state.pending)
                client.async_write.assert_awaited_once()

    async def test_ambiguous_transport_failure_does_not_retry_or_change_power(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        client.async_write.side_effect = OcfError("transport")
        with self.assertRaises(HomeAssistantError) as error:
            await device.set_ocf_level("audio_sync", 25)
        self.assertEqual(error.exception.translation_key, "ocf_level_unconfirmed")
        client.async_write.assert_awaited_once()
        self.rpc.power_on.assert_not_awaited()
        self.rpc.power_off.assert_not_awaited()

    async def test_backend_neighbor_change_is_unconfirmed_without_rollback(self):
        device, coordinator, client = self.prepare()
        await coordinator.async_refresh_all()
        write = client.async_write.side_effect

        async def corrupt_neighbor(*args, **kwargs):
            result = await write(*args, **kwargs)
            self.payloads[CHANNEL][P + "channelVolume"][1]["value"] = 3
            return result

        client.async_write.side_effect = corrupt_neighbor
        with self.assertRaises(HomeAssistantError) as error:
            await device.set_ocf_level(CENTER, 1)
        self.assertEqual(error.exception.translation_key, "ocf_level_unconfirmed")
        self.assertEqual(device.ocf_level_value(CENTER), 1)
        self.assertEqual(device.ocf_level_value("channel_level_side"), 3)
        client.async_write.assert_awaited_once()

    async def test_post_write_malformed_or_missing_target_cannot_reuse_matching_cache(
        self,
    ):
        for failure in (
            "channel_missing",
            "channel_bool",
            "sync_missing",
            "sync_float",
            "sync_support",
        ):
            with self.subTest(failure=failure):
                device, coordinator, client = self.prepare()
                await coordinator.async_refresh_all()
                write = client.async_write.side_effect

                async def corrupt_readback(
                    *args, write=write, failure=failure, **kwargs
                ):
                    result = await write(*args, **kwargs)
                    if failure == "channel_missing":
                        self.payloads[CHANNEL][P + "channelVolume"].pop(0)
                    elif failure == "channel_bool":
                        self.payloads[CHANNEL][P + "channelVolume"][0]["value"] = False
                    elif failure == "sync_missing":
                        self.payloads[SYNC].pop(P + "audiosync")
                    elif failure == "sync_float":
                        self.payloads[SYNC][P + "audiosync"] = 0.0
                    else:
                        self.payloads[SYNC].pop(P + "isSupported")
                    return result

                client.async_write.side_effect = corrupt_readback
                field = CENTER if failure.startswith("channel") else "audio_sync"
                with self.assertRaises(HomeAssistantError) as error:
                    await device.set_ocf_level(field, 0)
                self.assertEqual(
                    error.exception.translation_key, "ocf_level_unconfirmed"
                )
                self.assertFalse(device.ocf_level_available(field))
                self.assertIsNone(device.ocf_level_value(field))
                client.async_write.assert_awaited_once()
