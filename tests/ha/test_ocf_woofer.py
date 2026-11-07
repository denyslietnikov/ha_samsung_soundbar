"""Real HA Woofer slider, stable IDs, route availability and service validation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.samsung_soundbar.const import DOMAIN
from custom_components.samsung_soundbar.local_ocf import OcfError, OcfWriteResult
from custom_components.samsung_soundbar.ocf_profiles import Q800F_WOOFER

from . import test_ocf_state as ocf_fixtures
from .test_lifecycle import registered

ocf = ocf_fixtures.ocf

HREF = "/sec/networkaudio/woofer"
FIELD = "x.com.samsung.networkaudio.woofer"
CONNECTION = "x.com.samsung.networkaudio.connection"
UNIQUE = "existing-cloud-id_sw_woofer_level"


@pytest.fixture
def woofer(ocf):
    values = {"level": -6, "connection": "on"}
    ocf.validated = True
    ocf.async_read = AsyncMock(
        side_effect=lambda href: (
            {FIELD: values["level"], CONNECTION: values["connection"]}
            if href == HREF
            else {}
        )
    )

    async def write(href, payload, *, woofer_profile):
        assert href == HREF
        assert woofer_profile is Q800F_WOOFER
        values["level"] = payload[FIELD]
        return OcfWriteResult(True, True, False, 68)

    ocf.async_write = AsyncMock(side_effect=write)
    return SimpleNamespace(client=ocf, values=values)


async def test_existing_number_id_becomes_local_slider_with_actual_db_readback(
    hass, entry, transports, woofer
):
    old = er.async_get(hass).async_get_or_create(
        "number",
        DOMAIN,
        UNIQUE,
        suggested_object_id="my_existing_woofer",
        config_entry=entry,
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = registered(hass, entry)[UNIQUE][0]
    assert entity_id == old.entity_id
    state = hass.states.get(entity_id)
    assert state.state == "-6"
    assert (
        state.attributes["min"],
        state.attributes["max"],
        state.attributes["step"],
        state.attributes["mode"],
    ) == (-12, 6, 1, "slider")
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": -7}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "-7"
    woofer.client.async_write.assert_awaited_once_with(
        HREF, {FIELD: -7}, woofer_profile=Q800F_WOOFER
    )
    before = registered(hass, entry)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert registered(hass, entry) == before
    assert hass.states.get(entity_id).state == "-7"


async def test_fractional_level_is_not_truncated_by_number_service(
    hass, entry, transports, woofer
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = registered(hass, entry)[UNIQUE][0]
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity_id, "value": -6.5},
            blocking=True,
        )
    woofer.client.async_write.assert_not_awaited()


async def test_external_app_change_and_disconnect_reach_same_number(
    hass, entry, transports, woofer
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = registered(hass, entry)[UNIQUE][0]
    coordinator = entry.runtime_data.coordinator
    woofer.values["level"] = -8
    await coordinator.async_refresh_all()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "-8"
    woofer.values["connection"] = "off"
    await coordinator.async_refresh_all()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unavailable"
    assert entry.runtime_data.device.local_available
    woofer.client.async_write.assert_not_awaited()


@pytest.mark.parametrize("failure", ("off", "ocf"))
async def test_off_or_ocf_error_does_not_wake_device_or_fallback_to_relative_commands(
    hass, entry, transports, woofer, failure
):
    transports.rpc.remote_key = AsyncMock()
    transports.rpc.power_on = AsyncMock()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    ids = registered(hass, entry)
    entity_id = ids[UNIQUE][0]
    for unique, (button_id, _) in ids.items():
        if "_btn_woofer_" in unique:
            assert hass.states.get(button_id).state == "unavailable"
    if failure == "off":
        transports.rpc.status.return_value = {
            **transports.rpc.status.return_value,
            "power": "powerOff",
        }
    else:
        woofer.client.available = False
        woofer.client.async_read.side_effect = OcfError("auth")
    await entry.runtime_data.coordinator.async_refresh_all()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unavailable"
    assert registered(hass, entry) == ids
    transports.rpc.remote_key.assert_not_awaited()
    transports.rpc.power_on.assert_not_awaited()
