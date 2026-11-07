"""Async OCF adapter tests; mock only the proven synchronous protocol library."""

import asyncio
import importlib
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import MagicMock, patch

import cbor2
from test_ocf_credentials import CLIENT, PSK, TARGET, bundle

WOOFER = "/sec/networkaudio/woofer"
FIELD = "x.com.samsung.networkaudio.woofer"


class TestLocalOcf(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "custom_components/samsung_soundbar/local_ocf.py"
        )
        self.assertTrue(path.exists(), "OCF adapter is not implemented")
        self.module = importlib.import_module(
            "custom_components.samsung_soundbar.local_ocf"
        )
        credentials = importlib.import_module(
            "custom_components.samsung_soundbar.ocf_credentials"
        )
        self.credential = credentials.parse_credential_bundle(bundle())
        self.level = 1
        self.session = MagicMock()
        self.session.get.side_effect = lambda path, **kwargs: (
            69,
            cbor2.dumps(
                {"di": str(TARGET)} if path == ["oic", "d"] else {FIELD: self.level}
            ),
        )
        self.session.post.return_value = (68, b"")
        self.protocol = SimpleNamespace(
            discover=MagicMock(return_value=SimpleNamespace(ports=(46379,))),
            auth=MagicMock(),
            session=MagicMock(return_value=self.session),
            cancellation=threading.Event,
        )
        patcher = patch.object(
            self.module, "_load_protocol", return_value=self.protocol
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = 0
        self.hass = SimpleNamespace(
            async_add_executor_job=lambda function: asyncio.to_thread(function)
        )
        self.client = self.module.LocalSoundbarOcfClient(
            self.hass, "192.0.2.26", self.credential, clock=lambda: self.now
        )
        self.addAsyncCleanup(self.client.async_close)

    async def test_identity_before_resource_read_and_reuse(self):
        self.assertEqual(await self.client.async_read(WOOFER), {FIELD: 1})
        await self.client.async_read(WOOFER)
        self.protocol.session.assert_called_once()
        self.assertEqual(self.session.get.call_args_list[0].args[0], ["oic", "d"])
        self.protocol.auth.assert_called_once_with(identity=CLIENT.bytes, key=PSK)
        self.session.subscribe.assert_not_called()

    async def test_sensitive_or_unknown_paths_are_rejected_without_network(self):
        for href in (
            "/oic/sec/cred",
            "/WiFiConfResURI",
            "/sec/networkaudio/../networkInfo",
        ):
            with self.assertRaises(self.module.OcfError):
                await self.client.async_read(href)
        self.protocol.discover.assert_not_called()

    async def test_tone_grant_accepts_only_canonical_single_integer_property(self):
        from custom_components.samsung_soundbar.ocf_profiles import (
            Q800F_TONE,
            ToneProfile,
        )

        profile = Q800F_TONE["bass_level"]
        href, field = profile.href, profile.property
        self.session.get.side_effect = lambda path, **kwargs: (
            69,
            cbor2.dumps({"di": str(TARGET)} if path == ["oic", "d"] else {field: -6}),
        )
        result = await self.client.async_write(href, {field: -6}, tone_profile=profile)
        self.assertTrue(result.readback_matches)
        for payload in (
            {field: True},
            {field: 0.5},
            {field: 7},
            {field: -7},
            {field: 0, "other": 1},
        ):
            with self.subTest(payload=payload), self.assertRaises(self.module.OcfError):
                await self.client.async_write(href, payload, tone_profile=profile)
        for grant in (None, ToneProfile(profile.field, profile.property)):
            with self.assertRaises(self.module.OcfError):
                await self.client.async_write(href, {field: 0}, tone_profile=grant)
        with self.assertRaises(self.module.OcfError):
            await self.client.async_write(
                "/sec/networkaudio/eq", {field: 0}, tone_profile=profile
            )
        self.session.post.assert_called_once()

    async def test_eq_grant_accepts_only_verified_preset_command(self):
        from custom_components.samsung_soundbar.ocf_profiles import (
            Q800F_EQ,
            EqualizerProfile,
        )

        prefix = "x.com.samsung.networkaudio."
        payload = {prefix + "EQname": "POP", prefix + "action": "setEQmode"}
        self.session.get.side_effect = lambda path, **kwargs: (
            69,
            cbor2.dumps({"di": str(TARGET)} if path == ["oic", "d"] else payload),
        )
        result = await self.client.async_write(
            Q800F_EQ.href, payload, eq_profile=Q800F_EQ
        )
        self.assertTrue(result.readback_matches)
        for invalid in (
            {prefix + "EQname": "ROCK", prefix + "action": "setEQmode"},
            {prefix + "EQname": "pop", prefix + "action": "setEQmode"},
            {prefix + "EQname": "POP"},
            {prefix + "EQband": ["0"] * 7},
            {**payload, prefix + "EQband": ["0"] * 7},
            {prefix + "EQname": "POP", prefix + "action": "other"},
        ):
            with self.subTest(payload=invalid), self.assertRaises(self.module.OcfError):
                await self.client.async_write(
                    Q800F_EQ.href, invalid, eq_profile=Q800F_EQ
                )
        for profile in (None, EqualizerProfile()):
            with self.assertRaises(self.module.OcfError):
                await self.client.async_write(
                    Q800F_EQ.href, payload, eq_profile=profile
                )
        self.session.post.assert_called_once()

    async def test_mismatched_authenticated_identity_is_not_accepted(self):
        self.session.get.return_value = (69, cbor2.dumps({"di": str(CLIENT)}))
        self.session.get.side_effect = None
        with self.assertRaises(self.module.OcfError) as error:
            await self.client.async_read(WOOFER)
        self.assertEqual(error.exception.kind, "identity")
        self.assertFalse(self.client.available)
        self.session.close.assert_called_once()

    async def test_failed_connect_backs_off_and_rediscovers(self):
        self.session.connect.side_effect = OSError(PSK.hex())
        for _ in range(2):
            with self.assertRaises(self.module.OcfError):
                await self.client.async_connect()
        self.assertEqual(self.protocol.discover.call_count, 1)
        self.now = 100
        self.session.connect.side_effect = None
        self.protocol.discover.return_value.ports = (45001,)
        await self.client.async_connect()
        self.assertEqual(self.protocol.session.call_args.args[1], 45001)

    async def test_ambiguous_ports_are_not_scanned(self):
        self.protocol.discover.return_value.ports = (12345, 46379)
        with self.assertRaises(self.module.OcfError):
            await self.client.async_connect()
        self.protocol.session.assert_not_called()

    async def test_resource_permission_failure_is_not_global_auth_failure(self):
        await self.client.async_connect()
        self.session.get.side_effect = None
        self.session.get.return_value = (131, b"")
        with self.assertRaises(self.module.OcfError) as error:
            await self.client.async_read(WOOFER)
        self.assertEqual(error.exception.kind, "permission")
        self.assertTrue(self.client.available)

    async def test_malformed_payload_is_a_safe_error(self):
        await self.client.async_connect()
        self.session.get.side_effect = None
        self.session.get.return_value = (69, b"broken" + PSK)
        with self.assertRaises(self.module.OcfError) as error:
            await self.client.async_read(WOOFER)
        self.assertNotIn(PSK.hex(), str(error.exception))

    async def test_write_ack_and_independent_readback(self):
        self.level = 0
        result = await self.client.async_write(WOOFER, {FIELD: 0})
        self.assertTrue(result.acknowledged)
        self.assertTrue(result.readback_matches)
        self.assertFalse(result.ambiguous)
        self.session.post.assert_called_once()
        self.assertEqual(cbor2.loads(self.session.post.call_args.args[1]), {FIELD: 0})
        self.assertEqual(
            self.protocol.session.call_args.kwargs["write_max_attempts"], 1
        )

    async def test_write_timeout_never_replays_and_returns_readback(self):
        self.session.post.side_effect = TimeoutError(PSK.hex())
        result = await self.client.async_write(WOOFER, {FIELD: 1})
        self.assertFalse(result.acknowledged)
        self.assertTrue(result.readback_matches)
        self.assertTrue(result.ambiguous)
        self.session.post.assert_called_once()

    async def test_failed_write_readback_invalidates_transport_without_replay(self):
        await self.client.async_connect()
        self.session.get.side_effect = TimeoutError("read failed")
        result = await self.client.async_write(WOOFER, {FIELD: 1})
        self.assertTrue(result.acknowledged)
        self.assertIsNone(result.readback_matches)
        self.assertFalse(self.client.available)
        self.session.post.assert_called_once()

    async def test_candidate_failure_restores_old_credential_and_session(self):
        await self.client.async_connect()
        replacement = type(self.credential)(TARGET, CLIENT, b"a" * 16)
        self.session.connect.side_effect = [OSError("invalid new key"), None]
        with self.assertRaises(self.module.OcfError):
            await self.client.async_validate_candidate(replacement)
        self.assertEqual(await self.client.async_read(WOOFER), {FIELD: 1})
        self.assertEqual(self.protocol.auth.call_args.kwargs["key"], PSK)

    async def test_candidate_can_be_validated_during_retry_backoff(self):
        self.session.connect.side_effect = OSError("offline")
        with self.assertRaises(self.module.OcfError):
            await self.client.async_connect()
        self.session.connect.side_effect = None
        await self.client.async_validate_candidate(self.credential)
        self.assertIsNone(self.client._session)

    async def test_mismatch_and_rejection_are_not_transport_fallbacks(self):
        result = await self.client.async_write(WOOFER, {FIELD: 0})
        self.assertTrue(result.acknowledged)
        self.assertFalse(result.readback_matches)
        self.session.post.return_value = (131, b"")
        result = await self.client.async_write(WOOFER, {FIELD: 0})
        self.assertFalse(result.acknowledged)
        self.assertFalse(result.ambiguous)

    async def test_unverified_writes_rejected(self):
        for href, payload in (
            (WOOFER, {FIELD: -12}),
            (WOOFER, {FIELD: True}),
            ("/sec/networkaudio/advancedaudio", {"nightmode": 1}),
        ):
            with self.assertRaises(self.module.OcfError):
                await self.client.async_write(href, payload)
        self.session.post.assert_not_called()

    async def test_cancellation_drains_connect_and_closes_peer(self):
        started = threading.Event()

        def connect(**kwargs):
            started.set()
            kwargs["cancel"].wait(3)
            raise TimeoutError

        self.session.connect.side_effect = connect
        task = asyncio.create_task(self.client.async_connect())
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.session.close.assert_called_once()
        self.assertFalse(self.client.available)

    async def test_close_is_terminal_and_idempotent(self):
        await self.client.async_connect()
        await self.client.async_close()
        await self.client.async_close()
        self.session.close.assert_called_once()
        with self.assertRaises(self.module.OcfError):
            await self.client.async_read(WOOFER)

    async def test_verified_q800f_profile_allows_actual_negative_db_values(self):
        from custom_components.samsung_soundbar.ocf_profiles import Q800F_WOOFER

        self.level = -7
        result = await self.client.async_write(
            WOOFER, {FIELD: -7}, woofer_profile=Q800F_WOOFER
        )
        self.assertTrue(result.readback_matches)
        self.assertEqual(cbor2.loads(self.session.post.call_args.args[1]), {FIELD: -7})

    async def test_readback_boolean_is_not_integer_zero_confirmation(self):
        self.level = False
        result = await self.client.async_write(WOOFER, {FIELD: 0})
        self.assertFalse(result.readback_matches)

    async def test_delayed_virtual_readback_polls_get_without_repeating_post(self):
        from custom_components.samsung_soundbar.ocf_profiles import Q800F_AUDIO_SWITCHES

        profile = Q800F_AUDIO_SWITCHES["virtual_sound"]
        await self.client.async_connect()
        self.session.get.side_effect = [
            (69, cbor2.dumps({profile.property: 1})),
            (69, cbor2.dumps({profile.property: 0})),
        ]
        with patch.object(self.module, "sleep", return_value=None, create=True):
            result = await self.client.async_write(
                profile.href, {profile.property: 0}, switch_profile=profile
            )
        self.assertTrue(result.readback_matches)
        self.session.post.assert_called_once()

    async def test_audio_profile_rejects_multi_field_bool_or_wrong_case_payload(self):
        from custom_components.samsung_soundbar.ocf_profiles import Q800F_AUDIO_SWITCHES

        profile = Q800F_AUDIO_SWITCHES["moderate_bass"]
        for payload in (
            {profile.property: True},
            {profile.property: 1, "nightmode": 0},
            {"x.com.samsung.networkaudio.moderateBass": 1},
        ):
            with self.assertRaises(self.module.OcfError):
                await self.client.async_write(
                    profile.href, payload, switch_profile=profile
                )
        self.session.post.assert_not_called()

    async def test_cancelled_error_cleanup_keeps_peer_serialized(self):
        started, release = threading.Event(), threading.Event()

        def close():
            started.set()
            release.wait(3)

        self.session.connect.side_effect = OSError("offline")
        self.session.close.side_effect = close
        task = asyncio.create_task(self.client.async_connect())
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        other = asyncio.create_task(self.client.async_connect())
        try:
            await asyncio.sleep(0.02)
            self.assertFalse(task.done())
            self.protocol.session.assert_called_once()
        finally:
            release.set()
            await asyncio.gather(task, other, return_exceptions=True)

    async def test_double_cancellation_never_reuses_unverified_session(self):
        started, release = threading.Event(), threading.Event()

        def identity_read(path, **kwargs):
            if path == ["oic", "d"]:
                started.set()
                release.wait(3)
                return 69, cbor2.dumps({"di": str(CLIENT)})
            return 69, cbor2.dumps({FIELD: 1})

        self.session.get.side_effect = identity_read
        task = asyncio.create_task(self.client.async_connect())
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        write = asyncio.create_task(self.client.async_write(WOOFER, {FIELD: 1}))
        await asyncio.sleep(0.01)
        try:
            self.session.post.assert_not_called()
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            with self.assertRaises(self.module.OcfError):
                await write

    async def test_vendor_logging_and_diagnostics_hide_credentials(self):
        logger = logging.getLogger("smartthings_local.protocol.dtls_session")
        await self.client.async_connect()
        record = logging.LogRecord(
            logger.name,
            logging.ERROR,
            "",
            0,
            "%s %s %s",
            (PSK.hex(), str(TARGET), str(CLIENT)),
            None,
        )
        logger.filter(record)
        text = record.getMessage() + repr(self.client.diagnostics()) + repr(self.client)
        for value in (PSK.hex(), str(TARGET), str(CLIENT)):
            self.assertNotIn(value, text)

    async def test_malformed_vendor_log_and_uppercase_identity_are_private(self):
        logger = logging.getLogger("smartthings_local.protocol.dtls_session")
        await self.client.async_connect()
        malformed = logging.LogRecord(
            logger.name, logging.ERROR, "", 0, "%s %s", (PSK.hex(),), None
        )
        logger.filter(malformed)
        self.assertNotIn(PSK.hex(), malformed.getMessage())
        record = logging.LogRecord(
            logger.name,
            logging.ERROR,
            "",
            0,
            "%s %s",
            (PSK.hex().upper(), str(TARGET).upper()),
            None,
        )
        logger.filter(record)
        self.assertNotIn(PSK.hex().upper(), record.getMessage())


if __name__ == "__main__":
    import unittest

    unittest.main()
