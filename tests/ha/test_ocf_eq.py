"""Experimental EQ diagnostics stay guarded, without an automatic local select."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.samsung_soundbar.const import DOMAIN
from custom_components.samsung_soundbar.local_ocf import OcfError, OcfWriteResult
from custom_components.samsung_soundbar.local_rpc import LocalRpcError
from custom_components.samsung_soundbar.ocf_profiles import Q800F_EQ

from . import test_ocf_state as ocf_fixtures
from .test_lifecycle import registered

ocf = ocf_fixtures.ocf
P = "x.com.samsung.networkaudio."
EQ = "/sec/networkaudio/eq"
MODE = "/sec/networkaudio/currentEQMode"
ADVANCED = "/sec/networkaudio/advancedaudio"
UNIQUE = "existing-cloud-id_sw_eq_preset"
OPTIONS = ["NONE", "POP", "JAZZ", "CLASSIC", "CUSTOM"]


@pytest.fixture
def equalizer(ocf, transports):
    transports.rpc.sound_mode.return_value = "STANDARD"
    transports.rpc.status.return_value = {
        **transports.rpc.status.return_value,
        "sound_mode": "STANDARD",
    }
    payloads = {
        EQ: {
            P + "EQname": "NONE",
            P + "supportedList": list(OPTIONS),
            P + "action": "setEQmode",
            P + "EQband": ["0"] * 7,
        },
        MODE: {P + "currentEQMode": "usereq"},
        ADVANCED: {P + "bassboost": 0},
    }
    curves = {
        "NONE": ["0"] * 7,
        "CUSTOM": ["0"] * 7,
        "POP": ["0", "-3", "3", "1", "-5", "0", "0"],
        "JAZZ": ["0", "0", "2", "4", "0", "0", "0"],
        "CLASSIC": ["2", "0", "0", "5", "0", "1", "0"],
    }
    ocf.async_read = AsyncMock(side_effect=lambda href: dict(payloads.get(href, {})))

    async def write(href, payload, *, eq_profile):
        assert href == EQ and eq_profile is Q800F_EQ
        assert payload == {
            P + "EQname": payload[P + "EQname"],
            P + "action": "setEQmode",
        }
        payloads[EQ].update(payload)
        payloads[EQ][P + "EQband"] = list(curves[payload[P + "EQname"]])
        return OcfWriteResult(True, True, False, 68)

    ocf.async_write = AsyncMock(side_effect=write)
    return SimpleNamespace(client=ocf, payloads=payloads, curves=curves)


async def test_old_eq_id_is_disabled_while_diagnostic_route_stays_guarded(
    hass, entry, transports, equalizer
):
    old = er.async_get(hass).async_get_or_create(
        "select", DOMAIN, UNIQUE, config_entry=entry, suggested_object_id="my_eq"
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = registered(hass, entry)[UNIQUE][0]
    assert entity_id == old.entity_id
    assert (
        er.async_get(hass).async_get(entity_id).disabled_by
        is er.RegistryEntryDisabler.INTEGRATION
    )
    assert hass.states.get(entity_id) is None
    assert entry.runtime_data.device.supported_equalizer_presets == OPTIONS
    await entry.runtime_data.device.set_equalizer_preset("POP")
    await hass.async_block_till_done()
    assert entry.runtime_data.device.active_equalizer_preset == "POP"
    assert entry.runtime_data.device.equalizer_bands == [0, -3, 3, 1, -5, 0, 0]
    equalizer.client.async_write.assert_awaited_once_with(
        EQ, {P + "EQname": "POP", P + "action": "setEQmode"}, eq_profile=Q800F_EQ
    )
    transports.auth.assert_not_awaited()
    transports.api.execute_device_command.assert_not_called()
    before = registered(hass, entry)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert registered(hass, entry) == before


async def test_late_eq_discovery_and_external_preset_change(
    hass, entry, transports, equalizer
):
    payload = equalizer.payloads.pop(EQ)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert UNIQUE not in registered(hass, entry)
    payload[P + "EQname"] = "JAZZ"
    payload[P + "EQband"] = equalizer.curves["JAZZ"]
    equalizer.payloads[EQ] = payload
    await entry.runtime_data.coordinator.async_refresh_all()
    await hass.async_block_till_done()
    assert UNIQUE not in registered(hass, entry)
    assert entry.runtime_data.device.active_equalizer_preset == "JAZZ"
    equalizer.client.async_write.assert_not_awaited()


@pytest.mark.parametrize("failure", ("mode", "bassboost", "power", "auth", "support"))
async def test_unavailable_context_never_auto_changes_or_falls_back(
    hass, entry, transports, equalizer, failure
):
    transports.rpc.power_on = AsyncMock()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    before = registered(hass, entry)
    if failure == "mode":
        equalizer.payloads[MODE][P + "currentEQMode"] = "tone"
    elif failure == "bassboost":
        equalizer.payloads[ADVANCED][P + "bassboost"] = 1
    elif failure == "power":
        transports.rpc.status.return_value = {
            **transports.rpc.status.return_value,
            "power": "powerOff",
        }
    elif failure == "support":
        equalizer.payloads[EQ][P + "isSupported"] = 0
    else:
        equalizer.client.available = False
        equalizer.client.async_read.side_effect = OcfError("auth")
    await entry.runtime_data.coordinator.async_refresh_all()
    await hass.async_block_till_done()
    assert registered(hass, entry) == before
    assert not entry.runtime_data.device.ocf_equalizer.available
    with pytest.raises(HomeAssistantError):
        await entry.runtime_data.device.set_equalizer_preset("POP")
    equalizer.client.async_write.assert_not_awaited()
    transports.rpc.power_on.assert_not_awaited()
    transports.rpc.set_sound_mode.assert_not_awaited()
    transports.api.execute_device_command.assert_not_called()


async def test_failed_write_does_not_publish_optimistic_eq(
    hass, entry, transports, equalizer
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    equalizer.client.async_write.side_effect = None
    equalizer.client.async_write.return_value = OcfWriteResult(False, False, True, None)
    with pytest.raises(HomeAssistantError) as error:
        await entry.runtime_data.device.set_equalizer_preset("POP")
    assert error.value.translation_key == "ocf_eq_unconfirmed"
    assert entry.runtime_data.device.active_equalizer_preset == "NONE"
    equalizer.client.async_write.assert_awaited_once()


async def test_preflight_rechecks_options_and_context_under_queue(
    hass, entry, transports, equalizer
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    equalizer.payloads[EQ][P + "supportedList"] = ["NONE", "CUSTOM"]
    with pytest.raises(ServiceValidationError):
        await entry.runtime_data.device.set_equalizer_preset("POP")
    equalizer.client.async_write.assert_not_awaited()


async def test_forget_reimport_preserves_eq_registry_id(
    hass, entry, transports, equalizer
):
    old = er.async_get(hass).async_get_or_create(
        "select", DOMAIN, UNIQUE, config_entry=entry, suggested_object_id="old_eq"
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    before = registered(hass, entry)
    data = dict(entry.data)
    data.pop("ocf_configured")
    hass.config_entries.async_update_entry(entry, data=data)
    await hass.async_block_till_done()
    assert registered(hass, entry) == before
    assert (
        er.async_get(hass).async_get(old.entity_id).disabled_by
        is er.RegistryEntryDisabler.INTEGRATION
    )
    hass.config_entries.async_update_entry(entry, data={**data, "ocf_configured": True})
    await hass.async_block_till_done()
    assert registered(hass, entry) == before


@pytest.mark.parametrize("failure", ("power", "mode", "missing", "error"))
async def test_service_reads_actual_rpc_context_even_with_fresh_old_cache(
    hass, entry, transports, equalizer, failure
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    if failure == "power":
        transports.rpc.power_state.return_value = "powerOff"
    elif failure == "mode":
        transports.rpc.sound_mode.return_value = "GAME"
    elif failure == "missing":
        transports.rpc.sound_mode.return_value = None
    else:
        transports.rpc.sound_mode.side_effect = LocalRpcError("offline")
    with pytest.raises(HomeAssistantError):
        await entry.runtime_data.device.set_equalizer_preset("POP")
    equalizer.client.async_write.assert_not_awaited()
