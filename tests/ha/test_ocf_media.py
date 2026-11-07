"""Opt-in OCF media diagnostics stay read-only, private and outside shared state."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol

from custom_components.samsung_soundbar.const import (
    CONF_CONTROL_MODE,
    CONTROL_MODE_HYBRID_LOCAL_OCF,
    DOMAIN,
)
from custom_components.samsung_soundbar.local_ocf import OcfError
from custom_components.samsung_soundbar.ocf_state import POLL_HREFS

from . import test_ocf_state as ocf_fixtures
from .test_lifecycle import registered

ocf = ocf_fixtures.ocf
BASE = "/sec/networkaudio/"
PREFIX = "x.com.samsung.networkaudio."
MEDIA_HREFS = tuple(
    BASE + suffix
    for suffix in (
        "soundFrom",
        "playback",
        "musicinfo",
        "av/audioTrackData",
        "av/mediaPlayback",
        "av/mediaTrackControl",
        "av/mediaPlaybackShuffle",
        "av/mediaPlaybackRepeat",
    )
)
PRIVATE = (
    "PRIVATE_TRACK_TITLE",
    "PRIVATE_ARTIST",
    "PRIVATE_ALBUM",
    "https://private.invalid/cover?token=PRIVATE_URL_TOKEN",
    "9fa8438b-bdd2-4d25-b088-c1a04494df9a",
    "PRIVATE_UNKNOWN_FIELD",
    "PRIVATE_SECRET",
)


@pytest.fixture
def media(ocf):
    audio_read = ocf.async_read.side_effect
    payloads = {
        href: {
            PREFIX + "status": "play",
            "duration": 12345,
            "title": PRIVATE[0],
            "artist": PRIVATE[1],
            "album": PRIVATE[2],
            "thumbnail": PRIVATE[3],
            "di": PRIVATE[4],
            "power": "off",
            PREFIX + "woofer": -12,
            PRIVATE[5]: {"secret": PRIVATE[6]},
        }
        for href in MEDIA_HREFS
    }

    async def read(href):
        if href in payloads:
            return deepcopy(payloads[href])
        return audio_read(href)

    ocf.async_read = AsyncMock(side_effect=read)
    return SimpleNamespace(client=ocf, payloads=payloads, read=read)


async def setup(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def dump(hass, data=None):
    return await hass.services.async_call(
        DOMAIN, "dump_ocf_status", data or {}, blocking=True, return_response=True
    )


def read_hrefs(client):
    return [call.args[0] for call in client.async_read.await_args_list]


def assert_media_envelope(response, hrefs):
    assert set(response) == {
        "configured",
        "transport",
        "resources",
        "values",
        "fields",
        "media",
    }
    section = response["media"]
    assert section["read_only"] is True
    assert section["controls_verified"] is False
    assert section["units_verified"] is False
    assert set(section["resources"]) == set(hrefs)
    return section["resources"]


async def test_media_preset_reads_exactly_eight_resources_without_writes(
    hass, entry, media
):
    await setup(hass, entry)
    media.client.async_read.reset_mock()
    response = await dump(hass, {"preset": "media"})
    assert read_hrefs(media.client) == list(MEDIA_HREFS)
    resources = assert_media_envelope(response, MEDIA_HREFS)
    for resource in resources.values():
        assert resource["last_error"] is None
        stamp = datetime.fromisoformat(resource["observed_at"])
        assert stamp.utcoffset() == timedelta(0)
        assert isinstance(resource["fields"], dict)
    media.client.async_write.assert_not_awaited()


async def test_audio_remains_default_and_background_never_reads_media(
    hass, entry, media
):
    await setup(hass, entry)
    assert not set(read_hrefs(media.client)) & set(MEDIA_HREFS)
    media.client.async_read.reset_mock()
    response = await dump(hass)
    assert set(read_hrefs(media.client)) == set(POLL_HREFS)
    assert "media" not in response
    assert response["values"]["woofer_level"] == 1
    media.client.async_read.reset_mock()
    explicit = await dump(hass, {"preset": "audio"})
    assert set(read_hrefs(media.client)) == set(POLL_HREFS)
    assert "media" not in explicit
    media.client.async_read.reset_mock()
    await entry.runtime_data.coordinator.async_refresh_all()
    await hass.async_block_till_done()
    assert not set(read_hrefs(media.client)) & set(MEDIA_HREFS)
    media.client.async_write.assert_not_awaited()


@pytest.mark.parametrize(
    "data,expected",
    [
        ({"preset": "media", "hrefs": []}, []),
        ({"preset": "audio", "hrefs": []}, []),
        ({"preset": "media", "hrefs": [BASE + "woofer"]}, [BASE + "woofer"]),
        ({"hrefs": [MEDIA_HREFS[0]]}, [MEDIA_HREFS[0]]),
        ({"preset": "audio", "hrefs": [MEDIA_HREFS[1]]}, [MEDIA_HREFS[1]]),
        (
            {"hrefs": [BASE + "woofer", MEDIA_HREFS[0]]},
            [BASE + "woofer", MEDIA_HREFS[0]],
        ),
    ],
)
async def test_explicit_hrefs_override_presets_including_empty(
    hass, entry, media, data, expected
):
    await setup(hass, entry)
    media.client.async_read.reset_mock()
    response = await dump(hass, data)
    assert read_hrefs(media.client) == expected
    selected = [href for href in expected if href in MEDIA_HREFS]
    if selected:
        assert_media_envelope(response, selected)
    else:
        assert "media" not in response
    media.client.async_write.assert_not_awaited()


@pytest.mark.parametrize(
    "data",
    [
        {"preset": "all"},
        {"preset": "MEDIA"},
        {"hrefs": ["/oic/sec/cred"]},
        {"hrefs": ["/sec/networkaudio/playback/../woofer"]},
        {"hrefs": ["https://example.invalid/sec/networkaudio/playback"]},
        {"hrefs": [BASE + "unknown"]},
        {"hrefs": [BASE + "av/support"]},
        {"preset": "media", "method": "POST"},
        {"preset": "media", "payload": {"playbackStatus": "play"}},
        {"preset": "media", "write": True},
        {"hrefs": [MEDIA_HREFS[0]] * (len(POLL_HREFS) + 9)},
    ],
)
async def test_service_rejects_unverified_paths_presets_writes_and_oversized_lists(
    hass, entry, media, data
):
    await setup(hass, entry)
    media.client.async_read.reset_mock()
    with pytest.raises(vol.Invalid):
        await dump(hass, data)
    media.client.async_read.assert_not_awaited()
    media.client.async_write.assert_not_awaited()


async def test_diagnostic_allowlist_accepts_audio_plus_all_eight_media_hrefs(
    hass, entry, media
):
    await setup(hass, entry)
    hrefs = [*POLL_HREFS, *MEDIA_HREFS]
    media.client.async_read.reset_mock()
    response = await dump(hass, {"hrefs": hrefs})
    assert read_hrefs(media.client) == hrefs
    assert_media_envelope(response, MEDIA_HREFS)


async def test_unknown_strings_uuid_titles_and_urls_never_escape_response_or_logs(
    hass, entry, media, caplog
):
    await setup(hass, entry)
    caplog.clear()
    response = await dump(hass, {"preset": "media"})
    assert_media_envelope(response, MEDIA_HREFS)
    for marker in PRIVATE:
        assert marker not in repr(response)
        assert marker not in caplog.text
    assert "PRIVATE" not in repr(entry.runtime_data.coordinator.state.records)


@pytest.mark.parametrize("status", ("play", "pause", "stop"))
async def test_status_and_duration_remain_raw_diagnostics_without_inferred_units(
    hass, entry, media, status
):
    await setup(hass, entry)
    playback = BASE + "playback"
    track = BASE + "av/audioTrackData"
    media.payloads[playback][PREFIX + "status"] = status
    response = await dump(hass, {"hrefs": [playback, track]})
    resources = assert_media_envelope(response, (playback, track))
    assert resources[playback]["fields"]["status"] == {
        "state": "value",
        "value": status,
    }
    assert resources[track]["fields"]["duration"] == {"state": "value", "value": 12345}
    assert resources[track]["fields"]["title"] == {"state": "present"}
    assert resources[track]["fields"]["thumbnail"] == {"state": "present"}
    assert entry.runtime_data.device.media_duration is None
    assert entry.runtime_data.device.media_title is None


async def test_media_reads_do_not_refresh_audio_ttl_or_change_rpc_entities(
    hass, entry, transports, media
):
    await setup(hass, entry)
    coordinator = entry.runtime_data.coordinator
    before_records = deepcopy(coordinator.state.records)
    before_ids = registered(hass, entry)
    before_states = {
        entity_id: (
            hass.states.get(entity_id).state,
            dict(hass.states.get(entity_id).attributes),
        )
        for entity_id, _ in before_ids.values()
    }
    before_resources = deepcopy(coordinator._ocf_resources)
    before_pending = deepcopy(coordinator.state.pending)
    before_written = dict(coordinator.state.written_at)
    transports.rpc.reset_mock()
    response = await dump(hass, {"preset": "media"})
    assert_media_envelope(response, MEDIA_HREFS)
    assert coordinator.state.records == before_records
    assert coordinator._ocf_resources == before_resources
    assert coordinator.state.pending == before_pending
    assert coordinator.state.written_at == before_written
    assert registered(hass, entry) == before_ids
    for entity_id, _ in before_ids.values():
        state = hass.states.get(entity_id)
        assert (state.state, dict(state.attributes)) == before_states[entity_id]
    assert coordinator.state.value("power") == "on"
    assert transports.rpc.mock_calls == []
    media.client.async_write.assert_not_awaited()
    assert "media" not in coordinator.ocf_diagnostics()


@pytest.mark.parametrize("kind", ("permission", "unsupported"))
async def test_resource_permission_failures_do_not_hide_other_media(
    hass, entry, media, kind
):
    await setup(hass, entry)

    async def read(href):
        if href == MEDIA_HREFS[1]:
            raise OcfError(kind)
        return await media.read(href)

    media.client.async_read.side_effect = read
    media.client.async_read.reset_mock()
    response = await dump(hass, {"preset": "media"})
    resources = assert_media_envelope(response, MEDIA_HREFS)
    assert read_hrefs(media.client) == list(MEDIA_HREFS)
    assert resources[MEDIA_HREFS[1]]["last_error"] == kind
    assert resources[MEDIA_HREFS[1]]["fields"] == {}
    for href in MEDIA_HREFS[:1] + MEDIA_HREFS[2:]:
        assert resources[href]["last_error"] is None
        assert resources[href]["observed_at"]


@pytest.mark.parametrize("kind", ("auth", "identity", "transport", "closed"))
async def test_session_failure_stops_reads_and_marks_remaining_not_read(
    hass, entry, media, kind
):
    await setup(hass, entry)

    async def read(href):
        if href == MEDIA_HREFS[1]:
            raise OcfError(kind)
        return await media.read(href)

    media.client.async_read.side_effect = read
    media.client.async_read.reset_mock()
    response = await dump(hass, {"preset": "media"})
    resources = assert_media_envelope(response, MEDIA_HREFS)
    assert read_hrefs(media.client) == list(MEDIA_HREFS[:2])
    assert resources[MEDIA_HREFS[0]]["last_error"] is None
    assert resources[MEDIA_HREFS[1]]["last_error"] == kind
    for href in MEDIA_HREFS[2:]:
        assert resources[href]["last_error"] == "not_read"
        assert resources[href]["fields"] == {}
        assert resources[href].get("observed_at") is None
    media.client.async_write.assert_not_awaited()


async def test_rpc_only_media_diagnostics_do_not_load_store_or_create_ocf_session(
    hass, entry
):
    with (
        patch("custom_components.samsung_soundbar.OcfCredentialStore") as store,
        patch("custom_components.samsung_soundbar.LocalSoundbarOcfClient") as factory,
    ):
        await setup(hass, entry)
        response = await dump(hass, {"preset": "media"})
        resources = assert_media_envelope(response, MEDIA_HREFS)
        assert {item["last_error"] for item in resources.values()} == {"disabled"}
        assert entry.runtime_data.coordinator.ocf is None
        store.assert_not_called()
        factory.assert_not_called()


async def test_hybrid_without_credential_reports_not_configured_without_loading_on_demand(
    hass, entry
):
    hass.config_entries.async_update_entry(
        entry,
        version=2,
        minor_version=1,
        options={**entry.options, CONF_CONTROL_MODE: CONTROL_MODE_HYBRID_LOCAL_OCF},
    )
    await setup(hass, entry)
    with (
        patch("custom_components.samsung_soundbar.OcfCredentialStore") as store,
        patch("custom_components.samsung_soundbar.LocalSoundbarOcfClient") as factory,
    ):
        response = await dump(hass, {"preset": "media"})
        resources = assert_media_envelope(response, MEDIA_HREFS)
        assert {item["last_error"] for item in resources.values()} == {"not_configured"}
        store.assert_not_called()
        factory.assert_not_called()


@pytest.mark.parametrize("operation", ("detach", "shutdown"))
async def test_queued_media_reads_recheck_detach_before_touching_client(
    hass, entry, media, operation
):
    await setup(hass, entry)
    coordinator = entry.runtime_data.coordinator
    media.client.async_read.reset_mock()
    async with coordinator._write_lock:
        reading = asyncio.create_task(coordinator.async_ocf_snapshot(MEDIA_HREFS))
        await asyncio.sleep(0)
        removing = asyncio.create_task(
            coordinator.async_detach_ocf()
            if operation == "detach"
            else coordinator.async_shutdown()
        )
        await asyncio.sleep(0)
    response, _ = await asyncio.wait_for(asyncio.gather(reading, removing), 2)
    resources = assert_media_envelope(response, MEDIA_HREFS)
    assert {item["last_error"] for item in resources.values()} == {"closed"}
    media.client.async_read.assert_not_awaited()
    media.client.async_close.assert_awaited_once()
    assert coordinator.ocf is None


async def test_forget_keeps_rpc_entities_and_never_reopens_media_session(
    hass, entry, media
):
    await setup(hass, entry)
    await dump(hass, {"preset": "media"})
    before = registered(hass, entry)
    data = dict(entry.data)
    data.pop("ocf_configured")
    hass.config_entries.async_update_entry(entry, data=data)
    await hass.async_block_till_done()
    assert registered(hass, entry) == before
    media.client.async_close.assert_awaited_once()
    media.client.async_read.reset_mock()
    with (
        patch("custom_components.samsung_soundbar.OcfCredentialStore") as store,
        patch("custom_components.samsung_soundbar.LocalSoundbarOcfClient") as factory,
    ):
        response = await dump(hass, {"preset": "media"})
        resources = assert_media_envelope(response, MEDIA_HREFS)
        assert {item["last_error"] for item in resources.values()} == {"not_configured"}
        store.assert_not_called()
        factory.assert_not_called()
    media.client.async_read.assert_not_awaited()
    assert entry.runtime_data.device.local_available


async def test_mode_change_while_queued_prevents_media_reads(hass, entry, media):
    await setup(hass, entry)
    coordinator = entry.runtime_data.coordinator
    media.client.async_read.reset_mock()
    async with coordinator._write_lock:
        reading = asyncio.create_task(coordinator.async_ocf_snapshot(MEDIA_HREFS))
        await asyncio.sleep(0)
        coordinator.state.use_ocf = False
    try:
        response = await asyncio.wait_for(reading, 2)
        resources = assert_media_envelope(response, MEDIA_HREFS)
        assert {item["last_error"] for item in resources.values()} == {"disabled"}
        media.client.async_read.assert_not_awaited()
    finally:
        coordinator.state.use_ocf = True


async def test_detach_during_media_get_drains_current_read_but_never_reads_next(
    hass, entry, media
):
    await setup(hass, entry)
    coordinator = entry.runtime_data.coordinator
    started, release = asyncio.Event(), asyncio.Event()

    async def read(href):
        assert href == MEDIA_HREFS[0]
        started.set()
        await release.wait()
        assert coordinator.ocf is media.client
        return await media.read(href)

    media.client.async_read = AsyncMock(side_effect=read)
    reading = asyncio.create_task(coordinator.async_ocf_snapshot(MEDIA_HREFS))
    removing = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        removing = asyncio.create_task(coordinator.async_detach_ocf())
        await asyncio.sleep(0)
        assert not removing.done()
        media.client.async_close.assert_not_awaited()
        release.set()
        response, _ = await asyncio.wait_for(asyncio.gather(reading, removing), 2)
        resources = assert_media_envelope(response, MEDIA_HREFS)
        assert read_hrefs(media.client) == [MEDIA_HREFS[0]]
        assert resources[MEDIA_HREFS[0]]["last_error"] is None
        for href in MEDIA_HREFS[1:]:
            assert resources[href]["last_error"] in ("closed", "not_configured")
            assert resources[href]["fields"] == {}
            assert resources[href].get("observed_at") is None
        media.client.async_close.assert_awaited_once()
    finally:
        release.set()
        for task in (reading, removing):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (reading, removing) if task is not None),
            return_exceptions=True,
        )


async def test_cancelled_media_read_propagates_and_does_not_replay(hass, entry, media):
    await setup(hass, entry)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def read(_href):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    media.client.async_read = AsyncMock(side_effect=read)
    reading = asyncio.create_task(
        entry.runtime_data.coordinator.async_ocf_snapshot(MEDIA_HREFS)
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        reading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(reading, 2)
        assert cancelled.is_set()
        assert read_hrefs(media.client) == [MEDIA_HREFS[0]]
        media.client.async_write.assert_not_awaited()
        media.client.async_read.side_effect = media.read
        response = await dump(hass, {"hrefs": []})
        assert "media" not in response
        assert read_hrefs(media.client) == [MEDIA_HREFS[0]]
    finally:
        reading.cancel()
        await asyncio.gather(reading, return_exceptions=True)


async def test_media_diagnostics_release_shared_queue_between_resources(
    hass, entry, media
):
    await setup(hass, entry)
    coordinator = entry.runtime_data.coordinator
    started, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
    order = []

    async def read(href):
        order.append(href)
        if href == MEDIA_HREFS[0]:
            started.set()
            await release.wait()
        return await media.read(href)

    async def other_queue_user():
        queued.set()
        async with coordinator._write_lock:
            order.append("queue_user")

    media.client.async_read = AsyncMock(side_effect=read)
    reading = asyncio.create_task(coordinator.async_ocf_snapshot(MEDIA_HREFS[:2]))
    waiting = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        waiting = asyncio.create_task(other_queue_user())
        await asyncio.wait_for(queued.wait(), 2)
        release.set()
        await asyncio.wait_for(asyncio.gather(reading, waiting), 2)
        assert order == [MEDIA_HREFS[0], "queue_user", MEDIA_HREFS[1]]
        media.client.async_write.assert_not_awaited()
    finally:
        release.set()
        for task in (reading, waiting):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (reading, waiting) if task is not None),
            return_exceptions=True,
        )


async def test_cancelled_queued_media_request_never_runs_after_lock_release(
    hass, entry, media
):
    await setup(hass, entry)
    coordinator = entry.runtime_data.coordinator
    media.client.async_read.reset_mock()
    async with coordinator._write_lock:
        reading = asyncio.create_task(coordinator.async_ocf_snapshot(MEDIA_HREFS))
        await asyncio.sleep(0)
        reading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reading
    await asyncio.sleep(0)
    assert not coordinator._write_lock.locked()
    media.client.async_read.assert_not_awaited()
    media.client.async_write.assert_not_awaited()
