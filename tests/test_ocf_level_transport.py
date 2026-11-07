"""Exact local numeric grants and channel patch/readback safety."""

import asyncio
import copy
import threading
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch

import cbor2
import test_local_ocf as fixtures
from test_ocf_credentials import TARGET

from custom_components.samsung_soundbar.ocf_level_profiles import (
    PREFIX,
    Q800F_AUDIO_SYNC,
    Q800F_CHANNEL_LEVELS,
    LevelProfile,
    level_payload,
    validate_level,
)


class TestLevelProfiles(TestCase):
    def test_sync_rejects_device_quantized_off_grid_values(self):
        for value in (1, 24, 26, 299):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_level(Q800F_AUDIO_SYNC, value)
        for value in (0, 25, 100, 300):
            self.assertEqual(validate_level(Q800F_AUDIO_SYNC, value), value)

    def test_only_canonical_profiles_and_exact_integral_values(self):
        center = Q800F_CHANNEL_LEVELS["channel_level_center"]
        self.assertEqual(
            level_payload(center, 1.0),
            {PREFIX + "channelVolume": [{"name": "Spk_Center", "value": 1}]},
        )
        self.assertEqual(
            level_payload(Q800F_AUDIO_SYNC, 25), {PREFIX + "audiosync": 25}
        )
        for profile in (center, Q800F_AUDIO_SYNC):
            for value in (
                True,
                None,
                "1",
                0.5,
                float("nan"),
                float("inf"),
                profile.minimum - 1,
                profile.maximum + 1,
            ):
                with (
                    self.subTest(field=profile.field, value=value),
                    self.assertRaises(ValueError),
                ):
                    validate_level(profile, value)
        fake = LevelProfile(
            center.field,
            center.href,
            center.property,
            -6,
            6,
            wire_name=center.wire_name,
        )
        with self.assertRaises(ValueError):
            validate_level(fake, 0)


class TestLevelTransport(IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.TestLocalOcf.asyncSetUp

    async def test_cancelled_preflight_cannot_start_a_post(self):
        profile = self.prepare_channels()
        await self.client.async_connect()
        original_get = self.session.get.side_effect
        started, release = threading.Event(), threading.Event()

        def blocking_get(path, **kwargs):
            started.set()
            release.wait(2)
            return original_get(path, **kwargs)

        self.session.get.side_effect = blocking_get
        writing = asyncio.create_task(
            self.client.async_write(
                profile.href, level_payload(profile, 1), setting_profile=profile
            )
        )
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            writing.cancel()
            await asyncio.sleep(0)
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await writing
        self.session.post.assert_not_called()

    def prepare_channels(self):
        self.values = {
            PREFIX + "isSupported": 1,
            PREFIX + "channelVolume": [
                {"name": "Spk_Center", "value": 0, "status": 1},
                {"name": "Spk_Rear", "value": 4, "status": 1, "future_flag": 7},
                {"name": "Spk_Wide", "value": 0, "status": -1},
            ],
            "future_metadata": {"preserve": True},
        }
        self.session.get.side_effect = lambda path, **kwargs: (
            69,
            cbor2.dumps({"di": str(TARGET)} if path == ["oic", "d"] else self.values),
        )

        def post(path, body, **kwargs):
            payload = cbor2.loads(body)
            change = payload[PREFIX + "channelVolume"][0]
            for row in self.values[PREFIX + "channelVolume"]:
                if row["name"] == change["name"]:
                    row["value"] = change["value"]
            return 68, b""

        self.session.post.side_effect = post
        return Q800F_CHANNEL_LEVELS["channel_level_center"]

    async def test_channel_subset_readback_is_confirmed_and_neighbors_are_not_sent(
        self,
    ):
        profile = self.prepare_channels()
        original = copy.deepcopy(self.values)
        payload = level_payload(profile, 1)
        result = await self.client.async_write(
            profile.href, payload, setting_profile=profile
        )
        self.assertTrue(result.readback_matches)
        self.session.post.assert_called_once()
        sent = cbor2.loads(self.session.post.call_args.args[1])
        self.assertEqual(sent, payload)
        self.assertEqual(
            self.values[PREFIX + "channelVolume"][1:],
            original[PREFIX + "channelVolume"][1:],
        )
        self.assertEqual(self.values["future_metadata"], original["future_metadata"])

    async def test_wrong_neighbor_change_cannot_confirm_channel_write(self):
        profile = self.prepare_channels()
        post = self.session.post.side_effect

        def wrong(*args, **kwargs):
            code = post(*args, **kwargs)
            self.values[PREFIX + "channelVolume"][1]["value"] = 5
            return code

        self.session.post.side_effect = wrong
        result = await self.client.async_write(
            profile.href, level_payload(profile, 1), setting_profile=profile
        )
        self.assertFalse(result.readback_matches)
        self.session.post.assert_called_once()

    async def test_unavailable_or_duplicate_target_never_sends_post(self):
        profile = self.prepare_channels()
        for status in (0, -1, True):
            self.values[PREFIX + "channelVolume"][0]["status"] = status
            with self.assertRaises(self.module.OcfError):
                await self.client.async_write(
                    profile.href, level_payload(profile, 1), setting_profile=profile
                )
        self.values[PREFIX + "channelVolume"][0]["status"] = 1
        self.values[PREFIX + "channelVolume"].append(
            dict(self.values[PREFIX + "channelVolume"][0])
        )
        with self.assertRaises(self.module.OcfError):
            await self.client.async_write(
                profile.href, level_payload(profile, 1), setting_profile=profile
            )
        self.session.post.assert_not_called()

    async def test_sync_grant_writes_raw_milliseconds_once(self):
        self.session.get.side_effect = lambda path, **kwargs: (
            69,
            cbor2.dumps(
                {"di": str(TARGET)}
                if path == ["oic", "d"]
                else {PREFIX + "audiosync": 25, PREFIX + "isSupported": 1}
            ),
        )
        profile = Q800F_AUDIO_SYNC
        result = await self.client.async_write(
            profile.href, level_payload(profile, 25), setting_profile=profile
        )
        self.assertTrue(result.readback_matches)
        self.session.post.assert_called_once()

    async def test_channel_timeout_confirms_only_get_and_never_replays(self):
        profile = self.prepare_channels()
        post = self.session.post.side_effect

        def timeout(*args, **kwargs):
            post(*args, **kwargs)
            raise TimeoutError("private")

        self.session.post.side_effect = timeout
        result = await self.client.async_write(
            profile.href, level_payload(profile, 1), setting_profile=profile
        )
        self.assertTrue(result.ambiguous)
        self.assertTrue(result.readback_matches)
        self.assertFalse(result.acknowledged)
        self.session.post.assert_called_once()

    async def test_nested_boolean_cannot_confirm_an_integer_channel(self):
        profile = self.prepare_channels()

        def boolean(*args, **kwargs):
            self.values[PREFIX + "channelVolume"][0]["value"] = True
            return 68, b""

        self.session.post.side_effect = boolean
        with patch.object(self.module, "monotonic", side_effect=(0, 6)):
            result = await self.client.async_write(
                profile.href, level_payload(profile, 1), setting_profile=profile
            )
        self.assertFalse(result.readback_matches)
        self.session.post.assert_called_once()

    async def test_sync_needs_explicit_typed_support_before_post(self):
        profile = Q800F_AUDIO_SYNC
        for support in (None, 0, True, "1"):
            self.session.get.side_effect = lambda path, support=support, **kwargs: (
                69,
                cbor2.dumps(
                    {"di": str(TARGET)}
                    if path == ["oic", "d"]
                    else {PREFIX + "isSupported": support, profile.property: 0}
                ),
            )
            with self.subTest(support=support), self.assertRaises(self.module.OcfError):
                await self.client.async_write(
                    profile.href, level_payload(profile, 25), setting_profile=profile
                )
        self.session.post.assert_not_called()

    async def test_forged_grants_mixed_grants_and_malformed_payloads_are_rejected(self):
        profile = self.prepare_channels()
        payload = level_payload(profile, 1)
        for bad in (
            {profile.property: [{"name": profile.wire_name, "value": True}]},
            {profile.property: [{"name": "Spk_Wide", "value": 1}]},
            {profile.property: [{"name": profile.wire_name, "value": 1, "status": 1}]},
            {profile.property: payload[profile.property] * 2},
            {**payload, "other": 1},
        ):
            with self.subTest(payload=bad), self.assertRaises(self.module.OcfError):
                await self.client.async_write(
                    profile.href, bad, setting_profile=profile
                )
        fake = LevelProfile(
            profile.field,
            profile.href,
            profile.property,
            -6,
            6,
            wire_name=profile.wire_name,
        )
        with self.assertRaises(self.module.OcfError):
            await self.client.async_write(profile.href, payload, setting_profile=fake)
        from custom_components.samsung_soundbar.ocf_profiles import Q800F_WOOFER

        with self.assertRaises(self.module.OcfError):
            await self.client.async_write(
                profile.href,
                payload,
                setting_profile=profile,
                woofer_profile=Q800F_WOOFER,
            )
        self.session.post.assert_not_called()
