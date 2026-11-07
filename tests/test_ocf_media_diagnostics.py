"""Opt-in media GETs must not become polling, commands or trusted UI state."""

import asyncio
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

import test_ocf_state as fixtures

from custom_components.samsung_soundbar.const import CONTROL_MODE_LOCAL_ONLY
from custom_components.samsung_soundbar.local_ocf import READ_HREFS, OcfError
from custom_components.samsung_soundbar.ocf_media import MEDIA_HREFS
from custom_components.samsung_soundbar.ocf_state import POLL_HREFS

BASE = "/sec/networkaudio/"
PLAYBACK = BASE + "playback"
TRACK = BASE + "av/audioTrackData"
MEDIA = (PLAYBACK, TRACK)


class TestMediaDiagnostics(IsolatedAsyncioTestCase):
    setUp = fixtures.TestOcfCoordinator.setUp
    make_device = fixtures.TestOcfCoordinator.make_device
    attach_ocf = fixtures.TestOcfCoordinator.attach_ocf

    def test_fixed_media_paths_use_existing_transport_but_never_background_poll(self):
        self.assertEqual(len(MEDIA_HREFS), 8)
        self.assertTrue(set(MEDIA_HREFS) <= READ_HREFS)
        self.assertTrue(set(MEDIA_HREFS).isdisjoint(POLL_HREFS))

    async def test_explicit_media_read_is_ephemeral_and_never_overrides_power(self):
        device, coordinator = self.make_device()
        client = self.attach_ocf(coordinator)
        await coordinator.async_refresh_all()
        before = dict(coordinator.state.records["ocf"])
        self.payloads[PLAYBACK] = {
            "x.com.samsung.networkaudio.status": "play",
            "power": "off",
        }
        self.payloads[TRACK] = {
            "title": "Private track",
            "thumbnail": "http://private.invalid/?token=SECRET",
            "duration": 123,
        }
        client.async_read.reset_mock()
        result = await coordinator.async_ocf_snapshot(MEDIA)
        self.assertEqual(
            [call.args[0] for call in client.async_read.await_args_list], list(MEDIA)
        )
        self.assertEqual(
            result["media"]["resources"][PLAYBACK]["fields"]["status"],
            {"state": "value", "value": "play"},
        )
        self.assertEqual(
            result["media"]["resources"][TRACK]["fields"]["title"], {"state": "present"}
        )
        self.assertTrue(result["media"]["read_only"])
        self.assertFalse(result["media"]["controls_verified"])
        self.assertFalse(result["media"]["units_verified"])
        self.assertEqual(coordinator.state.records["ocf"], before)
        self.assertEqual(device.state, "on")
        self.assertIsNone(device.media_title)
        self.assertNotIn("Private track", repr(result))
        self.assertNotIn("SECRET", repr(result))
        client.async_write.assert_not_awaited()
        self.rpc.power_on.assert_not_awaited()
        self.rpc.power_off.assert_not_awaited()

    async def test_default_sweep_and_cache_do_not_read_media_or_cache_the_result(self):
        _device, coordinator = self.make_device()
        client = self.attach_ocf(coordinator)
        await coordinator.async_refresh_all()
        client.async_read.reset_mock()
        result = await coordinator.async_ocf_snapshot()
        self.assertNotIn("media", result)
        self.assertEqual(
            [call.args[0] for call in client.async_read.await_args_list],
            list(POLL_HREFS),
        )
        await coordinator.async_ocf_snapshot((PLAYBACK,))
        client.async_read.reset_mock()
        result = await coordinator.async_ocf_snapshot(())
        client.async_read.assert_not_awaited()
        self.assertNotIn("media", result)
        await coordinator.async_refresh_all()
        self.assertTrue(
            all(
                call.args[0] in POLL_HREFS for call in client.async_read.await_args_list
            )
        )

    async def test_media_permission_failure_is_isolated_but_identity_stops_reads(self):
        _device, coordinator = self.make_device()
        client = self.attach_ocf(coordinator)
        await coordinator.async_refresh_all()
        for kind, expected_reads in (("permission", 2), ("identity", 1)):
            with self.subTest(kind=kind):

                async def read(href, kind=kind):
                    if href == PLAYBACK:
                        raise OcfError(kind)
                    return {"title": ""}

                client.async_read = AsyncMock(side_effect=read)
                result = await coordinator.async_ocf_snapshot(MEDIA)
                self.assertEqual(client.async_read.await_count, expected_reads)
                self.assertEqual(
                    result["media"]["resources"][PLAYBACK]["last_error"], kind
                )
                self.assertEqual(
                    result["media"]["resources"][TRACK]["last_error"],
                    None if kind == "permission" else "not_read",
                )
                self.assertIsNone(
                    coordinator._ocf_resources[fixtures.WOOFER]["last_error"]
                )
                if kind == "permission":
                    self.assertTrue(coordinator.ocf_field_available("woofer_level"))

    async def test_rpc_only_rejects_media_without_reading_a_retained_client(self):
        _device, coordinator = self.make_device(CONTROL_MODE_LOCAL_ONLY)
        client = self.attach_ocf(coordinator)
        coordinator.state.use_ocf = False
        result = await coordinator.async_ocf_snapshot(MEDIA)
        client.async_read.assert_not_awaited()
        self.assertEqual(
            result["media"]["resources"][PLAYBACK]["last_error"], "disabled"
        )

    async def test_cancelled_media_read_does_not_read_next_resource_or_write(self):
        _device, coordinator = self.make_device()
        client = self.attach_ocf(coordinator)
        await coordinator.async_refresh_all()
        client.async_read.reset_mock()
        started = asyncio.Event()

        async def read(href):
            started.set()
            await asyncio.Event().wait()

        client.async_read.side_effect = read
        task = asyncio.create_task(coordinator.async_ocf_snapshot(MEDIA))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        client.async_read.assert_awaited_once_with(PLAYBACK)
        client.async_write.assert_not_awaited()
        self.assertFalse(coordinator._write_lock.locked())

    async def test_unknown_path_rejected_before_any_io(self):
        _device, coordinator = self.make_device()
        client = self.attach_ocf(coordinator)
        for href in (
            "/oic/sec/cred",
            BASE + "av/../audioTrackData",
            BASE + "networkInfo",
        ):
            with self.subTest(href=href), self.assertRaises(OcfError):
                await coordinator.async_ocf_snapshot((PLAYBACK, href))
        client.async_read.assert_not_awaited()

    async def test_media_transport_failure_updates_health_without_changing_rpc_or_ttl(
        self,
    ):
        device, coordinator = self.make_device()
        client = self.attach_ocf(coordinator)
        await coordinator.async_refresh_all()
        previous = coordinator.state.records["ocf"]["woofer_level"]

        async def offline(href):
            client.available = False
            raise OcfError("transport")

        client.async_read.side_effect = offline
        result = await coordinator.async_ocf_snapshot((PLAYBACK,))
        self.assertEqual(
            result["media"]["resources"][PLAYBACK]["last_error"], "transport"
        )
        self.assertFalse(coordinator.state.snapshot().ocf_available)
        self.assertTrue(device.available)
        self.assertIs(coordinator.state.records["ocf"]["woofer_level"], previous)

    async def test_missing_credential_explains_media_not_read(self):
        _device, coordinator = self.make_device()
        result = await coordinator.async_ocf_snapshot((PLAYBACK,))
        self.assertEqual(
            result["media"]["resources"][PLAYBACK]["last_error"], "not_configured"
        )
