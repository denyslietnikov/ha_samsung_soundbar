"""Local transaction ownership using real HA coordinator scheduling."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STOP

from custom_components.samsung_soundbar.coordinator import SoundbarCoordinator
from custom_components.samsung_soundbar.local_ocf import OcfError, OcfWriteResult
from custom_components.samsung_soundbar.local_rpc import LocalRpcError
from custom_components.samsung_soundbar.ocf_equalizer import OcfEqualizer
from custom_components.samsung_soundbar.ocf_profiles import Q800F_EQ

ADVANCED = "/sec/networkaudio/advancedaudio"
PREFIX = "x.com.samsung.networkaudio."


@pytest.fixture
async def local_coordinator(hass, entry):
    device = SimpleNamespace(
        device_name="Soundbar",
        local_rpc_enabled=True,
        local_only=False,
        ocf_enabled=True,
        local_last_error=None,
        coordinator=None,
        update_local_status=AsyncMock(),
        update_local_input_source=AsyncMock(),
        read_local_fields=AsyncMock(),
    )
    now = [1.0]
    with patch.object(SoundbarCoordinator, "_schedule_refresh"):
        coordinator = SoundbarCoordinator(hass, entry, device, clock=lambda: now[0])
        try:
            yield coordinator, device, now
        finally:
            await coordinator.async_shutdown()


def ocf_client():
    return SimpleNamespace(
        available=True,
        async_read=AsyncMock(return_value={PREFIX + "bassboost": 0}),
        async_write=AsyncMock(return_value=OcfWriteResult(True, True, False, 68)),
        async_close=AsyncMock(),
        diagnostics=lambda: {"available": True},
    )


@pytest.mark.parametrize("local_only", (True, False))
async def test_rpc_polling_is_two_seconds_in_both_modes(local_coordinator, local_only):
    coordinator, device, now = local_coordinator
    device.local_only = local_only
    device.ocf_enabled = not local_only
    assert coordinator.update_interval.total_seconds() == 2
    assert coordinator.state.use_local
    assert set(coordinator.state.records) == {"rpc", "ocf"}
    await coordinator.async_refresh()
    device.update_local_status.assert_awaited_once_with(min_age=None)
    now[0] = 3
    await coordinator.async_refresh()
    device.update_local_input_source.assert_awaited_once_with(min_age=None)
    now[0] = 62
    await coordinator.async_refresh()
    assert device.update_local_status.await_count == 2


async def test_rpc_disabled_never_runs_actions_or_polls(local_coordinator):
    coordinator, device, _now = local_coordinator
    device.local_rpc_enabled = False
    coordinator.state.use_local = False
    await coordinator.async_refresh()
    device.update_local_status.assert_not_awaited()
    action = AsyncMock()
    with pytest.raises(LocalRpcError):
        await coordinator.async_write(action, {"power": "on"})
    action.assert_not_awaited()


async def test_rpc_only_never_reads_ocf_even_with_attached_client(local_coordinator):
    coordinator, device, _now = local_coordinator
    device.ocf_enabled = False
    client = ocf_client()
    coordinator.configure_ocf(client)
    await coordinator.async_refresh_all()
    await coordinator.async_ocf_snapshot()
    assert coordinator.state.use_ocf is False
    client.async_read.assert_not_awaited()
    with pytest.raises(OcfError):
        await coordinator.async_write_ocf(ADVANCED, {PREFIX + "bassboost": 1})
    client.async_write.assert_not_awaited()
    await coordinator.async_shutdown()


async def test_targeted_rpc_write_reads_only_its_fields(local_coordinator):
    coordinator, device, _now = local_coordinator
    action = AsyncMock(return_value="written")
    assert (
        await coordinator.async_write(action, {"sound_mode": "Standard"}) == "written"
    )
    device.read_local_fields.assert_awaited_once_with({"sound_mode"})
    device.update_local_status.assert_not_awaited()
    action.assert_awaited_once()
    device.read_local_fields.reset_mock()
    await coordinator.async_write(action, {"input_source": "WIFI"})
    device.read_local_fields.assert_awaited_once_with(
        {"input_source", "power", "sound_from_detail_name"}
    )


async def test_rpc_failure_never_replays_on_ocf(local_coordinator):
    coordinator, _device, _now = local_coordinator
    client = ocf_client()
    with patch.object(coordinator, "_schedule_ocf"):
        coordinator.configure_ocf(client)
    action = AsyncMock(side_effect=LocalRpcError("offline"))
    with pytest.raises(LocalRpcError):
        await coordinator.async_write(action, {"power": "off"})
    action.assert_awaited_once()
    client.async_write.assert_not_awaited()
    assert not coordinator.state.pending
    await coordinator.async_shutdown()


async def test_rpc_command_cannot_own_ocf_fields(local_coordinator):
    coordinator, _device, _now = local_coordinator
    action = AsyncMock()
    with pytest.raises(LocalRpcError):
        await coordinator.async_write(action, {"bass_mode": True})
    action.assert_not_awaited()


async def test_cancelled_rpc_readback_invalidates_target_and_releases_queue(
    local_coordinator,
):
    coordinator, device, _now = local_coordinator
    coordinator.receive("rpc", {"sound_mode": "Standard", "volume_level": 0.08})
    started = asyncio.Event()

    async def blocked(_fields):
        started.set()
        await asyncio.Event().wait()

    device.read_local_fields.side_effect = blocked
    task = asyncio.create_task(
        coordinator.async_write(AsyncMock(), {"sound_mode": "Surround"})
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert coordinator.state.value("sound_mode") is None
    assert coordinator.state.value("volume_level") == 0.08
    assert not coordinator.state.pending
    assert not coordinator._write_lock.locked()
    assert not coordinator._poll_lock.locked()
    device.read_local_fields.side_effect = None
    await asyncio.wait_for(
        coordinator.async_write(AsyncMock(), {"volume_level": 0.09}), 1
    )


async def test_writes_and_rpc_poll_do_not_interleave(local_coordinator):
    coordinator, device, _now = local_coordinator
    started, release = asyncio.Event(), asyncio.Event()
    order = []

    async def first():
        order.append("first")
        started.set()
        await release.wait()

    async def second():
        order.append("second")

    async def poll(**_kwargs):
        order.append("poll")

    device.update_local_status.side_effect = poll
    one = asyncio.create_task(coordinator.async_write(first, {}))
    await started.wait()
    two = asyncio.create_task(coordinator.async_write(second, {}))
    polling = asyncio.create_task(coordinator._async_update_data())
    await asyncio.sleep(0)
    assert order == ["first"]
    release.set()
    await asyncio.gather(one, two, polling)
    assert order[0] == "first"
    assert set(order[1:]) == {"second", "poll"}


async def test_diagnostic_refresh_awaits_optional_ocf_sweep(local_coordinator):
    coordinator, device, _now = local_coordinator
    started, release = asyncio.Event(), asyncio.Event()
    client = ocf_client()

    async def read(_href):
        started.set()
        await release.wait()
        return {PREFIX + "bassboost": 1}

    client.async_read.side_effect = read
    coordinator.configure_ocf(client)
    task = asyncio.create_task(coordinator.async_refresh_all())
    await started.wait()
    await asyncio.sleep(0)
    device.update_local_status.assert_awaited_once()
    assert not task.done()
    release.set()
    await task
    assert coordinator.state.value("bass_mode") is True
    await coordinator.async_shutdown()


async def test_ocf_timeout_and_cancellation_never_publish_optimism(local_coordinator):
    coordinator, device, _now = local_coordinator
    client = ocf_client()
    with patch.object(coordinator, "_schedule_ocf"):
        coordinator.configure_ocf(client)
    coordinator.receive("rpc", {"power": "on"})
    coordinator.receive("ocf", {"bass_mode": False})
    client.async_write.side_effect = OcfError("transport")
    with pytest.raises(OcfError):
        await coordinator.async_write_ocf(ADVANCED, {PREFIX + "bassboost": 1})
    assert coordinator.state.value("bass_mode") is False
    assert coordinator.state.value("power") == "on"
    assert not coordinator.state.pending
    device.read_local_fields.assert_not_awaited()
    started = asyncio.Event()

    async def blocked(*_args):
        started.set()
        await asyncio.Event().wait()

    client.async_write.side_effect = blocked
    task = asyncio.create_task(
        coordinator.async_write_ocf(ADVANCED, {PREFIX + "bassboost": 1})
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not coordinator.ocf_field_available("bass_mode")
    assert not coordinator.state.pending
    assert not coordinator._write_lock.locked()
    await coordinator.async_shutdown()


async def test_ocf_projection_keeps_strict_types(local_coordinator):
    coordinator, _device, _now = local_coordinator
    client = ocf_client()
    with patch.object(coordinator, "_schedule_ocf"):
        coordinator.configure_ocf(client)
    client.async_read.return_value = {PREFIX + "bassboost": True}
    await coordinator.async_ocf_snapshot((ADVANCED,))
    assert coordinator.state.value("bass_mode") is None
    client.async_read.return_value = {PREFIX + "bassboost": 1}
    await coordinator.async_ocf_snapshot((ADVANCED,))
    assert coordinator.state.value("bass_mode") is True
    await coordinator.async_shutdown()


async def test_equalizer_diagnostic_does_not_expose_disabled_ocf(local_coordinator):
    coordinator, device, _now = local_coordinator
    client = ocf_client()
    with patch.object(coordinator, "_schedule_ocf"):
        coordinator.configure_ocf(client)
    coordinator.receive("ocf", {"active_equalizer_preset": "NONE"})
    equalizer = OcfEqualizer(device)
    assert equalizer.value("active_equalizer_preset") == "NONE"
    device.ocf_enabled = False
    assert equalizer.value("active_equalizer_preset") is None
    await coordinator.async_shutdown()


async def test_equalizer_cached_profile_requires_current_validated_client(
    local_coordinator,
):
    coordinator, device, _now = local_coordinator
    device.model = "HW-Q800F"
    device.local_identifier = "22_AV_HW-Q800F"
    client = ocf_client()
    client.validated = True
    with patch.object(coordinator, "_schedule_ocf"):
        coordinator.configure_ocf(client)
    coordinator.receive(
        "ocf",
        {
            "equalizer_schema_valid": True,
            "active_equalizer_preset": "NONE",
            "supported_equalizer_presets": ["NONE", "POP"],
            "equalizer_action": "setEQmode",
            "equalizer_band_levels": [0] * 7,
        },
    )
    equalizer = OcfEqualizer(device)
    assert equalizer.profile is Q800F_EQ
    client.validated = False
    assert equalizer.profile is None
    await coordinator.async_shutdown()


@pytest.mark.parametrize("stop_first", (True, False))
async def test_stop_and_unload_share_idempotent_listener_cleanup(
    hass, entry, local_coordinator, caplog, stop_first
):
    coordinator, _device, _now = local_coordinator
    client = ocf_client()
    with patch.object(coordinator, "_schedule_ocf"):
        coordinator.configure_ocf(client)
    if stop_first:
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
    else:
        await coordinator.async_shutdown()
    await entry._async_process_on_unload(hass)
    await coordinator.async_shutdown()
    client.async_close.assert_awaited_once()
    assert "Unable to remove unknown job listener" not in caplog.text
