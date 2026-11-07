"""Read-only text polling keeps RPC core authoritative and rejects stale context."""

import asyncio
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

import test_coordinator as rpc_fixtures
import test_ocf_state as fixtures

from custom_components.samsung_soundbar.local_ocf import OcfError
from custom_components.samsung_soundbar.ocf_state import POLL_HREFS

TRACK = "/sec/networkaudio/av/audioTrackData"
TEXT = {"title": "Private Track", "artist": "Private Artist", "album": "Private Album"}
AIRPLAY = {"power": "on", "input_source": "WIFI", "sound_from_detail_name": "AirPlay"}


class TestTrackCoordinator(IsolatedAsyncioTestCase):
    setUp = fixtures.TestOcfCoordinator.setUp
    make_device = fixtures.TestOcfCoordinator.make_device
    attach_ocf = fixtures.TestOcfCoordinator.attach_ocf

    async def prepare(self):
        self.rpc.status.return_value = {
            **rpc_fixtures.LOCAL_STATUS,
            "input_source": "WIFI_AIRPLAY",
        }
        self.rpc.input_source.return_value = "WIFI_AIRPLAY"
        device, coordinator = self.make_device()
        client = self.attach_ocf(coordinator)
        client.validated = True
        self.payloads[TRACK] = dict(TEXT)
        await coordinator.async_refresh_all()
        return device, coordinator, client

    async def test_text_is_owned_by_ocf_without_changing_core_or_commands(self):
        device, coordinator, client = await self.prepare()
        self.assertEqual(coordinator.media_text_value("media_title"), TEXT["title"])
        self.assertEqual(coordinator.state.resolve("media_title").source, "ocf")
        self.assertEqual(device.state, "on")
        self.assertEqual(device.volume_level, 0.08)
        self.assertEqual(device.sound_from_detail_name, "AirPlay")
        self.assertIsNone(coordinator.media_text_value("media_duration"))
        client.async_write.assert_not_awaited()
        self.rpc.power_on.assert_not_awaited()
        self.rpc.power_off.assert_not_awaited()

    async def test_metadata_cadence_does_not_refresh_or_postpone_audio_settings(self):
        _device, coordinator, client = await self.prepare()
        due_audio = coordinator._next_ocf
        before = coordinator.state.records["ocf"]["woofer_level"]
        client.async_read.reset_mock()
        self.now = 3
        await coordinator.async_refresh()
        await coordinator._async_wait_ocf_poll()
        client.async_read.assert_not_awaited()
        self.now = 7
        self.payloads[TRACK] = {"title": "New Track"}
        await coordinator.async_refresh()
        await coordinator._async_wait_ocf_poll()
        client.async_read.assert_awaited_once_with(TRACK)
        self.assertEqual(coordinator._next_ocf, due_audio)
        self.assertIs(coordinator.state.records["ocf"]["woofer_level"], before)
        self.assertEqual(coordinator.media_text_value("media_title"), "New Track")
        self.assertIsNone(coordinator.media_text_value("media_artist"))
        self.assertIsNone(coordinator.media_text_value("media_album_name"))
        self.now = 32
        await coordinator.async_refresh()
        await coordinator._async_wait_ocf_poll()
        self.assertTrue(
            set(POLL_HREFS)
            <= {call.args[0] for call in client.async_read.await_args_list}
        )

    async def test_malformed_empty_or_failed_text_clears_only_media(self):
        _device, coordinator, client = await self.prepare()
        for payload in (
            {},
            {"title": ""},
            {"title": True},
            {"title": "Good", "artist": None},
        ):
            self.payloads[TRACK] = payload
            await coordinator.async_refresh_all()
            self.assertEqual(
                coordinator.media_text_value("media_title"),
                "Good" if payload.get("title") == "Good" else None,
            )
            self.assertIsNone(coordinator.media_text_value("media_artist"))
        original = client.async_read.side_effect

        async def unavailable(href):
            if href == TRACK:
                raise OcfError("permission")
            return original(href)

        client.async_read.side_effect = unavailable
        await coordinator.async_refresh_all()
        self.assertIsNone(coordinator.media_text_value("media_title"))
        self.assertTrue(coordinator.ocf_field_available("woofer_level"))
        self.assertTrue(coordinator.state.snapshot().local_available)

    async def test_crossed_a_to_b_to_a_read_cannot_repopulate_track(self):
        _device, coordinator, client = await self.prepare()
        started, release = asyncio.Event(), asyncio.Event()
        original = client.async_read.side_effect

        async def delayed(href):
            if href == TRACK:
                started.set()
                await release.wait()
                return {"title": "Old Delayed Track"}
            return original(href)

        client.async_read.side_effect = delayed
        refreshing = asyncio.create_task(coordinator.async_refresh_all())
        try:
            await asyncio.wait_for(started.wait(), 1)
            coordinator.receive(
                "rpc",
                {"input_source": "BT", "sound_from_detail_name": "External Device"},
            )
            coordinator.receive("rpc", AIRPLAY)
            release.set()
            await refreshing
            self.assertIsNone(coordinator.media_text_value("media_title"))
        finally:
            release.set()
            if not refreshing.done():
                refreshing.cancel()
            await asyncio.gather(refreshing, return_exceptions=True)

    async def test_slow_text_read_does_not_block_fast_rpc_or_replay_on_cancel(self):
        device, coordinator, client = await self.prepare()
        started = asyncio.Event()
        original = client.async_read.side_effect

        async def delayed(href):
            if href == TRACK:
                started.set()
                await asyncio.Event().wait()
            return original(href)

        client.async_read.side_effect = delayed
        coordinator._next_media = 0
        coordinator._schedule_ocf()
        await asyncio.wait_for(started.wait(), 1)
        self.now = 3
        self.rpc.volume.return_value = 9
        await asyncio.wait_for(coordinator.async_refresh(), 1)
        self.assertEqual(device.volume_level, 0.09)
        await coordinator.async_detach_ocf()
        self.assertIsNone(coordinator.media_text_value("media_title"))
        client.async_write.assert_not_awaited()

    async def test_ephemeral_media_diagnostics_cannot_refresh_or_inject_ui_metadata(
        self,
    ):
        device, coordinator, _client = await self.prepare()
        original = coordinator.state.records["ocf"]["media_title"]
        self.payloads[TRACK] = {"title": "Diagnostic Only", "artist": "Hidden Artist"}
        response = await coordinator.async_ocf_snapshot((TRACK,))
        self.assertNotIn("Diagnostic Only", repr(response))
        self.assertNotIn(TEXT["title"], repr(response))
        self.assertIs(coordinator.state.records["ocf"]["media_title"], original)
        self.assertEqual(coordinator.media_text_value("media_title"), TEXT["title"])
        self.assertNotIn(TEXT["title"], repr(coordinator.diagnostics()))
        self.assertNotIn(TEXT["title"], repr(device.retrieve_data))

    async def test_rpc_loss_and_source_commands_clear_metadata_before_io(self):
        _device, coordinator, _client = await self.prepare()
        coordinator.transport_changed("rpc", False)
        self.assertIsNone(coordinator.media_text_value("media_title"))
        coordinator.transport_changed("rpc", True)
        self.assertIsNone(coordinator.media_text_value("media_title"))
        await coordinator.async_refresh_all()
        self.assertEqual(coordinator.media_text_value("media_title"), TEXT["title"])
        coordinator.state.begin_write({"input_source"}, source="rpc")
        self.assertIsNone(coordinator.media_text_value("media_title"))

    async def test_refresh_all_during_text_only_sweep_still_refreshes_audio(self):
        _device, coordinator, client = await self.prepare()
        started, release = asyncio.Event(), asyncio.Event()
        original = client.async_read.side_effect

        async def delayed(href):
            if href == TRACK and not started.is_set():
                started.set()
                await release.wait()
            return original(href)

        client.async_read = AsyncMock(side_effect=delayed)
        coordinator._next_media = 0
        coordinator._schedule_ocf()
        await asyncio.wait_for(started.wait(), 1)
        refreshing = asyncio.create_task(coordinator.async_refresh_all())
        try:
            await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(refreshing, 1)
            self.assertTrue(
                set(POLL_HREFS)
                <= {call.args[0] for call in client.async_read.await_args_list}
            )
        finally:
            release.set()
            if not refreshing.done():
                refreshing.cancel()
            await asyncio.gather(refreshing, return_exceptions=True)
