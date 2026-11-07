"""A retained address is not proof of physical ownership after migration."""

import pytest
from homeassistant.helpers import issue_registry as ir

from custom_components.samsung_soundbar.const import DOMAIN


@pytest.mark.parametrize(
    "identity", [None, {}, {"model": "HW-Q800F"}, {"wifi_mac": "invalid"}]
)
async def test_unbound_or_invalid_identity_cannot_activate_rpc(
    hass, entry, transports, identity
):
    options = {**entry.options, "local_identity": identity}
    hass.config_entries.async_update_entry(
        entry, version=2, minor_version=0, options=options
    )
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    transports.factory.assert_not_called()
    assert not hasattr(entry, "runtime_data")
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"local_configuration_{entry.entry_id}"
    )


async def test_migrated_remote_entry_cannot_activate_its_unverified_old_host(
    hass, entry, transports
):
    hass.config_entries.async_update_entry(
        entry,
        version=1,
        minor_version=1,
        data={**entry.data, "token": {"access_token": "synthetic"}},
        options={"control_mode": "smartthings_cloud", "local_host": "192.0.2.99"},
    )
    original_entry_id, original_unique_id = entry.entry_id, entry.unique_id
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    transports.factory.assert_not_called()
    assert (entry.entry_id, entry.unique_id) == (original_entry_id, original_unique_id)
    assert "token" not in entry.data
