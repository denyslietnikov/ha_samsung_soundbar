"""Real HA tone numbers, per-field discovery and readback, no Cloud traffic."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.samsung_soundbar.const import DOMAIN
from custom_components.samsung_soundbar.local_ocf import OcfError, OcfWriteResult

from . import test_ocf_state as ocf_fixtures
from .test_lifecycle import registered

ocf = ocf_fixtures.ocf
HREF = "/sec/networkaudio/tone"
PREFIX = "x.com.samsung.networkaudio."


@pytest.fixture
def tone(ocf):
    values = {PREFIX + "bass": 0, PREFIX + "treble": 1}
    ocf.async_read = AsyncMock(
        side_effect=lambda href: dict(values) if href == HREF else {}
    )

    async def write(href, payload, *, tone_profile):
        assert href == HREF
        assert set(payload) == {tone_profile.property}
        assert type(payload[tone_profile.property]) is int
        values.update(payload)
        return OcfWriteResult(True, True, False, 68)

    ocf.async_write = AsyncMock(side_effect=write)
    return SimpleNamespace(client=ocf, values=values)


async def test_native_tone_sliders_read_write_and_reload(hass, entry, transports, tone):
    registry = er.async_get(hass)
    unique = "existing-cloud-id_num_bass_level"
    old = registry.async_get_or_create(
        "number", DOMAIN, unique, config_entry=entry, suggested_object_id="saved_bass"
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    ids = registered(hass, entry)
    bass = ids[unique][0]
    treble = ids["existing-cloud-id_num_treble_level"][0]
    assert bass == old.entity_id
    state = hass.states.get(bass)
    assert state.state == "0"
    assert state.attributes["friendly_name"] == "Soundbar Q800F Tone: Bass"
    assert (
        state.attributes["min"],
        state.attributes["max"],
        state.attributes["step"],
        state.attributes["mode"],
    ) == (-6, 6, 1, "slider")
    assert registry.async_get(bass).translation_key == "bass_level"
    await hass.services.async_call(
        "number", "set_value", {"entity_id": bass, "value": -3}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(bass).state == "-3"
    assert hass.states.get(treble).state == "1"
    assert entry.runtime_data.device.retrieve_data["tone"] == {
        "bass_level": -3,
        "treble_level": 1,
    }
    tone.client.async_write.assert_awaited_once()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert registered(hass, entry) == ids
    assert hass.states.get(bass).state == "-3"


async def test_late_per_field_discovery_and_external_change(
    hass, entry, transports, tone
):
    tone.values.pop(PREFIX + "treble")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    ids = registered(hass, entry)
    assert "existing-cloud-id_num_treble_level" not in ids
    tone.values[PREFIX + "treble"] = -1
    tone.values[PREFIX + "bass"] = 4
    await coordinator.async_refresh_all()
    await hass.async_block_till_done()
    ids = registered(hass, entry)
    assert hass.states.get(ids["existing-cloud-id_num_bass_level"][0]).state == "4"
    assert hass.states.get(ids["existing-cloud-id_num_treble_level"][0]).state == "-1"
    tone.client.async_write.assert_not_awaited()


@pytest.mark.parametrize("failure", ("power", "auth", "support"))
async def test_unavailable_does_not_remove_entities_wake_or_fallback(
    hass, entry, transports, tone, failure
):
    transports.rpc.power_on = AsyncMock()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    ids = registered(hass, entry)
    if failure == "power":
        transports.rpc.status.return_value = {
            **transports.rpc.status.return_value,
            "power": "powerOff",
        }
    elif failure == "auth":
        tone.client.available = False
        tone.client.async_read.side_effect = OcfError("auth")
    else:
        tone.values[PREFIX + "isSupported"] = 0
    await entry.runtime_data.coordinator.async_refresh_all()
    await hass.async_block_till_done()
    assert registered(hass, entry) == ids
    bass = ids["existing-cloud-id_num_bass_level"][0]
    assert hass.states.get(bass).state == "unavailable"
    # HA skips unavailable entities; the device guard also rejects direct actions.
    await hass.services.async_call(
        "number", "set_value", {"entity_id": bass, "value": 2}, blocking=True
    )
    with pytest.raises(HomeAssistantError):
        await entry.runtime_data.device.set_tone_level("bass_level", 2)
    tone.client.async_write.assert_not_awaited()
    transports.rpc.power_on.assert_not_awaited()


async def test_invalid_fraction_and_failed_readback_have_translated_errors(
    hass, entry, transports, tone
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    bass = registered(hass, entry)["existing-cloud-id_num_bass_level"][0]
    with pytest.raises(ServiceValidationError) as error:
        await hass.services.async_call(
            "number", "set_value", {"entity_id": bass, "value": 0.5}, blocking=True
        )
    assert error.value.translation_key == "ocf_tone_invalid"
    tone.client.async_write.assert_not_awaited()
    tone.client.async_write.side_effect = None
    tone.client.async_write.return_value = OcfWriteResult(False, False, True, None)
    with pytest.raises(HomeAssistantError) as error:
        await hass.services.async_call(
            "number", "set_value", {"entity_id": bass, "value": 2}, blocking=True
        )
    assert error.value.translation_key == "ocf_tone_unconfirmed"
    await hass.async_block_till_done()
    assert hass.states.get(bass).state == "0"
    tone.client.async_write.assert_awaited_once()


async def test_no_credential_does_not_create_tone_controls(hass, entry, transports):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert not any("_num_" in unique for unique in registered(hass, entry))


async def test_forget_reimport_preserves_tone_entity_ids(hass, entry, transports, tone):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    before = registered(hass, entry)
    data = dict(entry.data)
    data.pop("ocf_configured")
    hass.config_entries.async_update_entry(entry, data=data)
    await hass.async_block_till_done()
    assert registered(hass, entry) == before
    assert (
        hass.states.get(before["existing-cloud-id_num_bass_level"][0]).state
        == "unavailable"
    )
    hass.config_entries.async_update_entry(entry, data={**data, "ocf_configured": True})
    await hass.async_block_till_done()
    assert registered(hass, entry) == before
