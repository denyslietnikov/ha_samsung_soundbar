"""Entry-only migration to the two local transports."""

from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.samsung_soundbar as integration
from custom_components.samsung_soundbar.const import DOMAIN


@pytest.mark.parametrize(
    ("mode", "configured", "expected"),
    [
        ("hybrid_local_smartthings", False, "hybrid_local_ocf"),
        ("local_only", True, "hybrid_local_ocf"),
        ("local_only", False, "local_only"),
        ("smartthings_cloud", False, "local_only"),
        ("smartthings_cloud", True, "hybrid_local_ocf"),
    ],
)
async def test_entry_migration_preserves_identity_and_never_opens_store(
    hass, mode, configured, expected
):
    identity = {"wifi_mac": "02:00:00:00:00:01"}
    obsolete = {
        "token": {"access_token": "synthetic"},
        "api_key": "synthetic",
        "auth_implementation": "legacy",
        "location_id": "old-location",
        "installed_app_id": "old-app",
        "subscription_id": "old-subscription",
        "cloud_integration": True,
        "local_fallback_to_cloud": True,
        "settings_eq": True,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="original-id",
        version=1,
        minor_version=1,
        data={
            "device_id": "original-id",
            "local_identity": identity,
            "ocf_configured": configured,
            **obsolete,
        },
        options={"control_mode": mode, "local_host": "192.0.2.10", **obsolete},
    )
    entry.add_to_hass(hass)
    original_entry_id = entry.entry_id
    with patch("custom_components.samsung_soundbar.OcfCredentialStore") as store:
        assert hasattr(integration, "async_migrate_entry"), (
            "Native migration is required"
        )
        assert await integration.async_migrate_entry(hass, entry)
        assert await integration.async_migrate_entry(hass, entry)
    store.assert_not_called()
    assert entry.entry_id == original_entry_id
    assert entry.unique_id == entry.data["device_id"] == "original-id"
    assert entry.data["local_identity"] == identity
    assert entry.data["ocf_configured"] is configured
    assert entry.options["control_mode"] == expected
    assert not obsolete.keys() & entry.data.keys()
    assert not obsolete.keys() & entry.options.keys()
    assert entry.version == 2


async def test_hostless_migration_retains_original_id(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="original-id",
        version=1,
        data={"device_id": "original-id", "token": {"access_token": "synthetic"}},
    )
    entry.add_to_hass(hass)
    assert hasattr(integration, "async_migrate_entry"), "Native migration is required"
    assert await integration.async_migrate_entry(hass, entry)
    assert entry.data["device_id"] == entry.unique_id == "original-id"
    assert not entry.options.get("local_host")
