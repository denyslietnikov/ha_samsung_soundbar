"""Local validation cannot overwrite a newer binding of the same entry."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.samsung_soundbar.const import DOMAIN

FLOW = "custom_components.samsung_soundbar.config_flow"
IDENTITY_A = {"wifi_mac": "02:00:00:00:00:0a"}
IDENTITY_B = {"wifi_mac": "02:00:00:00:00:0b"}
TARGET_A = {
    "local_host": "192.0.2.10",
    "local_port": 1516,
    "local_verify_ssl": False,
    "local_timeout": 5,
    "control_mode": "local_only",
}


def set_binding(hass, entry, identity):
    options = {**entry.options, **TARGET_A}
    if identity is None:
        options.pop("local_identity", None)
    else:
        options["local_identity"] = deepcopy(identity)
    hass.config_entries.async_update_entry(
        entry, version=2, minor_version=0, options=options
    )


def change_to_b(hass, entry):
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "local_host": "192.0.2.11",
            "local_identity": dict(IDENTITY_B),
        },
    )
    return deepcopy(dict(entry.options))


async def start(hass, entry, kind):
    if kind == "options":
        return await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": "reconfigure",
            "entry_id": entry.entry_id,
        },
    )


async def submit(hass, kind, form, values):
    manager = (
        hass.config_entries.options if kind == "options" else hass.config_entries.flow
    )
    return await manager.async_configure(form["flow_id"], values)


@pytest.mark.parametrize("kind", ["options", "reconfigure"])
async def test_pending_confirmation_cannot_replace_new_binding(hass, entry, kind):
    set_binding(hass, entry, None)
    with (
        patch(
            f"{FLOW}._validate_local", new=AsyncMock(return_value=IDENTITY_A)
        ) as validate,
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        form = await start(hass, entry, kind)
        form = await submit(hass, kind, form, TARGET_A)
        assert form["step_id"] == "confirm_local_identity"
        newer = change_to_b(hass, entry)
        result = await submit(hass, kind, form, {})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "identity_mismatch"}
    assert dict(entry.options) == newer
    assert validate.await_count == 1
    reload.assert_not_called()


@pytest.mark.parametrize("kind", ["options", "reconfigure"])
@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("local_host", "192.0.2.11"),
        ("local_port", 1517),
        ("local_verify_ssl", True),
        ("local_timeout", 9),
        ("control_mode", "hybrid_local_ocf"),
        (
            "local_identity",
            {**IDENTITY_A, "upnp_udn": "uuid:00000000-0000-4000-8000-00000000000b"},
        ),
    ],
)
async def test_binding_change_during_validation_never_commits_stale_options(
    hass, entry, kind, pending, key, value
):
    set_binding(hass, entry, None if pending else IDENTITY_A)
    started, release = asyncio.Event(), asyncio.Event()

    async def validate(_hass, _options):
        started.set()
        await release.wait()
        return dict(IDENTITY_A)

    with (
        patch(
            f"{FLOW}._validate_local", new=AsyncMock(return_value=IDENTITY_A)
        ) as probe,
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        form = await start(hass, entry, kind)
        if pending:
            form = await submit(hass, kind, form, TARGET_A)
            assert form["step_id"] == "confirm_local_identity"
        probe.side_effect = validate
        task = asyncio.create_task(
            submit(hass, kind, form, {} if pending else TARGET_A)
        )
        await started.wait()
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, key: value}
        )
        newer = deepcopy(dict(entry.options))
        release.set()
        result = await task
    assert result["type"] == "form"
    assert result["errors"] == {"base": "identity_mismatch"}
    assert dict(entry.options) == newer
    reload.assert_not_called()


@pytest.mark.parametrize("kind", ["options", "reconfigure"])
@pytest.mark.parametrize("identity", [{"model": "HW-Q800F"}, {"wifi_mac": "invalid"}])
async def test_invalid_legacy_identity_can_be_confirmed(hass, entry, kind, identity):
    set_binding(hass, entry, identity)
    with (
        patch(f"{FLOW}._validate_local", new=AsyncMock(return_value=IDENTITY_A)),
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        form = await start(hass, entry, kind)
        form = await submit(hass, kind, form, TARGET_A)
        assert form["step_id"] == "confirm_local_identity"
        result = await submit(hass, kind, form, {})
    assert result["type"] == ("create_entry" if kind == "options" else "abort")
    assert entry.options["local_identity"] == IDENTITY_A


async def test_forget_awaits_coordinator_detach_before_clearing_runtime(hass, entry):
    client = SimpleNamespace(async_close=AsyncMock())
    coordinator = SimpleNamespace(
        async_detach_ocf=AsyncMock(), configure_ocf=MagicMock(), ocf=client
    )
    runtime = SimpleNamespace(ocf=client, coordinator=coordinator)

    async def detach():
        assert runtime.ocf is client
        coordinator.ocf = None

    coordinator.async_detach_ocf.side_effect = detach
    entry.runtime_data = runtime
    store = SimpleNamespace(configuration_lock=asyncio.Lock(), async_remove=AsyncMock())
    try:
        with (
            patch(f"{FLOW}.OcfCredentialStore", return_value=store),
            patch.object(hass.config_entries, "async_schedule_reload"),
        ):
            form = await start(hass, entry, "options")
            form = await submit(hass, "options", form, {"ocf_action": "forget"})
            result = await submit(hass, "options", form, {"confirm": True})
        assert result["type"] == "create_entry"
        coordinator.async_detach_ocf.assert_awaited_once()
        coordinator.configure_ocf.assert_not_called()
        client.async_close.assert_not_awaited()
        assert runtime.ocf is None
    finally:
        del entry.runtime_data


async def test_cancelled_forget_does_not_retain_detached_client_or_report_success(
    hass, entry
):
    client = SimpleNamespace(async_close=AsyncMock())
    coordinator = SimpleNamespace(ocf=client, async_detach_ocf=AsyncMock())
    runtime = SimpleNamespace(ocf=client, coordinator=coordinator)

    async def detach():
        coordinator.ocf = None
        raise asyncio.CancelledError

    coordinator.async_detach_ocf.side_effect = detach
    entry.runtime_data = runtime
    store = SimpleNamespace(configuration_lock=asyncio.Lock(), async_remove=AsyncMock())
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "ocf_configured": True}
    )
    try:
        with (
            patch(f"{FLOW}.OcfCredentialStore", return_value=store),
            patch.object(hass.config_entries, "async_schedule_reload") as reload,
        ):
            form = await start(hass, entry, "options")
            form = await submit(hass, "options", form, {"ocf_action": "forget"})
            with pytest.raises(asyncio.CancelledError):
                await submit(hass, "options", form, {"confirm": True})
        assert runtime.ocf is None
        store.async_remove.assert_awaited_once_with(locked=True)
        client.async_close.assert_not_awaited()
        reload.assert_not_called()
        assert entry.data["ocf_configured"] is True
    finally:
        del entry.runtime_data
