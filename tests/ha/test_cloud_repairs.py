"""Cloud errors create real Repairs without taking LAN entities offline."""

from unittest.mock import AsyncMock, MagicMock

import pytest
import voluptuous as vol
from homeassistant.components import repairs
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsung_soundbar.cloud_errors import (
    CloudAccessError,
    CloudRateLimitError,
)
from custom_components.samsung_soundbar.cloud_repairs import CloudRepairs
from custom_components.samsung_soundbar.const import (
    CONF_CONTROL_MODE,
    CONF_LOCAL_HOST,
    CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
    CONTROL_MODE_LOCAL_ONLY,
    CONTROL_MODE_SMARTTHINGS_CLOUD,
    DOMAIN,
)
from custom_components.samsung_soundbar.subscription import (
    SmartThingsSubscriptionRuntime,
    async_remove_subscription,
    async_setup_subscription,
)

from .conftest import IDENTITY
from .test_lifecycle import registered


def cloud_mode(hass, entry, mode=CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS):
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            "token": {
                "access_token": "test-token",
                "scope": "r:devices:* x:devices:* r:locations:*",
            },
            "location_id": "test-location",
        },
        options={**entry.options, CONF_CONTROL_MODE: mode},
    )


def device(hass):
    return hass.data[DOMAIN].devices["existing-cloud-id"].device


def issue(registry, entry, kind):
    return registry.async_get_issue(DOMAIN, f"cloud_{kind}_{entry.entry_id}")


@pytest.mark.parametrize(
    "status,kind", [(403, "access"), (402, "payment"), (429, "limit")]
)
async def test_hybrid_startup_access_failure_keeps_lan_and_does_not_reauth(
    hass, entry, transports, issue_registry, status, kind
):
    cloud_mode(hass, entry)
    transports.api.get_device.side_effect = (
        CloudRateLimitError(120) if status == 429 else CloudAccessError(status)
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert device(hass).available
    assert not device(hass).cloud_available
    assert device(hass).volume_level == 0.08
    assert device(hass).sound_mode == "Game Pro"
    assert issue(issue_registry, entry, kind) is not None
    assert issue(issue_registry, entry, "auth") is None
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    before = registered(hass, entry)
    for _ in range(3):
        await device(hass).coordinator.async_refresh_all()
    transports.api.get_device_status.assert_not_awaited()

    transports.api.get_device.side_effect = None
    coordinator = device(hass).coordinator
    coordinator._cloud_retry_until = coordinator._next_cloud = 0
    await coordinator.async_refresh_all()
    await hass.async_block_till_done()
    assert issue(issue_registry, entry, kind) is None
    assert registered(hass, entry) == before
    assert entry.state is ConfigEntryState.LOADED
    assert device(hass).cloud_available


async def test_runtime_auth_failure_starts_one_reauth_and_lan_stays_available(
    hass, entry, transports, issue_registry
):
    cloud_mode(hass, entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    transports.api.get_device_status.side_effect = ConfigEntryAuthFailed("revoked")
    await device(hass).coordinator.async_refresh_all()
    await hass.async_block_till_done()
    assert issue(issue_registry, entry, "auth") is not None
    assert device(hass).available
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1 and flows[0]["context"]["source"] == "reauth"
    count = transports.api.get_device_status.await_count
    for _ in range(3):
        await device(hass).coordinator.async_refresh_all()
    await hass.async_block_till_done()
    assert transports.api.get_device_status.await_count == count
    assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 1


async def test_hybrid_startup_without_authorization_can_migrate_to_local_only(
    hass, entry, transports, issue_registry
):
    cloud_mode(hass, entry)
    hass.config_entries.async_update_entry(
        entry, data={key: value for key, value in entry.data.items() if key != "token"}
    )
    transports.api.get_device.side_effect = ConfigEntryAuthFailed(
        "Authorization is required"
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    before = registered(hass, entry)
    assert device(hass).available
    assert issue(issue_registry, entry, "auth") is not None
    assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 1
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_CONTROL_MODE: CONTROL_MODE_LOCAL_ONLY,
            CONF_LOCAL_HOST: "192.0.2.26",
        },
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert registered(hass, entry) == before
    assert entry.state is ConfigEntryState.LOADED
    assert device(hass).local_only
    assert issue(issue_registry, entry, "auth") is None
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_rate_limit_honors_backoff_even_for_forced_refresh(
    hass, entry, transports, issue_registry
):
    cloud_mode(hass, entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    transports.api.get_device_status.side_effect = CloudRateLimitError(120)
    await device(hass).coordinator.async_refresh_all()
    assert issue(issue_registry, entry, "limit") is not None
    coordinator = device(hass).coordinator
    assert coordinator._cloud_retry_until > coordinator._clock() + 115
    count = transports.api.get_device_status.await_count
    for _ in range(5):
        await coordinator.async_refresh_all()
    assert transports.api.get_device_status.await_count == count
    assert device(hass).available
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_cloud_setup_access_failure_is_retry_not_reauth(
    hass, entry, transports, issue_registry
):
    cloud_mode(hass, entry, CONTROL_MODE_SMARTTHINGS_CLOUD)
    transports.api.get_device.side_effect = CloudAccessError(403)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert issue(issue_registry, entry, "access") is not None
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_repeated_cloud_setup_outages_create_repair_then_clear_on_recovery(
    hass, entry, transports, issue_registry
):
    cloud_mode(hass, entry, CONTROL_MODE_SMARTTHINGS_CLOUD)
    transports.api.get_device.side_effect = ConfigEntryNotReady("offline")
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert issue(issue_registry, entry, "unavailable") is None
    assert not await hass.config_entries.async_reload(entry.entry_id)
    assert issue(issue_registry, entry, "unavailable") is None
    assert not await hass.config_entries.async_reload(entry.entry_id)
    assert issue(issue_registry, entry, "unavailable") is not None
    transports.api.get_device.side_effect = None
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert issue(issue_registry, entry, "unavailable") is None


async def test_transient_repairs_need_three_failures_and_are_entry_scoped(
    hass, entry, issue_registry
):
    other = MockConfigEntry(domain=DOMAIN, title="Other soundbar", data={})
    other.add_to_hass(hass)
    CloudRepairs(hass, other).failed(CloudAccessError(403))
    manager = CloudRepairs(hass, entry)
    for _ in range(2):
        manager.failed(ConfigEntryNotReady("temporarily offline"))
        assert issue(issue_registry, entry, "unavailable") is None
    manager.failed(ConfigEntryNotReady("temporarily offline"))
    assert issue(issue_registry, entry, "unavailable") is not None
    serialized = str(issue(issue_registry, entry, "unavailable"))
    assert "temporarily offline" not in serialized
    manager.recovered()
    assert issue(issue_registry, entry, "unavailable") is None
    assert issue(issue_registry, other, "access") is not None


async def test_repair_forwards_to_options_and_migration_clears_issue(
    hass, entry, transports, issue_registry
):
    cloud_mode(hass, entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    before = registered(hass, entry)
    CloudRepairs(hass, entry).failed(CloudAccessError(403))
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs.repairs_flow_manager(hass)
    result = await manager.async_init(
        DOMAIN, data={"issue_id": f"cloud_access_{entry.entry_id}"}
    )
    assert result["step_id"] == "choose"
    with pytest.raises(vol.Invalid):
        result["data_schema"]({"action": "reauth"})
    result = await manager.async_configure(result["flow_id"], {"action": "options"})
    assert result["reason"] == "reconfigure"
    flow_id = result["next_flow"][1]
    assert issue(issue_registry, entry, "access") is not None
    result = await hass.config_entries.options.async_configure(
        flow_id,
        {
            CONF_CONTROL_MODE: CONTROL_MODE_LOCAL_ONLY,
            CONF_LOCAL_HOST: "192.0.2.26",
        },
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert "token" not in entry.data
    assert entry.options["local_identity"] == IDENTITY
    assert registered(hass, entry) == before
    assert issue(issue_registry, entry, "access") is None
    assert entry.state is ConfigEntryState.LOADED


async def test_oauth_token_rotation_does_not_reload_entry(hass, entry, transports):
    cloud_mode(hass, entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = device(hass).coordinator
    auth_count = transports.auth.await_count
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "token": {"access_token": "rotated"}}
    )
    await hass.async_block_till_done()
    assert device(hass).coordinator is coordinator
    assert transports.auth.await_count == auth_count


async def test_auth_repair_forwards_to_real_reauth_flow(
    hass, entry, transports, issue_registry
):
    cloud_mode(hass, entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    CloudRepairs(hass, entry).failed(ConfigEntryAuthFailed("revoked"))
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs.repairs_flow_manager(hass)
    result = await manager.async_init(
        DOMAIN, data={"issue_id": f"cloud_auth_{entry.entry_id}"}
    )
    assert result["data_schema"]({"action": "reauth"}) == {"action": "reauth"}
    result = await manager.async_configure(result["flow_id"], {"action": "reauth"})
    assert result["reason"] == "reconfigure"
    assert issue(issue_registry, entry, "auth") is not None
    flow = hass.config_entries.flow.async_get(result["next_flow"][1])
    assert flow["context"]["source"] == "reauth"
    assert flow["context"]["entry_id"] == entry.entry_id


async def test_repair_migration_also_recovers_failed_cloud_setup(
    hass, entry, transports, issue_registry
):
    cloud_mode(hass, entry, CONTROL_MODE_SMARTTHINGS_CLOUD)
    transports.api.get_device.side_effect = CloudAccessError(403)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not entry.update_listeners
    count = transports.auth.await_count
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs.repairs_flow_manager(hass)
    result = await manager.async_init(
        DOMAIN, data={"issue_id": f"cloud_access_{entry.entry_id}"}
    )
    result = await manager.async_configure(result["flow_id"], {"action": "options"})
    result = await hass.config_entries.options.async_configure(
        result["next_flow"][1],
        {
            CONF_CONTROL_MODE: CONTROL_MODE_LOCAL_ONLY,
            CONF_LOCAL_HOST: "192.0.2.26",
        },
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert issue(issue_registry, entry, "access") is None
    assert "token" not in entry.data
    assert device(hass).local_only
    assert hass.data[DOMAIN].api is None
    assert hass.data[DOMAIN].auth_provider is None
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert transports.auth.await_count == count


async def test_entry_removal_clears_only_its_cloud_repairs(
    hass, entry, transports, issue_registry
):
    cloud_mode(hass, entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    other = MockConfigEntry(domain=DOMAIN, title="Other soundbar", data={})
    other.add_to_hass(hass)
    CloudRepairs(hass, entry).failed(CloudAccessError(403))
    CloudRepairs(hass, other).failed(CloudAccessError(403))
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert issue(issue_registry, entry, "access") is None
    assert issue(issue_registry, other, "access") is not None


async def test_cloud_repair_translations_are_available_to_ha(
    hass, entry, issue_registry
):
    from homeassistant.helpers.translation import async_get_translations

    CloudRepairs(hass, entry).failed(CloudAccessError(403))
    for language in ("en", "de"):
        translated = await async_get_translations(hass, language, "issues", {DOMAIN})
        prefix = f"component.{DOMAIN}.issues.cloud_access"
        assert translated[f"{prefix}.title"]
        assert "403" in translated[f"{prefix}.description"]
        assert translated[f"{prefix}.fix_flow.step.choose.data.action"]


async def test_optional_push_failures_and_cleanup_do_not_block_polling_or_migration(
    hass, entry, transports
):
    cloud_mode(hass, entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            "token": {
                "access_token": "test-token",
                "scope": "r:devices:* x:devices:* r:locations:* sse",
                "installed_app_id": "test-app",
            },
        },
    )
    await hass.async_block_till_done()
    transports.api.get_device_health = AsyncMock(
        side_effect=ConfigEntryNotReady("temporary failure")
    )
    transports.api.create_subscription = AsyncMock(side_effect=CloudAccessError(403))
    assert (
        await async_setup_subscription(hass, entry, transports.api, device(hass))
        is None
    )
    assert entry.state is ConfigEntryState.LOADED
    assert device(hass).available
    transports.api.delete_subscription = AsyncMock(side_effect=CloudAccessError(402))
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "subscription_id": "test-subscription"}
    )
    runtime = SmartThingsSubscriptionRuntime(
        client=transports.api, task=MagicMock(done=MagicMock(return_value=True))
    )
    await async_remove_subscription(entry, runtime)
    assert runtime.stopped
    transports.api.delete_subscription.assert_awaited_once_with("test-subscription")
