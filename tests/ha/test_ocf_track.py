"""AirPlay text reaches real HA entities without widening playback or diagnostics."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.media_player.const import MediaPlayerEntityFeature
from homeassistant.core import callback

from custom_components.samsung_soundbar.const import (
    CONF_CONTROL_MODE,
    CONTROL_MODE_HYBRID_LOCAL_OCF,
    CONTROL_MODE_LOCAL_ONLY,
    DOMAIN,
)
from custom_components.samsung_soundbar.coordinator import SoundbarCoordinator
from custom_components.samsung_soundbar.local_ocf import OcfError
from custom_components.samsung_soundbar.local_rpc import LocalRpcError
from custom_components.samsung_soundbar.ocf_state import POLL_HREFS

from . import test_ocf_state as ocf_fixtures
from .conftest import LOCAL_STATUS
from .test_lifecycle import registered

ocf = ocf_fixtures.ocf
TRACK = "/sec/networkaudio/av/audioTrackData"
PLAYBACK = "/sec/networkaudio/playback"
PREFIX = "x.com.samsung.networkaudio."
TEXT_FIELDS = ("media_title", "media_artist", "media_album_name")
PRIVATE = ("PRIVATE_TRACK_TITLE", "PRIVATE_TRACK_ARTIST", "PRIVATE_TRACK_ALBUM")
ARTWORK = "https://private.invalid/cover?token=PRIVATE_ART_TOKEN"


@pytest.fixture
def track(hass, entry, transports, ocf):
    """Keep transport boundaries fake, but setup/state/entities entirely real."""
    hass.config_entries.async_update_entry(
        entry,
        version=2,
        minor_version=1,
        options={**entry.options, CONF_CONTROL_MODE: CONTROL_MODE_HYBRID_LOCAL_OCF},
    )
    status = {**LOCAL_STATUS, "input_source": "WIFI_AIRPLAY"}
    transports.rpc.status.return_value = status
    transports.rpc.input_source.return_value = "WIFI_AIRPLAY"
    ocf.available = True
    ocf.validated = True
    now = [1.0]
    payload = {
        "title": PRIVATE[0],
        "artist": PRIVATE[1],
        "album": PRIVATE[2],
        "thumbnail": ARTWORK,
        "trackUri": "https://private.invalid/PRIVATE_TRACK_URI",
        "duration": 12345,
    }
    audio_read = ocf.async_read.side_effect

    async def read(href):
        if href == TRACK:
            return deepcopy(payload)
        if href == PLAYBACK:
            return {PREFIX + "status": "STOPPED"}
        return audio_read(href)

    ocf.async_read.side_effect = read

    def coordinator(hass, entry, device):
        return SoundbarCoordinator(hass, entry, device, clock=lambda: now[0])

    with patch(
        "custom_components.samsung_soundbar.SoundbarCoordinator",
        side_effect=coordinator,
    ):
        yield SimpleNamespace(
            client=ocf,
            payload=payload,
            status=status,
            now=now,
            read=read,
            rpc=transports.rpc,
        )


async def setup(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    await coordinator.async_refresh_all()
    await hass.async_block_till_done()
    return coordinator


def player(hass, entry):
    entity_id = next(
        entity_id
        for entity_id, _ in registered(hass, entry).values()
        if entity_id.startswith("media_player.")
    )
    return hass.states.get(entity_id)


def assert_text(hass, entry, values):
    state = player(hass, entry)
    device = entry.runtime_data.device
    assert tuple(state.attributes.get(field) for field in TEXT_FIELDS) == values
    assert state.attributes.get("media_content_type") == (
        "music" if values[0] else None
    )
    assert tuple(getattr(device, field) for field in TEXT_FIELDS) == values
    assert (
        tuple(device.coordinator.media_text_value(field) for field in TEXT_FIELDS)
        == values
    )


def hrefs(track):
    return [call.args[0] for call in track.client.async_read.await_args_list]


async def refresh(hass, entry):
    await entry.runtime_data.coordinator.async_refresh_all()
    await hass.async_block_till_done()


async def dump(hass, service="dump_ocf_status", data=None):
    return await hass.services.async_call(
        DOMAIN, service, data or {}, blocking=True, return_response=True
    )


async def test_native_ha_text_changes_without_new_entities_or_pollers(
    hass, entry, track
):
    coordinator = await setup(hass, entry)
    before = registered(hass, entry)
    assert_text(hass, entry, PRIVATE)
    assert coordinator.state.value("input_source") == "WIFI"
    assert coordinator.state.value("sound_from_detail_name") == "AirPlay"
    for field, value in zip(TEXT_FIELDS, PRIVATE, strict=True):
        record = coordinator.state.resolve(field)
        assert (record.source, record.value, record.ttl) == ("ocf", value, 15.0)
    assert TRACK not in POLL_HREFS
    track.payload.update(title="Next title", artist="Next artist", album="Next album")
    await refresh(hass, entry)
    assert_text(hass, entry, ("Next title", "Next artist", "Next album"))
    assert registered(hass, entry) == before
    track.client.async_write.assert_not_awaited()


async def test_pause_diagnostics_preserve_text_and_never_derive_playback(
    hass, entry, track
):
    await setup(hass, entry)
    before = player(hass, entry)
    features = before.attributes["supported_features"]
    for status in ("PLAYING", "STOPPED", "PLAYING"):

        async def read(href, status=status):
            if href == PLAYBACK:
                return {PREFIX + "status": status}
            return await track.read(href)

        track.client.async_read.side_effect = read
        response = await dump(hass, data={"hrefs": [PLAYBACK]})
        assert (
            response["media"]["resources"][PLAYBACK]["fields"]["status"]["value"]
            == status
        )
        await refresh(hass, entry)
        assert_text(hass, entry, PRIVATE)
        current = player(hass, entry)
        assert current.state == "on"
        assert current.attributes["supported_features"] == features
        for attribute in (
            "entity_picture",
            "media_image_url",
            "media_duration",
            "media_position",
        ):
            assert current.attributes.get(attribute) is None
    for feature in ("PLAY", "PAUSE", "STOP", "NEXT_TRACK", "PREVIOUS_TRACK", "SEEK"):
        assert not features & getattr(MediaPlayerEntityFeature, feature)
    track.client.async_write.assert_not_awaited()


@pytest.mark.parametrize("source", ("WIFI_IDLE", "WIFI_GOOGLE", "BT", "E_ARC"))
async def test_leaving_airplay_clears_real_attributes_and_stops_track_reads(
    hass, entry, track, source
):
    await setup(hass, entry)
    assert_text(hass, entry, PRIVATE)
    track.status["input_source"] = source
    track.rpc.input_source.return_value = source
    await refresh(hass, entry)
    assert_text(hass, entry, (None, None, None))
    track.client.async_read.reset_mock()
    await refresh(hass, entry)
    assert TRACK not in hrefs(track)
    assert player(hass, entry).state == "on"


async def test_power_off_clears_text_without_affecting_volume_or_ids(
    hass, entry, track
):
    await setup(hass, entry)
    before = registered(hass, entry)
    assert_text(hass, entry, PRIVATE)
    track.status["power"] = "powerOff"
    track.rpc.power_state.return_value = "powerOff"
    await refresh(hass, entry)
    assert_text(hass, entry, (None, None, None))
    assert player(hass, entry).state == "off"
    assert entry.runtime_data.device.volume_level == 0.08
    assert registered(hass, entry) == before


async def test_rpc_disconnect_clears_text_and_recovery_requires_new_track(
    hass, entry, track
):
    coordinator = await setup(hass, entry)
    assert_text(hass, entry, PRIVATE)
    track.rpc.status.side_effect = LocalRpcError("offline")
    await refresh(hass, entry)
    assert_text(hass, entry, (None, None, None))
    assert player(hass, entry).state == "unavailable"
    track.rpc.status.side_effect = None
    track.client.async_read.side_effect = OcfError("transport")
    await refresh(hass, entry)
    assert player(hass, entry).state == "on"
    assert_text(hass, entry, (None, None, None))
    assert coordinator.state.value("volume_level") == 0.08
    track.client.async_read.side_effect = track.read
    track.payload["title"] = "Recovered track"
    await refresh(hass, entry)
    assert_text(hass, entry, ("Recovered track", PRIVATE[1], PRIVATE[2]))


async def test_ocf_outage_clears_text_without_disabling_core_rpc(hass, entry, track):
    coordinator = await setup(hass, entry)
    assert_text(hass, entry, PRIVATE)
    track.client.available = False
    track.client.async_read.side_effect = OcfError("transport")
    await refresh(hass, entry)
    assert_text(hass, entry, (None, None, None))
    assert player(hass, entry).state == "on"
    assert player(hass, entry).attributes["volume_level"] == 0.08
    assert not coordinator.state.snapshot().ocf_available
    track.client.available = True
    track.client.async_read.side_effect = track.read
    track.payload.clear()
    track.payload["title"] = "Recovered title only"
    await refresh(hass, entry)
    assert_text(hass, entry, ("Recovered title only", None, None))


@pytest.mark.parametrize("expected", ({"input_source": "BT"}, {"power": "off"}))
async def test_pending_context_command_clears_published_attributes_before_io(
    hass, entry, track, expected
):
    coordinator = await setup(hass, entry)
    assert_text(hass, entry, PRIVATE)
    started, release = asyncio.Event(), asyncio.Event()

    async def blocked():
        started.set()
        await release.wait()

    task = asyncio.create_task(coordinator.async_write(blocked, expected))
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert not task.done()
        assert_text(hass, entry, (None, None, None))
        assert "media_title" not in coordinator.data.values
    finally:
        release.set()
        await task


@pytest.mark.parametrize("phase", ("audio_get", "lock_wait", "command_queue"))
async def test_cancelled_sweep_always_clears_published_track_text(
    hass, entry, track, phase
):
    coordinator = await setup(hass, entry)
    assert_text(hass, entry, PRIVATE)
    started = asyncio.Event()
    command_started, release_command = asyncio.Event(), asyncio.Event()
    command = None

    async def blocked(_href):
        started.set()
        await asyncio.Event().wait()

    track.client.async_read.side_effect = blocked
    if phase == "lock_wait":
        await coordinator._write_lock.acquire()
    elif phase == "command_queue":

        async def blocked_command():
            command_started.set()
            await release_command.wait()

        command = asyncio.create_task(
            coordinator.async_write(blocked_command, {"volume_level": 0.09})
        )
        await asyncio.wait_for(command_started.wait(), 1)
    try:
        coordinator._next_ocf = 0
        coordinator._schedule_ocf()
        if phase == "audio_get":
            await asyncio.wait_for(started.wait(), 1)
        else:
            await asyncio.sleep(0)
        coordinator._ocf_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await coordinator._ocf_task
        assert_text(hass, entry, (None, None, None))
        assert "media_title" not in coordinator.data.values
        assert player(hass, entry).state == "on"
    finally:
        if phase == "lock_wait":
            coordinator._write_lock.release()
        if command is not None:
            release_command.set()
            await command


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"title": "  Replacement  "}, ("Replacement", None, None)),
        (
            {"title": "Replacement", "artist": "", "album": None},
            ("Replacement", None, None),
        ),
        (
            {"title": "Replacement", "artist": [], "album": 42},
            ("Replacement", None, None),
        ),
        (
            {"title": "Replacement", "artist": " A ", "album": " B "},
            ("Replacement", "A", "B"),
        ),
        ({"title": "T" * 1024}, ("T" * 1024, None, None)),
        ({}, (None, None, None)),
        ({"artist": "orphan artist", "album": "orphan album"}, (None, None, None)),
        ({"title": "   ", "artist": "orphan"}, (None, None, None)),
        ({"title": "T" * 1025}, (None, None, None)),
        ({"title": "bad\x00title"}, (None, None, None)),
        ({"title": "bad\ud800title"}, (None, None, None)),
        ({"title": 123}, (None, None, None)),
    ],
)
async def test_each_track_response_replaces_all_three_attributes(
    hass, entry, track, payload, expected
):
    await setup(hass, entry)
    assert_text(hass, entry, PRIVATE)
    track.payload.clear()
    track.payload.update(payload)
    await refresh(hass, entry)
    assert_text(hass, entry, expected)


@pytest.mark.parametrize(
    "kind", ("permission", "unsupported", "auth", "transport", "malformed")
)
async def test_track_failure_clears_text_but_audio_and_rpc_remain_usable(
    hass, entry, track, kind
):
    coordinator = await setup(hass, entry)
    before = registered(hass, entry)
    assert_text(hass, entry, PRIVATE)

    async def read(href):
        if href == TRACK:
            raise OcfError(kind)
        return await track.read(href)

    track.client.async_read.side_effect = read
    await refresh(hass, entry)
    assert_text(hass, entry, (None, None, None))
    assert player(hass, entry).state == "on"
    assert coordinator.state.value("volume_level") == 0.08
    assert coordinator.state.value("woofer_level") == 1
    assert registered(hass, entry) == before


async def test_text_ttl_is_15_seconds_and_media_diagnostics_cannot_refresh_it(
    hass, entry, track
):
    coordinator = await setup(hass, entry)
    assert_text(hass, entry, PRIVATE)
    records = {field: coordinator.state.resolve(field) for field in TEXT_FIELDS}
    track.now[0] = 15.9
    coordinator.state.merge("rpc", {"sound_from_detail_name": "AirPlay"})
    coordinator.publish()
    await hass.async_block_till_done()
    assert_text(hass, entry, PRIVATE)
    await dump(hass, data={"preset": "media"})
    assert all(
        coordinator.state.resolve(field) is record for field, record in records.items()
    )
    track.now[0] = 16.1
    coordinator.state.merge("rpc", {"sound_from_detail_name": "AirPlay"})
    coordinator.publish()
    await hass.async_block_till_done()
    assert_text(hass, entry, (None, None, None))
    assert player(hass, entry).state == "on"


async def test_text_5s_tier_does_not_postpone_audio_30s_or_change_rpc_2s(
    hass, entry, track
):
    coordinator = await setup(hass, entry)
    assert coordinator.update_interval.total_seconds() == 2
    assert coordinator._next_ocf == 31.0
    track.client.async_read.reset_mock()
    track.now[0] = 5.9
    await coordinator.async_refresh()
    await coordinator._async_wait_ocf_poll()
    track.client.async_read.assert_not_awaited()
    for now in (7.0, 13.0, 19.0, 25.0):
        track.now[0] = now
        await coordinator.async_refresh()
        await coordinator._async_wait_ocf_poll()
        await hass.async_block_till_done()
        assert coordinator._next_ocf == 31.0
    assert hrefs(track) == [TRACK] * 4
    track.client.async_read.reset_mock()
    track.now[0] = 31.0
    await coordinator.async_refresh()
    await coordinator._async_wait_ocf_poll()
    await hass.async_block_till_done()
    assert set(hrefs(track)) == {*POLL_HREFS, TRACK}
    assert hrefs(track).count(TRACK) == 1
    assert coordinator._next_ocf == 61.0


async def test_text_getters_are_read_only_and_rpc_cannot_own_track_fields(
    hass, entry, track
):
    coordinator = await setup(hass, entry)
    before = deepcopy(coordinator.state.records)
    reads = track.client.async_read.await_count
    poll = coordinator._ocf_task
    for _ in range(3):
        assert_text(hass, entry, PRIVATE)
    assert coordinator.state.records == before
    assert track.client.async_read.await_count == reads
    assert coordinator._ocf_task is poll
    track.payload.clear()
    await refresh(hass, entry)
    coordinator.receive("rpc", dict(zip(TEXT_FIELDS, PRIVATE, strict=True)))
    await hass.async_block_till_done()
    assert_text(hass, entry, (None, None, None))
    assert not set(TEXT_FIELDS) & coordinator.state.records["rpc"].keys()


@pytest.mark.parametrize("gate", ("model", "validated", "available"))
async def test_unverified_model_or_client_never_populates_text(
    hass, entry, track, gate
):
    if gate == "model":
        track.status["identifier"] = "22_AV_HW-Q990F"
    else:
        setattr(track.client, gate, False)
    await setup(hass, entry)
    assert_text(hass, entry, (None, None, None))
    assert TRACK not in hrefs(track)
    assert player(hass, entry).state == "on"


@pytest.mark.parametrize("field", ("power", "input_source", "sound_from_detail_name"))
async def test_pending_context_write_immediately_hides_text(hass, entry, track, field):
    coordinator = await setup(hass, entry)
    assert_text(hass, entry, PRIVATE)
    coordinator.state.begin_write({field}, source="rpc")
    coordinator.publish()
    await hass.async_block_till_done()
    assert_text(hass, entry, (None, None, None))


async def test_stale_rpc_detail_hides_text_even_if_text_was_recent(hass, entry, track):
    coordinator = await setup(hass, entry)
    track.now[0] = 10.0
    coordinator._schedule_ocf()
    await coordinator._async_wait_ocf_poll()
    await hass.async_block_till_done()
    assert_text(hass, entry, PRIVATE)
    # Detail remains the original t=1 read; text was independently refreshed at t=10.
    track.now[0] = 16.1
    coordinator.publish()
    await hass.async_block_till_done()
    assert_text(hass, entry, (None, None, None))


async def test_track_poll_reuses_one_owned_task_and_the_command_queue(
    hass, entry, track
):
    coordinator = await setup(hass, entry)
    track.now[0] = 7.0
    track.client.async_read.reset_mock()
    async with coordinator._write_lock:
        coordinator._schedule_ocf()
        poll = coordinator._ocf_task
        coordinator._schedule_ocf()
        assert coordinator._ocf_task is poll
        await asyncio.sleep(0)
        assert not poll.done()
        track.client.async_read.assert_not_awaited()
    await coordinator._async_wait_ocf_poll()
    assert hrefs(track) == [TRACK]
    assert_text(hass, entry, PRIVATE)


async def delayed_track(hass, entry, track):
    """Return a pending real poll and explicit release, never a timed sleep."""
    started, release = asyncio.Event(), asyncio.Event()
    stale = {"title": "DELAYED_OLD_TRACK", "artist": "old", "album": "old"}
    titles = []
    player_id = player(hass, entry).entity_id

    @callback
    def record(event):
        if event.data["entity_id"] == player_id and (state := event.data["new_state"]):
            titles.append(state.attributes.get("media_title"))

    unsubscribe = hass.bus.async_listen("state_changed", record)

    async def read(href):
        if href == TRACK and not started.is_set():
            started.set()
            await release.wait()
            return stale
        return await track.read(href)

    track.client.async_read.side_effect = read
    task = asyncio.create_task(entry.runtime_data.coordinator.async_refresh_all())
    try:
        await asyncio.wait_for(started.wait(), 1)
    except BaseException:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        unsubscribe()
        raise
    return task, release, titles, unsubscribe


@pytest.mark.parametrize(
    "transition", ("source", "power", "outage", "ocf_outage", "write")
)
async def test_delayed_a_b_a_track_cannot_repopulate_same_visible_context(
    hass, entry, track, transition
):
    coordinator = await setup(hass, entry)
    assert_text(hass, entry, PRIVATE)
    task, release, titles, unsubscribe = await delayed_track(hass, entry, track)
    try:
        if transition == "source":
            coordinator.receive(
                "rpc",
                {"input_source": "BT", "sound_from_detail_name": "External Device"},
            )
            coordinator.receive(
                "rpc", {"input_source": "WIFI", "sound_from_detail_name": "AirPlay"}
            )
        elif transition == "power":
            coordinator.receive("rpc", {"power": "off"})
            coordinator.receive("rpc", {"power": "on"})
        elif transition == "outage":
            coordinator.transport_changed("rpc", False)
            coordinator.transport_changed("rpc", True)
        elif transition == "ocf_outage":
            coordinator.transport_changed("ocf", False)
            coordinator.transport_changed("ocf", True)
        else:
            coordinator.state.begin_write({"input_source"}, source="rpc")
            coordinator.receive(
                "rpc",
                {
                    "power": "on",
                    "input_source": "WIFI",
                    "sound_from_detail_name": "AirPlay",
                },
            )
        assert_text(hass, entry, (None, None, None))
        release.set()
        await asyncio.wait_for(task, 1)
        await hass.async_block_till_done()
        assert "DELAYED_OLD_TRACK" not in titles
        assert_text(hass, entry, (None, None, None))
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        unsubscribe()


@pytest.mark.parametrize("operation", ("detach", "cancel", "reload"))
async def test_inflight_track_is_drained_on_cancel_detach_or_reload(
    hass, entry, track, operation
):
    coordinator = await setup(hass, entry)
    before = registered(hass, entry)
    task, release, titles, unsubscribe = await delayed_track(hass, entry, track)
    try:
        if operation == "cancel":
            poll = coordinator._ocf_task
            poll.cancel()
            await asyncio.gather(poll, return_exceptions=True)
        elif operation == "detach":
            await asyncio.wait_for(coordinator.async_detach_ocf(), 1)
        else:
            track.payload.clear()
            track.client.async_read.side_effect = track.read
            assert await hass.config_entries.async_reload(entry.entry_id)
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await hass.async_block_till_done()
        assert "DELAYED_OLD_TRACK" not in titles
        assert_text(hass, entry, (None, None, None))
        assert registered(hass, entry) == before
        if operation != "cancel":
            track.client.async_close.assert_awaited_once()
        assert player(hass, entry).state == "on"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        unsubscribe()


async def test_forget_key_drains_track_and_never_reopens_store_on_diagnostic_read(
    hass, entry, track
):
    await setup(hass, entry)
    before = registered(hass, entry)
    assert_text(hass, entry, PRIVATE)
    task, release, titles, unsubscribe = await delayed_track(hass, entry, track)
    try:
        with patch(
            "custom_components.samsung_soundbar.ocf_credentials.OcfCredentialStore.async_remove",
            new=AsyncMock(),
        ) as remove:
            form = await hass.config_entries.options.async_init(entry.entry_id)
            form = await hass.config_entries.options.async_configure(
                form["flow_id"], {"ocf_action": "forget"}
            )
            result = await hass.config_entries.options.async_configure(
                form["flow_id"], {"confirm": True}
            )
            assert result["type"] == "create_entry"
            remove.assert_awaited_once()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await hass.async_block_till_done()
        assert "DELAYED_OLD_TRACK" not in titles
        assert entry.data.get("ocf_configured") is not True
        assert_text(hass, entry, (None, None, None))
        assert registered(hass, entry) == before
        track.client.async_close.assert_awaited_once()
        with (
            patch("custom_components.samsung_soundbar.OcfCredentialStore") as store,
            patch(
                "custom_components.samsung_soundbar.LocalSoundbarOcfClient"
            ) as factory,
        ):
            await dump(hass, data={"preset": "media"})
            store.assert_not_called()
            factory.assert_not_called()
        assert_text(hass, entry, (None, None, None))
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        unsubscribe()


async def test_key_replacement_reload_cannot_publish_previous_client_track(
    hass, entry, track
):
    old = await setup(hass, entry)
    before = registered(hass, entry)
    task, release, titles, unsubscribe = await delayed_track(hass, entry, track)
    replacement = MagicMock(available=True, validated=True)
    replacement.async_close = AsyncMock()
    replacement.async_read = AsyncMock(return_value={})
    replacement.diagnostics.return_value = {"configured": True, "available": True}
    try:
        with patch(
            "custom_components.samsung_soundbar.LocalSoundbarOcfClient",
            return_value=replacement,
        ):
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await hass.async_block_till_done()
        assert "DELAYED_OLD_TRACK" not in titles
        assert_text(hass, entry, (None, None, None))
        assert old.ocf is None
        assert entry.runtime_data.coordinator.ocf is replacement
        track.client.async_close.assert_awaited_once()
        assert registered(hass, entry) == before
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        unsubscribe()


async def test_diagnostics_and_support_data_exclude_actual_ui_track_text(
    hass, entry, track, transports, caplog
):
    coordinator = await setup(hass, entry)
    assert_text(hass, entry, PRIVATE)
    caplog.clear()
    responses = [
        entry.runtime_data.device.retrieve_data,
        coordinator.ocf_diagnostics(),
        coordinator.diagnostics(),
        await dump(hass),
        await dump(hass, data={"preset": "media"}),
        await dump(hass, service="dump_discovery_snapshot"),
    ]
    for marker in (*PRIVATE, ARTWORK, "PRIVATE_TRACK_URI"):
        assert all(marker not in repr(response) for response in responses)
        assert marker not in caplog.text
        assert marker not in repr(entry.data) + repr(entry.options)
    assert_text(hass, entry, PRIVATE)
    transports.http.get.assert_not_awaited()
    transports.http.post.assert_not_awaited()
    transports.http.request.assert_not_awaited()
    track.client.async_write.assert_not_awaited()


@pytest.mark.parametrize("with_text", (False, True))
async def test_ephemeral_media_diagnostic_neither_injects_nor_refreshes_ui_text(
    hass, entry, track, with_text
):
    coordinator = await setup(hass, entry)
    if not with_text:
        track.payload.clear()
        await refresh(hass, entry)
    expected = PRIVATE if with_text else (None, None, None)
    assert_text(hass, entry, expected)
    records = deepcopy(coordinator.state.records)
    track.now[0] = 7.0
    track.payload.update(
        title="DIAGNOSTIC_ONLY_TITLE",
        artist="DIAGNOSTIC_ONLY_ARTIST",
        album="DIAGNOSTIC_ONLY_ALBUM",
    )
    response = await dump(hass, data={"preset": "media"})
    await hass.async_block_till_done()
    assert "DIAGNOSTIC_ONLY" not in repr(response)
    assert coordinator.state.records == records
    assert_text(hass, entry, expected)
    if with_text:
        track.now[0] = 16.1
        coordinator.state.merge("rpc", {"sound_from_detail_name": "AirPlay"})
        coordinator.publish()
        await hass.async_block_till_done()
        assert_text(hass, entry, (None, None, None))


async def test_local_only_keeps_saved_key_store_and_dtls_inactive(
    hass, entry, transports
):
    hass.config_entries.async_update_entry(
        entry,
        version=2,
        minor_version=1,
        data={**entry.data, "ocf_configured": True},
        options={**entry.options, CONF_CONTROL_MODE: CONTROL_MODE_LOCAL_ONLY},
    )
    transports.rpc.status.return_value = {
        **LOCAL_STATUS,
        "input_source": "WIFI_AIRPLAY",
    }
    transports.rpc.input_source.return_value = "WIFI_AIRPLAY"
    with (
        patch("custom_components.samsung_soundbar.OcfCredentialStore") as store,
        patch("custom_components.samsung_soundbar.LocalSoundbarOcfClient") as factory,
    ):
        coordinator = await setup(hass, entry)
        await dump(hass, data={"preset": "media"})
        await dump(hass, service="dump_discovery_snapshot")
        assert_text(hass, entry, (None, None, None))
        assert coordinator.ocf is None
        assert coordinator._ocf_task is None
        store.assert_not_called()
        factory.assert_not_called()
    assert player(hass, entry).state == "on"
