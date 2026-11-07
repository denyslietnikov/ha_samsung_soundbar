"""OCF removal drains owned work without closing a client under a transaction."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.samsung_soundbar.local_ocf import OcfError, OcfWriteResult

from . import test_local_transport_coordinator as fixtures

local_coordinator = fixtures.local_coordinator
ADVANCED = fixtures.ADVANCED
PREFIX = fixtures.PREFIX


def attach(coordinator):
    client = fixtures.ocf_client()
    with patch.object(coordinator, "_schedule_ocf"):
        coordinator.configure_ocf(client)
    return client


@pytest.mark.parametrize("replacement", (None, "another_client"))
async def test_synchronous_removal_or_replacement_requires_awaited_detach(
    local_coordinator, replacement
):
    coordinator, _device, _now = local_coordinator
    client = attach(coordinator)
    replacement = fixtures.ocf_client() if replacement else None
    with pytest.raises(RuntimeError, match="async_detach_ocf"):
        coordinator.configure_ocf(replacement)
    assert coordinator.ocf is client
    client.async_close.assert_not_awaited()


async def test_detach_drains_cancelled_poll_before_clearing_reference(
    local_coordinator,
):
    coordinator, _device, _now = local_coordinator
    detach = coordinator.async_detach_ocf
    client = attach(coordinator)
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def read(_href):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        assert coordinator.ocf is client
        return {PREFIX + "bassboost": 1}

    client.async_read.side_effect = read
    coordinator._schedule_ocf()
    poll = coordinator._ocf_task
    try:
        await asyncio.wait_for(started.wait(), 1)
        removing = asyncio.create_task(detach())
        await asyncio.wait_for(cancelled.wait(), 1)
        assert coordinator.ocf is client
        assert not removing.done()
        client.async_close.assert_not_awaited()
        release.set()
        await asyncio.wait_for(removing, 1)
        assert poll.done()
        assert client.async_read.await_count == 1
        assert coordinator.ocf is None
        assert not coordinator.state.use_ocf
        assert not coordinator.state.transport_available["ocf"]
        assert coordinator.state.value("bass_mode") is None
        assert coordinator.state.records["ocf"] == {}
        client.async_close.assert_awaited_once()
    finally:
        release.set()


async def test_detach_waits_for_in_progress_diagnostic_read(local_coordinator):
    coordinator, _device, _now = local_coordinator
    detach = coordinator.async_detach_ocf
    client = attach(coordinator)
    started, release = asyncio.Event(), asyncio.Event()

    async def read(_href):
        started.set()
        await release.wait()
        assert coordinator.ocf is client
        return {PREFIX + "bassboost": 0}

    client.async_read.side_effect = read
    reading = asyncio.create_task(coordinator.async_ocf_snapshot((ADVANCED,)))
    try:
        await asyncio.wait_for(started.wait(), 1)
        removing = asyncio.create_task(detach())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not removing.done()
        assert coordinator.ocf is client
        client.async_close.assert_not_awaited()
        release.set()
        await asyncio.wait_for(asyncio.gather(reading, removing), 1)
        client.async_close.assert_awaited_once()
    finally:
        release.set()


@pytest.mark.parametrize("shutdown", (False, True))
async def test_active_write_finishes_once_before_detach_or_shutdown_closes_client(
    local_coordinator, shutdown
):
    coordinator, _device, _now = local_coordinator
    detach = coordinator.async_detach_ocf
    client = attach(coordinator)
    started, release = asyncio.Event(), asyncio.Event()
    order = []

    async def write(*_args):
        order.append("write")
        started.set()
        await release.wait()
        order.append("ack")
        return OcfWriteResult(True, True, False, 68)

    async def read(_href):
        assert coordinator.ocf is client
        order.append("readback")
        return {PREFIX + "bassboost": 1}

    async def close():
        order.append("close")

    client.async_write.side_effect = write
    client.async_read.side_effect = read
    client.async_close.side_effect = close
    writing = asyncio.create_task(
        coordinator.async_write_ocf(ADVANCED, {PREFIX + "bassboost": 1})
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        removing = asyncio.create_task(detach())
        await asyncio.sleep(0)
        stopping = (
            asyncio.create_task(coordinator.async_shutdown()) if shutdown else None
        )
        await asyncio.sleep(0)
        assert not writing.done()
        assert not removing.done()
        client.async_close.assert_not_awaited()
        release.set()
        tasks = [writing, removing] + ([stopping] if stopping else [])
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
        assert order == ["write", "ack", "readback", "close"]
        client.async_write.assert_awaited_once()
        client.async_close.assert_awaited_once()
        await detach()
        await coordinator.async_shutdown()
        client.async_close.assert_awaited_once()
    finally:
        release.set()


@pytest.mark.parametrize("operation", ("write", "snapshot"))
async def test_queued_ocf_operations_recheck_detach_under_shared_lock(
    local_coordinator, operation
):
    coordinator, _device, _now = local_coordinator
    detach = coordinator.async_detach_ocf
    client = attach(coordinator)
    async with coordinator._write_lock:
        pending = asyncio.create_task(
            coordinator.async_write_ocf(ADVANCED, {PREFIX + "bassboost": 1})
            if operation == "write"
            else coordinator.async_ocf_snapshot((ADVANCED,))
        )
        await asyncio.sleep(0)
        removing = asyncio.create_task(detach())
        await asyncio.sleep(0)
    results = await asyncio.wait_for(
        asyncio.gather(pending, removing, return_exceptions=True), 1
    )
    if operation == "write":
        assert isinstance(results[0], OcfError)
        assert results[0].kind == "closed"
    else:
        assert isinstance(results[0], dict)
    assert results[1] is None
    client.async_write.assert_not_awaited()
    client.async_read.assert_not_awaited()
    client.async_close.assert_awaited_once()


async def test_cancelled_forget_drains_close_once_and_rpc_stays_usable(
    local_coordinator,
):
    coordinator, device, _now = local_coordinator
    detach = coordinator.async_detach_ocf
    client = attach(coordinator)
    closing, release = asyncio.Event(), asyncio.Event()

    async def close():
        closing.set()
        await release.wait()

    client.async_close.side_effect = close
    coordinator.transport_changed("rpc", True)
    coordinator.receive("rpc", {"power": "on", "volume_level": 0.08})
    removing = asyncio.create_task(detach())
    try:
        await asyncio.wait_for(closing.wait(), 1)
        removing.cancel()
        await asyncio.sleep(0)
        removing.cancel()
        await asyncio.sleep(0)
        assert not removing.done()
        assert coordinator.ocf is None
        action = AsyncMock(return_value="rpc-ok")
        assert (
            await asyncio.wait_for(
                coordinator.async_write(action, {"volume_level": 0.09}), 1
            )
            == "rpc-ok"
        )
        device.read_local_fields.assert_awaited_once_with({"volume_level"})
        assert coordinator.state.snapshot().available
        with pytest.raises(OcfError):
            await coordinator.async_write_ocf(ADVANCED, {PREFIX + "bassboost": 1})
        await coordinator.async_ocf_snapshot((ADVANCED,))
        coordinator._schedule_ocf()
        client.async_read.assert_not_awaited()
        client.async_write.assert_not_awaited()
        with pytest.raises(RuntimeError):
            coordinator.configure_ocf(fixtures.ocf_client())
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await removing
        await asyncio.gather(detach(), detach())
        await coordinator.async_shutdown()
        client.async_close.assert_awaited_once()
    finally:
        release.set()


async def test_detach_cancelled_during_active_write_preserves_its_result(
    local_coordinator,
):
    coordinator, _device, _now = local_coordinator
    detach = coordinator.async_detach_ocf
    client = attach(coordinator)
    started, release = asyncio.Event(), asyncio.Event()

    async def write(*_args):
        started.set()
        await release.wait()
        return OcfWriteResult(True, True, False, 68)

    client.async_write.side_effect = write
    writing = asyncio.create_task(
        coordinator.async_write_ocf(ADVANCED, {PREFIX + "bassboost": 1})
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        removing = asyncio.create_task(detach())
        await asyncio.sleep(0)
        removing.cancel()
        await asyncio.sleep(0)
        assert not removing.done()
        assert not writing.cancelled()
        client.async_close.assert_not_awaited()
        release.set()
        assert (await asyncio.wait_for(writing, 1)).readback_matches is True
        with pytest.raises(asyncio.CancelledError):
            await removing
        client.async_write.assert_awaited_once()
        client.async_read.assert_awaited_once()
        client.async_close.assert_awaited_once()
    finally:
        release.set()


async def test_refresh_waiting_for_poll_survives_detach_cancellation(local_coordinator):
    coordinator, _device, _now = local_coordinator
    detach = coordinator.async_detach_ocf
    client = attach(coordinator)
    started = asyncio.Event()

    async def read(_href):
        started.set()
        await asyncio.Event().wait()

    client.async_read.side_effect = read
    refreshing = asyncio.create_task(coordinator.async_refresh_all())
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.wait_for(detach(), 1)
    await asyncio.wait_for(refreshing, 1)
    client.async_close.assert_awaited_once()


async def test_detach_empty_and_reattach_keep_distinct_client_ownership(
    local_coordinator,
):
    coordinator, _device, _now = local_coordinator
    await coordinator.async_detach_ocf()
    first = attach(coordinator)
    await coordinator.async_detach_ocf()
    second = attach(coordinator)
    await coordinator.async_detach_ocf()
    await coordinator.async_shutdown()
    assert coordinator.ocf is None
    first.async_close.assert_awaited_once()
    second.async_close.assert_awaited_once()
