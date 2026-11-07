"""Changing local transports keeps the existing device and OCF credential."""

from unittest.mock import patch

import custom_components.samsung_soundbar as integration

from . import test_ocf_state as ocf_fixtures
from .test_lifecycle import registered

ocf = ocf_fixtures.ocf


async def select_mode(hass, entry, mode):
    form = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            "control_mode": mode,
            "local_host": entry.options["local_host"],
            "local_port": 1516,
            "local_verify_ssl": False,
            "local_timeout": 8,
            "device_volume": 100,
            "ocf_action": "keep",
        },
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()


async def test_switch_rpc_hybrid_preserves_ids_and_credential_without_rpc_ocf_reads(
    hass, entry, transports, ocf
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    before = registered(hass, entry)
    identity = entry.options["local_identity"]
    original_entry_id, original_unique_id = entry.entry_id, entry.unique_id
    with patch.object(integration.OcfCredentialStore, "async_load") as load:
        await select_mode(hass, entry, "local_only")
        load.assert_not_called()
    assert entry.runtime_data.ocf is None
    assert entry.runtime_data.device.available
    assert entry.runtime_data.device.volume_level == 0.08
    assert entry.data["ocf_configured"] is True
    assert registered(hass, entry) == before
    reads = ocf.async_read.await_count
    await entry.runtime_data.coordinator.async_refresh_all()
    assert ocf.async_read.await_count == reads
    await select_mode(hass, entry, "hybrid_local_ocf")
    assert entry.runtime_data.ocf is ocf
    assert entry.runtime_data.device.woofer_level == 1
    assert registered(hass, entry) == before
    assert entry.options["local_identity"] == identity
    assert (entry.entry_id, entry.unique_id) == (original_entry_id, original_unique_id)
