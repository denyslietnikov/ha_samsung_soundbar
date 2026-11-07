"""Native setup, isolation and recovery for the local transports."""

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.samsung_soundbar.const import DOMAIN

IDENTITY = {"wifi_mac": "02:00:00:00:00:10"}


def test_local_facade_exposes_metadata_without_obsolete_capability_api():
    from custom_components.samsung_soundbar.local_device import LocalDevice

    device = LocalDevice("original-id", model="HW-Q800F", firmware="test-version")
    assert device.device_id == "original-id"
    assert device.model == "HW-Q800F"
    assert device.firmware == "test-version"
    assert device.status.ocf_manufacturer_name == "Samsung Electronics"
    assert not hasattr(device.status, "has_capability")
    assert not hasattr(device.status, "attribute_value")
    device.status.volume = 8
    assert device.status.volume == 8


@pytest.fixture
def hass_storage():
    return {}


@pytest.fixture
def private_store(hass, entry, tmp_path):
    from custom_components.samsung_soundbar.ocf_credentials import OcfCredentialStore

    hass.config.config_dir = str(tmp_path)
    return OcfCredentialStore(hass, entry.entry_id)


def credential_bundle():
    return json.dumps(
        {
            "schema_version": 1,
            "target_device_uuid": "00000000-0000-4000-8000-000000000001",
            "client_identity_uuid": "00000000-0000-4000-8000-000000000002",
            "psk_encoding": "base64",
            "psk": base64.b64encode(bytes(range(16))).decode(),
        }
    )


async def import_credential(hass, entry):
    form = await hass.config_entries.options.async_init(entry.entry_id)
    form = await hass.config_entries.options.async_configure(
        form["flow_id"], {"ocf_action": "import"}
    )
    return await hass.config_entries.options.async_configure(
        form["flow_id"], {"credential_bundle": credential_bundle()}
    )


@pytest.fixture
def local_transports():
    rpc = MagicMock(create_token=AsyncMock(), call=AsyncMock())
    with (
        patch("custom_components.samsung_soundbar.config_flow.async_get_clientsession"),
        patch(
            "custom_components.samsung_soundbar.config_flow.async_read_local_identity",
            new=AsyncMock(return_value=IDENTITY),
        ),
        patch(
            "custom_components.samsung_soundbar.config_flow.LocalSoundbarRpcClient",
            return_value=rpc,
        ),
    ):
        yield rpc


@pytest.mark.parametrize("mode", ["local_only", "hybrid_local_ocf"])
async def test_native_flow_creates_both_modes_without_credentials(
    hass, local_transports, mode
):
    with patch(
        "custom_components.samsung_soundbar.async_setup_entry",
        new=AsyncMock(return_value=True),
    ):
        form = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        form = await hass.config_entries.flow.async_configure(
            form["flow_id"], {"control_mode": mode}
        )
        assert form["step_id"] == "local"
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            {
                "local_host": "192.0.2.10",
                "local_port": 1516,
                "local_verify_ssl": False,
                "local_timeout": 5,
                "device_name": "Soundbar",
            },
        )
    assert result["type"] == "create_entry"
    assert result["data"]["control_mode"] == mode
    assert result["data"]["device_id"] == "local:wifi_mac:02:00:00:00:00:10"
    assert result["data"]["local_identity"] == IDENTITY


@pytest.mark.parametrize("mode", ["local_only", "hybrid_local_ocf"])
async def test_setup_without_key_keeps_rpc_and_only_hybrid_warns(hass, entry, mode):
    from homeassistant.helpers import issue_registry as ir

    from custom_components.samsung_soundbar import async_setup_entry, async_unload_entry

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "control_mode": mode}
    )
    coordinator = SimpleNamespace(async_refresh=AsyncMock(), async_shutdown=AsyncMock())
    with (
        patch("custom_components.samsung_soundbar.LocalSoundbarRpcClient"),
        patch("custom_components.samsung_soundbar.async_get_clientsession"),
        patch("custom_components.samsung_soundbar.SoundbarDevice"),
        patch(
            "custom_components.samsung_soundbar.SoundbarCoordinator",
            return_value=coordinator,
        ),
        patch("custom_components.samsung_soundbar.OcfCredentialStore") as store,
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new=AsyncMock()
        ),
        patch.object(
            hass.config_entries,
            "async_unload_platforms",
            new=AsyncMock(return_value=True),
        ),
    ):
        assert await async_setup_entry(hass, entry)
        assert entry.runtime_data.ocf is None
        assert await async_unload_entry(hass, entry)
    store.assert_not_called()
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"ocf_credential_{entry.entry_id}"
    )
    assert (issue is not None) == (mode == "hybrid_local_ocf")
    if issue:
        assert issue.translation_key == "ocf_missing_credential"
    coordinator.async_shutdown.assert_awaited_once()


async def test_hostless_entry_reports_reconfigure_repair(hass, entry):
    from homeassistant.exceptions import ConfigEntryNotReady
    from homeassistant.helpers import issue_registry as ir

    from custom_components.samsung_soundbar import async_setup_entry

    hass.config_entries.async_update_entry(
        entry, options={"control_mode": "local_only"}
    )
    with pytest.raises(ConfigEntryNotReady, match="Reconfigure"):
        await async_setup_entry(hass, entry)
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"local_configuration_{entry.entry_id}"
    )
    assert issue is not None
    assert issue.data["entry_id"] == entry.entry_id


async def test_reconfigure_hostless_entry_confirms_binding_without_changing_ids(
    hass, entry, local_transports
):
    hass.config_entries.async_update_entry(
        entry, options={"control_mode": "local_only"}
    )
    original_id, unique_id = entry.entry_id, entry.unique_id
    with patch.object(hass.config_entries, "async_schedule_reload"):
        form = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": "reconfigure",
                "entry_id": entry.entry_id,
            },
        )
        assert form["step_id"] == "reconfigure"
        form = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            {
                "local_host": "192.0.2.10",
                "control_mode": "local_only",
                "local_port": 1516,
                "local_timeout": 5,
                "local_verify_ssl": False,
            },
        )
        assert form["step_id"] == "confirm_local_identity"
        result = await hass.config_entries.flow.async_configure(form["flow_id"], {})
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert entry.entry_id == original_id
    assert entry.unique_id == unique_id == entry.data["device_id"]
    assert entry.options["local_identity"] == IDENTITY


async def test_rpc_only_with_retained_credential_never_opens_store(hass, entry):
    from custom_components.samsung_soundbar import _async_setup_ocf

    hass.config_entries.async_update_entry(
        entry,
        version=2,
        minor_version=0,
        data={**entry.data, "ocf_configured": True},
        options={**entry.options, "control_mode": "local_only"},
    )
    entry.runtime_data = SimpleNamespace(ocf=None)
    with patch("custom_components.samsung_soundbar.OcfCredentialStore") as store:
        await _async_setup_ocf(hass, entry)
    store.assert_not_called()
    assert entry.data["ocf_configured"] is True
    del entry.runtime_data


async def test_setup_failure_shuts_down_owned_coordinator(hass, entry):
    from custom_components.samsung_soundbar import async_setup_entry

    coordinator = SimpleNamespace(async_refresh=AsyncMock(), async_shutdown=AsyncMock())
    with (
        patch("custom_components.samsung_soundbar.LocalSoundbarRpcClient"),
        patch("custom_components.samsung_soundbar.async_get_clientsession"),
        patch("custom_components.samsung_soundbar.SoundbarDevice"),
        patch(
            "custom_components.samsung_soundbar.SoundbarCoordinator",
            return_value=coordinator,
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new=AsyncMock(side_effect=RuntimeError("platform failure")),
        ),
        pytest.raises(RuntimeError, match="platform failure"),
    ):
        await async_setup_entry(hass, entry)
    coordinator.async_shutdown.assert_awaited_once()
    assert not hasattr(entry, "runtime_data")


@pytest.mark.parametrize("saved", [True, False])
async def test_import_authenticates_then_enables_hybrid_without_reidentification(
    hass, entry, local_transports, private_store, saved
):
    from homeassistant.helpers import issue_registry as ir

    options = {**entry.options, "control_mode": "local_only"}
    if saved:
        options["local_identity"] = IDENTITY
    else:
        options.pop("local_identity", None)
    hass.config_entries.async_update_entry(
        entry, version=2, minor_version=0, options=options
    )
    original_id, unique_id = entry.entry_id, entry.unique_id
    client = MagicMock(async_validate_candidate=AsyncMock(), async_close=AsyncMock())
    with (
        patch(
            "custom_components.samsung_soundbar.config_flow.LocalSoundbarOcfClient",
            return_value=client,
        ),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        result = await import_credential(hass, entry)
    reload.assert_not_called()
    assert result["type"] == "create_entry"
    assert entry.options["control_mode"] == "hybrid_local_ocf"
    assert entry.options["local_identity"] == IDENTITY
    assert entry.entry_id == original_id
    assert entry.unique_id == unique_id == entry.data["device_id"]
    assert entry.data["ocf_configured"] is True
    assert (await private_store.async_load()).psk == bytes(range(16))
    assert json.loads(credential_bundle())["psk"] not in repr(result) + repr(
        entry.data
    ) + repr(entry.options)
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, f"ocf_credential_{entry.entry_id}")
        is None
    )
    client.async_validate_candidate.assert_awaited_once()
    client.async_close.assert_awaited_once()


async def test_import_rechecks_own_device_after_credential_validation(
    hass, entry, local_transports, private_store
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "local_identity": IDENTITY}
    )

    async def validate(_credential):
        other = MockConfigEntry(
            domain=DOMAIN,
            unique_id="other-id",
            version=2,
            data={"device_id": "other-id"},
            options={"local_host": "192.0.2.99", "local_identity": IDENTITY},
        )
        other.add_to_hass(hass)

    client = MagicMock(
        async_validate_candidate=AsyncMock(side_effect=validate),
        async_close=AsyncMock(),
    )
    with (
        patch(
            "custom_components.samsung_soundbar.config_flow.LocalSoundbarOcfClient",
            return_value=client,
        ),
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        result = await import_credential(hass, entry)
    assert result["type"] == "form"
    assert await private_store.async_load() is None
    assert not entry.data.get("ocf_configured")


async def test_forget_retains_hybrid_and_warns(
    hass, entry, local_transports, private_store
):
    from homeassistant.helpers import issue_registry as ir

    from custom_components.samsung_soundbar.ocf_credentials import (
        parse_credential_bundle,
    )

    await private_store.async_save(parse_credential_bundle(credential_bundle()))
    hass.config_entries.async_update_entry(
        entry,
        version=2,
        minor_version=0,
        data={**entry.data, "ocf_configured": True},
        options={**entry.options, "control_mode": "hybrid_local_ocf"},
    )
    with patch.object(hass.config_entries, "async_schedule_reload"):
        form = await hass.config_entries.options.async_init(entry.entry_id)
        form = await hass.config_entries.options.async_configure(
            form["flow_id"], {"ocf_action": "forget"}
        )
        result = await hass.config_entries.options.async_configure(
            form["flow_id"], {"confirm": True}
        )
    assert result["type"] == "create_entry"
    assert entry.options["control_mode"] == "hybrid_local_ocf"
    assert await private_store.async_load() is None
    assert (
        ir.async_get(hass)
        .async_get_issue(DOMAIN, f"ocf_credential_{entry.entry_id}")
        .translation_key
        == "ocf_missing_credential"
    )


async def test_options_preserve_retained_marker_in_rpc_only(
    hass, entry, local_transports
):
    hass.config_entries.async_update_entry(
        entry,
        version=2,
        minor_version=0,
        options={
            **entry.options,
            "control_mode": "local_only",
            "ocf_configured": True,
            "local_identity": IDENTITY,
        },
    )
    with (
        patch("custom_components.samsung_soundbar.OcfCredentialStore") as store,
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        form = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            form["flow_id"], {"control_mode": "local_only"}
        )
    assert result["type"] == "create_entry"
    assert entry.options["ocf_configured"] is True
    store.assert_not_called()
    reload.assert_not_called()
