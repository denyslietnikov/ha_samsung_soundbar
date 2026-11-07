"""Actual HA storage, options and lifecycle with synthetic OCF credentials."""

import asyncio
import base64
import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.samsung_soundbar.const import DOMAIN

TARGET = "00000000-0000-4000-8000-000000000001"
CLIENT = "00000000-0000-4000-8000-000000000002"
PSK = bytes(range(16))


@pytest.fixture
def hass_storage():
    """Exercise actual HA Store disk writes instead of the plugin's memory mock."""
    return {}


def bundle(key=PSK):
    return json.dumps(
        {
            "schema_version": 1,
            "target_device_uuid": TARGET,
            "client_identity_uuid": CLIENT,
            "psk_encoding": "base64",
            "psk": base64.b64encode(key).decode(),
        }
    )


@pytest.fixture
def store(hass, entry, tmp_path):
    from custom_components.samsung_soundbar.ocf_credentials import OcfCredentialStore

    hass.config.config_dir = str(tmp_path)
    return OcfCredentialStore(hass, entry.entry_id)


async def test_real_private_store_roundtrip_and_separate_entries(hass, entry, store):
    from custom_components.samsung_soundbar.ocf_credentials import (
        OcfCredentialStore,
        parse_credential_bundle,
    )

    value = parse_credential_bundle(bundle())
    await store.async_save(value)
    assert await OcfCredentialStore(hass, entry.entry_id).async_load() == value
    assert Path(store.store.path).stat().st_mode & 0o777 == 0o600
    other = OcfCredentialStore(hass, "other-entry")
    assert await other.async_load() is None
    await other.async_remove()
    assert await store.async_load() == value
    assert "psk" not in repr(entry.data) + repr(entry.options)


async def test_silent_store_write_failure_keeps_old_key(hass, store):
    from custom_components.samsung_soundbar.ocf_credentials import (
        OcfCredentialError,
        parse_credential_bundle,
    )

    old, new = (
        parse_credential_bundle(bundle()),
        parse_credential_bundle(bundle(b"a" * 16)),
    )
    await store.async_save(old)
    with (
        patch.object(store.store, "async_save", new=AsyncMock()),
        pytest.raises(OcfCredentialError),
    ):
        await store.async_save(new)
    assert await store.async_load() == old


async def test_store_compare_and_swap_rejects_stale_import(store):
    from custom_components.samsung_soundbar.ocf_credentials import (
        OcfCredentialError,
        parse_credential_bundle,
    )

    old, new = (
        parse_credential_bundle(bundle()),
        parse_credential_bundle(bundle(b"a" * 16)),
    )
    await store.async_save(old)
    with pytest.raises(OcfCredentialError):
        await store.async_save(new, expected=None)
    assert await store.async_load() == old


async def test_options_import_has_no_secret_defaults_and_no_cloud(
    hass, entry, transports, store
):
    client = MagicMock()
    client.async_validate_candidate = AsyncMock()
    client.async_close = AsyncMock()
    with patch(
        "custom_components.samsung_soundbar.config_flow.LocalSoundbarOcfClient",
        return_value=client,
    ):
        form = await hass.config_entries.options.async_init(entry.entry_id)
        form = await hass.config_entries.options.async_configure(
            form["flow_id"], {"ocf_action": "import"}
        )
        assert form["step_id"] == "ocf_import"
        assert base64.b64encode(PSK).decode() not in repr(form)
        result = await hass.config_entries.options.async_configure(
            form["flow_id"], {"credential_bundle": bundle()}
        )
    assert result["type"] == "create_entry"
    assert entry.data["ocf_configured"] is True
    assert (await store.async_load()).psk == PSK
    assert "psk" not in repr(entry.data) + repr(entry.options)
    assert "credential_bundle" not in result["data"]
    assert "ocf_action" not in result["data"]
    client.async_validate_candidate.assert_awaited_once()
    client.async_close.assert_awaited_once()
    transports.auth.assert_not_awaited()


async def test_failed_import_keeps_previous_store_and_never_echoes_input(
    hass, entry, transports, store
):
    from custom_components.samsung_soundbar.local_ocf import OcfError
    from custom_components.samsung_soundbar.ocf_credentials import (
        parse_credential_bundle,
    )

    old = parse_credential_bundle(bundle())
    await store.async_save(old)
    client = MagicMock()
    client.async_validate_candidate = AsyncMock(side_effect=OcfError("auth"))
    client.async_close = AsyncMock()
    with patch(
        "custom_components.samsung_soundbar.config_flow.LocalSoundbarOcfClient",
        return_value=client,
    ):
        form = await hass.config_entries.options.async_init(entry.entry_id)
        form = await hass.config_entries.options.async_configure(
            form["flow_id"], {"ocf_action": "import"}
        )
        result = await hass.config_entries.options.async_configure(
            form["flow_id"], {"credential_bundle": bundle(b"a" * 16)}
        )
    assert result["type"] == "form"
    assert result["errors"]["base"] == "ocf_auth"
    assert base64.b64encode(b"a" * 16).decode() not in repr(result)
    assert await store.async_load() == old
    assert "ocf_configured" not in entry.data


async def test_cancel_and_forget_confirmation(hass, entry, store):
    from custom_components.samsung_soundbar.ocf_credentials import (
        parse_credential_bundle,
    )

    value = parse_credential_bundle(bundle())
    await store.async_save(value)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "ocf_configured": True}
    )
    form = await hass.config_entries.options.async_init(entry.entry_id)
    form = await hass.config_entries.options.async_configure(
        form["flow_id"], {"ocf_action": "forget"}
    )
    assert form["step_id"] == "ocf_forget"
    hass.config_entries.options.async_abort(form["flow_id"])
    assert await store.async_load() == value
    form = await hass.config_entries.options.async_init(entry.entry_id)
    form = await hass.config_entries.options.async_configure(
        form["flow_id"], {"ocf_action": "forget"}
    )
    result = await hass.config_entries.options.async_configure(
        form["flow_id"], {"confirm": True}
    )
    assert result["type"] == "create_entry"
    assert await store.async_load() is None
    assert entry.data.get("ocf_configured") is not True


async def test_runtime_has_lazy_ocf_without_changing_rpc_entities(
    hass, entry, transports, store
):
    from custom_components.samsung_soundbar.ocf_credentials import (
        parse_credential_bundle,
    )

    await store.async_save(parse_credential_bundle(bundle()))
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "ocf_configured": True}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.runtime_data.ocf is not None
    assert not entry.runtime_data.ocf.available
    assert entry.runtime_data.device.volume_level == 0.08
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert await store.async_load() is not None
    transports.auth.assert_not_awaited()


async def test_corrupt_store_does_not_stop_rpc_and_offers_local_repair(
    hass, entry, transports, store
):
    from homeassistant.helpers import issue_registry as ir

    await store.store.async_save({"psk": "invalid"})
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "ocf_configured": True}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.runtime_data.ocf is None
    assert entry.runtime_data.device.available
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"ocf_credential_{entry.entry_id}"
    )
    assert issue is not None
    assert "psk" not in repr(issue.data)
    transports.auth.assert_not_awaited()


async def test_entry_removal_cleans_only_its_credential(hass, entry, store):
    from custom_components.samsung_soundbar import async_remove_entry
    from custom_components.samsung_soundbar.ocf_credentials import (
        parse_credential_bundle,
    )

    await store.async_save(parse_credential_bundle(bundle()))
    await async_remove_entry(hass, entry)
    assert await store.async_load() is None


async def test_cancelled_import_does_not_save_key(hass, entry, transports, store):
    form = await hass.config_entries.options.async_init(entry.entry_id)
    form = await hass.config_entries.options.async_configure(
        form["flow_id"], {"ocf_action": "import"}
    )
    hass.config_entries.options.async_abort(form["flow_id"])
    assert await store.async_load() is None
    assert "ocf_configured" not in entry.data


async def test_failed_persistence_does_not_enable_ocf_or_change_old_key(
    hass, entry, transports, store
):
    from custom_components.samsung_soundbar.ocf_credentials import (
        OcfCredentialError,
        OcfCredentialStore,
        parse_credential_bundle,
    )

    old = parse_credential_bundle(bundle())
    await store.async_save(old)
    client = MagicMock()
    client.async_validate_candidate = AsyncMock()
    client.async_close = AsyncMock()
    with (
        patch(
            "custom_components.samsung_soundbar.config_flow.LocalSoundbarOcfClient",
            return_value=client,
        ),
        patch.object(
            OcfCredentialStore,
            "async_save",
            new=AsyncMock(side_effect=OcfCredentialError),
        ),
    ):
        form = await hass.config_entries.options.async_init(entry.entry_id)
        form = await hass.config_entries.options.async_configure(
            form["flow_id"], {"ocf_action": "import"}
        )
        result = await hass.config_entries.options.async_configure(
            form["flow_id"], {"credential_bundle": bundle(b"a" * 16)}
        )
    assert result["errors"]["base"] == "ocf_storage"
    assert await store.async_load() == old
    assert "ocf_configured" not in entry.data


async def test_replacement_schedules_one_reload_without_secret_in_entry(
    hass, entry, transports, store
):
    from custom_components.samsung_soundbar.ocf_credentials import (
        parse_credential_bundle,
    )

    await store.async_save(parse_credential_bundle(bundle()))
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "ocf_configured": True}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    before_id = entry.unique_id
    with (
        patch.object(
            entry.runtime_data.ocf, "async_validate_candidate", new=AsyncMock()
        ),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        form = await hass.config_entries.options.async_init(entry.entry_id)
        form = await hass.config_entries.options.async_configure(
            form["flow_id"], {"ocf_action": "import"}
        )
        result = await hass.config_entries.options.async_configure(
            form["flow_id"], {"credential_bundle": bundle(b"a" * 16)}
        )
    assert result["type"] == "create_entry"
    reload.assert_called_once_with(entry.entry_id)
    assert entry.unique_id == before_id
    assert (await store.async_load()).psk == b"a" * 16
    assert base64.b64encode(b"a" * 16).decode() not in repr(entry.data) + repr(
        entry.options
    )


async def test_ocf_repair_opens_import_not_oauth(hass, entry, transports, store):
    from homeassistant.components import repairs
    from homeassistant.components.repairs import FlowType
    from homeassistant.setup import async_setup_component

    from custom_components.samsung_soundbar.ocf_repairs import update_ocf_issue

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    update_ocf_issue(hass, entry, "auth")
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs.repairs_flow_manager(hass)
    result = await manager.async_init(
        DOMAIN, data={"issue_id": f"ocf_credential_{entry.entry_id}"}
    )
    assert result["step_id"] == "choose", repr(result)
    result = await manager.async_configure(result["flow_id"], {})
    assert result.get("reason") == "reconfigure", repr(result)
    assert result["next_flow"][0] is FlowType.OPTIONS_FLOW
    transports.auth.assert_not_awaited()


async def test_ocf_health_never_changes_cloud_issues(hass, entry):
    from homeassistant.helpers import issue_registry as ir

    from custom_components.samsung_soundbar.ocf_repairs import update_ocf_issue

    registry = ir.async_get(hass)
    ir.async_create_issue(
        hass,
        DOMAIN,
        "cloud_existing",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="cloud_access",
    )
    update_ocf_issue(hass, entry, "auth")
    update_ocf_issue(hass, entry, None)
    assert registry.async_get_issue(DOMAIN, "cloud_existing") is not None
    assert registry.async_get_issue(DOMAIN, f"ocf_credential_{entry.entry_id}") is None


async def test_removed_entry_cannot_recreate_an_orphan_credential(hass, entry, store):
    from custom_components.samsung_soundbar.ocf_credentials import (
        OcfCredentialError,
        parse_credential_bundle,
    )

    assert await hass.config_entries.async_remove(entry.entry_id)
    with pytest.raises(OcfCredentialError):
        await store.async_save(parse_credential_bundle(bundle()))
    assert not Path(store.store.path).exists()


async def test_verification_failure_rolls_back_a_successful_write(store):
    from custom_components.samsung_soundbar.ocf_credentials import (
        OcfCredentialError,
        parse_credential_bundle,
    )

    old, new = (
        parse_credential_bundle(bundle()),
        parse_credential_bundle(bundle(b"a" * 16)),
    )
    await store.async_save(old)
    original = store._read_persisted
    calls = 0

    def fail_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("verification failed")
        return original()

    with (
        patch.object(store, "_read_persisted", side_effect=fail_once),
        pytest.raises(OcfCredentialError),
    ):
        await store.async_save(new)
    assert await store.async_load() == old


async def test_cancellation_during_disk_write_drains_and_rolls_back(store):
    from custom_components.samsung_soundbar.ocf_credentials import (
        parse_credential_bundle,
    )

    old, new = (
        parse_credential_bundle(bundle()),
        parse_credential_bundle(bundle(b"a" * 16)),
    )
    await store.async_save(old)
    started, release = threading.Event(), threading.Event()
    original = store.store._write_prepared_data

    def delayed(*args):
        started.set()
        release.wait(3)
        return original(*args)

    with patch.object(store.store, "_write_prepared_data", side_effect=delayed):
        task = asyncio.create_task(store.async_save(new))
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await hass_executor_done(store.hass)
    assert await store.async_load() == old


async def hass_executor_done(hass):
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_abort_during_validation_never_saves(hass, entry, transports, store):
    from homeassistant.data_entry_flow import UnknownFlow

    started, release = asyncio.Event(), asyncio.Event()

    async def validate(_credential):
        started.set()
        await release.wait()

    client = MagicMock()
    client.async_validate_candidate = validate
    client.async_close = AsyncMock()
    with patch(
        "custom_components.samsung_soundbar.config_flow.LocalSoundbarOcfClient",
        return_value=client,
    ):
        form = await hass.config_entries.options.async_init(entry.entry_id)
        form = await hass.config_entries.options.async_configure(
            form["flow_id"], {"ocf_action": "import"}
        )
        task = asyncio.create_task(
            hass.config_entries.options.async_configure(
                form["flow_id"], {"credential_bundle": bundle()}
            )
        )
        await started.wait()
        hass.config_entries.options.async_abort(form["flow_id"])
        release.set()
        try:
            await task
        except UnknownFlow:
            pass
    assert await store.async_load() is None
    assert "ocf_configured" not in entry.data


async def test_reauth_retains_ocf_binding(hass, entry, transports, store):
    from custom_components.samsung_soundbar.config_flow import SamsungSoundbarConfigFlow
    from custom_components.samsung_soundbar.ocf_credentials import (
        parse_credential_bundle,
    )

    await store.async_save(parse_credential_bundle(bundle()))
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "ocf_configured": True}
    )
    flow = SamsungSoundbarConfigFlow()
    flow.hass = hass
    flow.context = {"source": "reauth", "entry_id": entry.entry_id}
    flow._devices = {entry.data["device_id"]: entry.title}
    flow._device_locations = {entry.data["device_id"]: "test-location"}
    with patch.object(hass.config_entries, "async_reload", new=AsyncMock()):
        await flow._async_finish_reauth({"token": {"access_token": "synthetic"}})
    assert entry.data.get("ocf_configured") is True
    assert await store.async_load() is not None


async def test_interrupted_transaction_recovers_previous_private_key(store):
    from custom_components.samsung_soundbar.ocf_credentials import (
        parse_credential_bundle,
    )

    old, new = (
        parse_credential_bundle(bundle()),
        parse_credential_bundle(bundle(b"a" * 16)),
    )
    await store.async_save(old)
    await store.rollback_store.async_save({"previous": old.to_storage()})
    await store.store.async_save(new.to_storage())
    assert await store.async_load() == old
    assert not Path(store.rollback_store.path).exists()


async def test_concurrent_imports_do_not_open_overlapping_peers(
    hass, entry, transports, store
):
    active, maximum = 0, 0

    async def validate(_credential):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1

    def client_factory(*args, **kwargs):
        client = MagicMock()
        client.async_validate_candidate = validate
        client.async_close = AsyncMock()
        return client

    forms = []
    for _ in range(2):
        form = await hass.config_entries.options.async_init(entry.entry_id)
        forms.append(
            await hass.config_entries.options.async_configure(
                form["flow_id"], {"ocf_action": "import"}
            )
        )
    with patch(
        "custom_components.samsung_soundbar.config_flow.LocalSoundbarOcfClient",
        side_effect=client_factory,
    ):
        results = await asyncio.gather(
            *(
                hass.config_entries.options.async_configure(
                    form["flow_id"], {"credential_bundle": bundle()}
                )
                for form in forms
            )
        )
    assert all(result["type"] == "create_entry" for result in results)
    assert maximum == 1


@pytest.mark.parametrize("operation", ["forget", "remove"])
async def test_forget_or_remove_during_validation_cannot_resurrect_key(
    hass, entry, transports, store, operation
):
    started, release = asyncio.Event(), asyncio.Event()

    async def validate(_credential):
        started.set()
        await release.wait()

    client = MagicMock()
    client.async_validate_candidate = validate
    client.async_close = AsyncMock()
    with patch(
        "custom_components.samsung_soundbar.config_flow.LocalSoundbarOcfClient",
        return_value=client,
    ):
        form = await hass.config_entries.options.async_init(entry.entry_id)
        form = await hass.config_entries.options.async_configure(
            form["flow_id"], {"ocf_action": "import"}
        )
        task = asyncio.create_task(
            hass.config_entries.options.async_configure(
                form["flow_id"], {"credential_bundle": bundle()}
            )
        )
        await started.wait()
        if operation == "remove":
            deletion = asyncio.create_task(
                hass.config_entries.async_remove(entry.entry_id)
            )
        else:
            forget = await hass.config_entries.options.async_init(entry.entry_id)
            forget = await hass.config_entries.options.async_configure(
                forget["flow_id"], {"ocf_action": "forget"}
            )
            deletion = asyncio.create_task(
                hass.config_entries.options.async_configure(
                    forget["flow_id"], {"confirm": True}
                )
            )
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(task, deletion, return_exceptions=True)
    assert await store.async_load() is None
    if operation == "forget":
        assert entry.data.get("ocf_configured") is not True
