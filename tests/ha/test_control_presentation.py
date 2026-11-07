"""Device controls: no legacy buttons, grouped names and no automatic local EQ."""

from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsung_soundbar.const import DOMAIN

from . import test_ocf_state as ocf_fixtures
from .test_lifecycle import registered

ocf = ocf_fixtures.ocf
P = "x.com.samsung.networkaudio."


@pytest.fixture
def presentation(ocf):
    values = {
        "woofer": {P + "woofer": -6, P + "connection": "on"},
        "tone": {P + "bass": 0, P + "treble": 0},
        "advancedaudio": {
            P + key: 0
            for key in (
                "bassboost",
                "nightmode",
                "voiceamplifier",
                "virtual",
                "moderatebass",
            )
        },
        "spacefitSound": {P + "spacefitSound": 1},
        "eq": {
            P + "EQname": "NONE",
            P + "supportedList": ["NONE", "POP", "JAZZ", "CLASSIC", "CUSTOM"],
            P + "action": "setEQmode",
            P + "EQband": ["0"] * 7,
        },
        "currentEQMode": {P + "currentEQMode": "tone"},
    }
    ocf.async_read = AsyncMock(
        side_effect=lambda href: dict(values.get(href.rsplit("/", 1)[-1], {}))
    )
    return ocf


async def test_old_woofer_buttons_are_removed_from_owned_registry_and_not_recreated(
    hass, entry, transports, presentation
):
    registry = er.async_get(hass)
    old = []
    for suffix in ("plus", "minus"):
        old.append(
            registry.async_get_or_create(
                "button",
                DOMAIN,
                "existing-cloud-id_btn_woofer_" + suffix,
                config_entry=entry,
                suggested_object_id="custom_woofer_" + suffix,
            ).entity_id
        )
    other = registry.async_get_or_create(
        "button",
        DOMAIN,
        "unrelated_button",
        config_entry=entry,
        suggested_object_id="keep_me",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for entity_id in old:
        assert registry.async_get(entity_id) is None
        assert hass.states.get(entity_id) is None
    assert registry.async_get(other.entity_id) is not None
    assert not any("_btn_woofer_" in key for key in registered(hass, entry))
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert not any("_btn_woofer_" in key for key in registered(hass, entry))


async def test_woofer_cleanup_cannot_remove_another_entry_entity(
    hass, entry, transports
):
    foreign = MockConfigEntry(
        domain=DOMAIN, title="Other soundbar", unique_id="foreign"
    )
    foreign.add_to_hass(hass)
    registry = er.async_get(hass)
    item = registry.async_get_or_create(
        "button",
        DOMAIN,
        "existing-cloud-id_btn_woofer_plus",
        config_entry=foreign,
        suggested_object_id="foreign_woofer",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(item.entity_id).config_entry_id == foreign.entry_id


@pytest.mark.parametrize(
    ("language", "tone_prefix", "sound_prefix", "source_name"),
    (
        ("en", "Tone: ", "Sound: ", "Source: Input"),
        ("de", "Ton: ", "Klang: ", "Quelle: Eingang"),
    ),
)
async def test_grouped_native_controls_keep_ids_and_custom_names(
    hass,
    entry,
    transports,
    presentation,
    language,
    tone_prefix,
    sound_prefix,
    source_name,
):
    hass.config.language = language
    registry = er.async_get(hass)
    expected = {}
    for domain, suffix in (
        ("number", "num_bass_level"),
        ("number", "num_treble_level"),
        ("number", "sw_woofer_level"),
        ("switch", "sw_bassmode"),
        ("select", "sw_input_preset"),
        ("select", "sw_sound_mode_preset"),
    ):
        unique = "existing-cloud-id_" + suffix
        expected[unique] = registry.async_get_or_create(
            domain,
            DOMAIN,
            unique,
            config_entry=entry,
            suggested_object_id="saved_" + suffix,
        ).entity_id
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    ids = registered(hass, entry)
    for unique, entity_id in expected.items():
        assert ids[unique][0] == entity_id
    tone_ids = [
        ids["existing-cloud-id_" + suffix][0]
        for suffix in ("num_bass_level", "num_treble_level", "sw_woofer_level")
    ]
    for entity_id in tone_ids:
        assert registry.async_get(entity_id).original_name.startswith(tone_prefix)
    for unique, (entity_id, _) in ids.items():
        if entity_id.startswith("switch.") or unique.endswith("_sw_sound_mode_preset"):
            assert registry.async_get(entity_id).original_name.startswith(sound_prefix)
    assert (
        registry.async_get(ids["existing-cloud-id_sw_input_preset"][0]).original_name
        == source_name
    )
    assert hass.states.get(tone_ids[0]).state == "0"
    registry.async_update_entity(tone_ids[0], name="My bass")
    before = registered(hass, entry)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert registered(hass, entry) == before
    assert hass.states.get(tone_ids[0]).attributes["friendly_name"] == "My bass"


async def test_experimental_local_eq_is_not_a_control_and_old_entry_is_disabled(
    hass, entry, transports, presentation
):
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "select",
        DOMAIN,
        "existing-cloud-id_sw_eq_preset",
        config_entry=entry,
        suggested_object_id="old_eq",
    )
    registry.async_update_entity(old.entity_id, name="My EQ")
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "settings_eq": True}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    item = registry.async_get(old.entity_id)
    assert item.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert item.name == "My EQ"
    assert hass.states.get(old.entity_id) is None
    assert entry.runtime_data.device.ocf_equalizer.profile is not None
    assert not entry.runtime_data.device.can_select_equalizer_preset
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert (
        registry.async_get(old.entity_id).disabled_by
        is er.RegistryEntryDisabler.INTEGRATION
    )


async def test_eq_registry_sync_respects_user_disable_and_only_reenables_owned_entries(
    hass, entry
):
    from custom_components.samsung_soundbar.entity_migrations import (
        async_sync_eq_entity,
    )

    registry = er.async_get(hass)
    item = registry.async_get_or_create(
        "select", DOMAIN, "existing-cloud-id_sw_eq_preset", config_entry=entry
    )
    registry.async_update_entity(
        item.entity_id, disabled_by=er.RegistryEntryDisabler.USER
    )
    async_sync_eq_entity(hass, entry, True)
    assert (
        registry.async_get(item.entity_id).disabled_by is er.RegistryEntryDisabler.USER
    )
    registry.async_update_entity(
        item.entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION
    )
    async_sync_eq_entity(hass, entry, True)
    assert registry.async_get(item.entity_id).disabled_by is None


async def test_no_woofer_buttons_when_model_is_discovered_late(hass, entry, transports):
    original = dict(transports.rpc.status.return_value)
    transports.rpc.status.return_value = {**original, "identifier": None}
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    transports.rpc.status.return_value = original
    await entry.runtime_data.coordinator.async_refresh_all()
    await hass.async_block_till_done()
    assert not any("_btn_woofer_" in key for key in registered(hass, entry))
