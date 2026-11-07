"""Actual HA switches: stable IDs, readback, per-feature availability and no Cloud."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.samsung_soundbar.const import DOMAIN
from custom_components.samsung_soundbar.local_ocf import OcfError, OcfWriteResult
from custom_components.samsung_soundbar.ocf_profiles import Q800F_AUDIO_SWITCHES

from . import test_ocf_state as ocf_fixtures
from .test_lifecycle import registered

ocf = ocf_fixtures.ocf


@pytest.fixture
def audio(ocf):
    payloads = {}
    for profile in Q800F_AUDIO_SWITCHES.values():
        payloads.setdefault(profile.href, {})[profile.property] = (
            1 if profile.field == "spacefit_sound" else 0
        )
    ocf.async_read = AsyncMock(side_effect=lambda href: dict(payloads.get(href, {})))

    async def write(href, payload, *, switch_profile):
        assert switch_profile is Q800F_AUDIO_SWITCHES[switch_profile.field]
        assert set(payload) == {switch_profile.property}
        assert type(payload[switch_profile.property]) is int
        payloads[href].update(payload)
        return OcfWriteResult(True, True, False, 68)

    ocf.async_write = AsyncMock(side_effect=write)
    return SimpleNamespace(client=ocf, payloads=payloads)


async def test_existing_switch_ids_and_native_toggles_use_local_readback(
    hass, entry, transports, audio
):
    saved = {}
    for field in ("night_mode", "bass_mode", "voice_amplifier", "virtual_sound"):
        profile = Q800F_AUDIO_SWITCHES[field]
        unique = "existing-cloud-id_sw_" + profile.suffix
        saved[unique] = (
            er.async_get(hass)
            .async_get_or_create(
                "switch",
                DOMAIN,
                unique,
                suggested_object_id="legacy_" + profile.suffix,
                config_entry=entry,
            )
            .entity_id
        )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    ids = registered(hass, entry)
    assert sum(entity.startswith("switch.") for entity, _ in ids.values()) == 6
    for unique, entity_id in saved.items():
        assert ids[unique][0] == entity_id
    for profile in Q800F_AUDIO_SWITCHES.values():
        entity_id = ids["existing-cloud-id_sw_" + profile.suffix][0]
        old = bool(audio.payloads[profile.href][profile.property])
        assert hass.states.get(entity_id).state == ("on" if old else "off")
        assert not hass.states.get(entity_id).attributes.get("assumed_state", False)
        await hass.services.async_call(
            "switch",
            "turn_off" if old else "turn_on",
            {"entity_id": entity_id},
            blocking=True,
        )
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).state == ("off" if old else "on")
        audio.client.async_write.assert_awaited_with(
            profile.href, {profile.property: int(not old)}, switch_profile=profile
        )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert registered(hass, entry) == ids


@pytest.mark.parametrize(
    ("language", "names"),
    (
        (
            "en",
            {
                "moderate_bass": "Sound: Moderate Bass",
                "spacefit_sound": "Sound: SpaceFit Sound",
            },
        ),
        (
            "de",
            {
                "moderate_bass": "Klang: Moderater Bass",
                "spacefit_sound": "Klang: SpaceFit Sound",
            },
        ),
    ),
)
async def test_new_switch_names_are_translated_without_changing_ids(
    hass, entry, transports, audio, language, names
):
    hass.config.language = language
    registry = er.async_get(hass)
    saved = {}
    for field in names:
        profile = Q800F_AUDIO_SWITCHES[field]
        unique = "existing-cloud-id_sw_" + profile.suffix
        saved[field] = registry.async_get_or_create(
            "switch",
            DOMAIN,
            unique,
            suggested_object_id="saved_" + field,
            config_entry=entry,
        ).entity_id
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for field, name in names.items():
        entity_id = saved[field]
        item = registry.async_get(entity_id)
        assert item.translation_key == field
        assert item.has_entity_name
        assert hass.states.get(entity_id).attributes["friendly_name"] == (
            "Soundbar Q800F " + name
        )
    before = registered(hass, entry)
    registry.async_update_entity(saved["moderate_bass"], name="My bass setting")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert registered(hass, entry) == before
    assert hass.states.get(saved["moderate_bass"]).attributes["friendly_name"] == (
        "My bass setting"
    )


async def test_external_app_change_and_spacefit_support_are_field_scoped(
    hass, entry, transports, audio
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    ids = registered(hass, entry)
    profile = Q800F_AUDIO_SWITCHES["moderate_bass"]
    audio.payloads[profile.href][profile.property] = 1
    space = Q800F_AUDIO_SWITCHES["spacefit_sound"]
    audio.payloads[space.href] = {"x.com.samsung.networkaudio.isSupported": 0}
    await entry.runtime_data.coordinator.async_refresh_all()
    await hass.async_block_till_done()
    assert hass.states.get(ids["existing-cloud-id_sw_moderate_bass"][0]).state == "on"
    assert (
        hass.states.get(ids["existing-cloud-id_sw_space_fit_sound"][0]).state
        == "unavailable"
    )
    assert hass.states.get(ids["existing-cloud-id_sw_bassmode"][0]).state == "off"
    assert registered(hass, entry) == ids
    assert entry.runtime_data.device.local_available


@pytest.mark.parametrize("failure", ("power", "ocf"))
async def test_failure_never_wakes_or_falls_back_and_keeps_entities(
    hass, entry, transports, audio, failure
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
    else:
        audio.client.available = False
        audio.client.async_read.side_effect = OcfError("auth")
    await entry.runtime_data.coordinator.async_refresh_all()
    await hass.async_block_till_done()
    for entity, _ in ids.values():
        if entity.startswith("switch."):
            assert hass.states.get(entity).state == "unavailable"
    assert registered(hass, entry) == ids
    audio.client.async_write.assert_not_awaited()
    transports.rpc.power_on.assert_not_awaited()


async def test_rejected_write_does_not_turn_on_switch_optimistically(
    hass, entry, transports, audio
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = registered(hass, entry)["existing-cloud-id_sw_moderate_bass"][0]
    audio.client.async_write = AsyncMock(
        return_value=OcfWriteResult(True, False, False, 68)
    )
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": entity_id}, blocking=True
        )
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "off"
    audio.client.async_write.assert_awaited_once()




async def test_forget_and_reimport_credential_preserves_switch_ids(
    hass, entry, transports, audio
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    before = registered(hass, entry)
    data = dict(entry.data)
    data.pop("ocf_configured")
    hass.config_entries.async_update_entry(entry, data=data)
    await hass.async_block_till_done()
    assert registered(hass, entry) == before
    for entity, _ in before.values():
        if entity.startswith("switch."):
            assert hass.states.get(entity).state == "unavailable"
    hass.config_entries.async_update_entry(entry, data={**data, "ocf_configured": True})
    await hass.async_block_till_done()
    assert registered(hass, entry) == before


async def test_existing_spacefit_action_uses_verified_local_route(
    hass, entry, transports, audio
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    player_id = next(
        entity
        for entity, _ in registered(hass, entry).values()
        if entity.startswith("media_player.")
    )
    await hass.services.async_call(
        DOMAIN,
        "set_space_fit_sound",
        {"entity_id": player_id, "enabled": False},
        blocking=True,
    )
    profile = Q800F_AUDIO_SWITCHES["spacefit_sound"]
    audio.client.async_write.assert_awaited_once_with(
        profile.href, {profile.property: 0}, switch_profile=profile
    )
