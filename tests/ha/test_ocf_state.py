"""OCF joins the real HA lifecycle without adding entities or cloud traffic."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol

from custom_components.samsung_soundbar.const import DOMAIN
from custom_components.samsung_soundbar.local_ocf import OcfError
from custom_components.samsung_soundbar.ocf_credentials import parse_credential_bundle

from .test_lifecycle import registered
from .test_ocf_credentials import bundle

PREFIX = "x.com.samsung.networkaudio."
WOOFER = "/sec/networkaudio/woofer"


@pytest.fixture
def ocf(hass, entry):
    client = MagicMock()
    client.available = True
    client.async_close = AsyncMock()
    client.async_write = AsyncMock()
    client.diagnostics.return_value = {"configured": True, "available": True}
    payloads = {
        WOOFER: {PREFIX + "woofer": 1, PREFIX + "connection": "on", "secret": "PRIVATE"}
    }
    client.async_read = AsyncMock(side_effect=lambda href: dict(payloads.get(href, {})))
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "ocf_configured": True}
    )
    with (
        patch(
            "custom_components.samsung_soundbar.OcfCredentialStore.async_load",
            new=AsyncMock(return_value=parse_credential_bundle(bundle())),
        ),
        patch(
            "custom_components.samsung_soundbar.LocalSoundbarOcfClient",
            return_value=client,
        ),
    ):
        yield client


async def test_polling_and_read_only_diagnostics_share_entry_transport(
    hass, entry, transports, ocf
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    assert coordinator.state.value("woofer_level") == 1
    assert coordinator.state.resolve("volume_level").source == "rpc"
    registry = registered(hass, entry)
    assert not any(key.endswith("_sw_woofer_level") for key in registry)
    initial_calls = ocf.async_read.await_count
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert ocf.async_read.await_count == initial_calls
    response = await hass.services.async_call(
        DOMAIN, "dump_ocf_status", {}, blocking=True, return_response=True
    )
    assert response["values"]["woofer_level"] == 1
    assert "PRIVATE" not in repr(response)
    ocf.async_write.assert_not_called()
    transports.auth.assert_not_awaited()
    await hass.config_entries.async_unload(entry.entry_id)
    ocf.async_close.assert_awaited_once()


async def test_ocf_outage_does_not_disable_rpc_or_remove_entities(
    hass, entry, transports, ocf
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    before = registered(hass, entry)
    ocf.async_read.side_effect = OcfError("auth")
    ocf.available = False
    await entry.runtime_data.coordinator.async_refresh_all()
    await hass.async_block_till_done()
    assert entry.runtime_data.device.available
    assert entry.runtime_data.device.volume_level == 0.08
    assert not entry.runtime_data.coordinator.state.snapshot().ocf_available
    assert registered(hass, entry) == before
    transports.auth.assert_not_awaited()


async def test_ocf_diagnostic_service_cannot_read_arbitrary_path(
    hass, entry, transports, ocf
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    count = ocf.async_read.await_count
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "dump_ocf_status",
            {"hrefs": ["/oic/sec/cred"]},
            blocking=True,
            return_response=True,
        )
    assert ocf.async_read.await_count == count


async def test_discovery_includes_only_projected_ocf_values(
    hass, entry, transports, ocf
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    response = await entry.runtime_data.device.async_dump_discovery_snapshot(
        include_raw_status=False
    )
    assert response["ocf"]["values"]["woofer_level"] == 1
    assert response["local"]["available"]
    assert "PRIVATE" not in repr(response)
    assert response["coordinator"]["transports"] == {
        "rpc": True,
        "ocf": True,
        "cloud": False,
    }
