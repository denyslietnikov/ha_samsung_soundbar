"""Presentation contracts using real HA entities with local device doubles."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.media_player.const import MediaPlayerEntityFeature
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsung_soundbar import number, select, switch
from custom_components.samsung_soundbar.const import CONF_ENTRY_DEVICE_ID, DOMAIN
from custom_components.samsung_soundbar.device_info import build_device_info
from custom_components.samsung_soundbar.entity_migrations import async_sync_eq_entity
from custom_components.samsung_soundbar.entity_updates import (
    register_device_update_listener,
)
from custom_components.samsung_soundbar.media_player import SoundbarMediaPlayer
from custom_components.samsung_soundbar.ocf_profiles import (
    Q800F_AUDIO_SWITCHES,
    Q800F_TONE,
    Q800F_WOOFER,
)


@pytest.fixture
def local_device():
    listeners = []

    def subscribe(listener):
        listeners.append(listener)
        return lambda: listeners.remove(listener)

    device = SimpleNamespace(
        device_id="saved-id",
        device_name="Living Room",
        manufacturer=None,
        model=None,
        firmware_version=None,
        suggested_area=None,
        coordinator=None,
        media_title=None,
        media_artist=None,
        media_album_name=None,
        local_rpc_enabled=True,
        ocf_enabled=True,
        control_mode="hybrid_local_ocf",
        available=True,
        local_available=True,
        local_last_error=None,
        local_codec=None,
        local_identifier=None,
        can_turn_on_off=True,
        can_control_volume=True,
        can_mute_volume=True,
        can_control_playback=True,
        can_select_source=False,
        can_select_sound_mode=False,
        can_select_equalizer_preset=True,
        can_control_woofer_level=True,
        ocf_woofer_profile=None,
        ocf_tone_profile=lambda field: None,
        ocf_level_profile=lambda field: None,
        ocf_audio_profile=lambda field: None,
        can_control_audio_switch=lambda field: True,
        add_update_listener=subscribe,
        listeners=listeners,
        woofer_control_available=False,
        audio_switch_available=lambda field: False,
        tone_control_available=lambda field: False,
    )
    for profile in Q800F_AUDIO_SWITCHES.values():
        setattr(device, profile.getter, None)
        setattr(device, profile.setter, AsyncMock())
    return device


def make_entry(hass, device, suffix="saved"):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=suffix,
        data={CONF_ENTRY_DEVICE_ID: device.device_id},
    )
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(device=device)
    return entry


async def test_local_metadata_omits_unknowns_and_preserves_registry_values(
    hass, local_device
):
    registry = dr.async_get(hass)
    entry = make_entry(hass, local_device)
    existing = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, local_device.device_id)},
        manufacturer="Samsung",
        model="HW-Q800F",
        sw_version="1103.1",
        configuration_url="https://retired.example",
    )
    metadata = build_device_info(local_device)
    assert metadata == {
        "identifiers": {(DOMAIN, "saved-id")},
        "name": "Living Room",
        "configuration_url": None,
    }
    result = registry.async_get_or_create(config_entry_id=entry.entry_id, **metadata)
    assert result.id == existing.id
    assert (result.manufacturer, result.model, result.sw_version) == (
        "Samsung",
        "HW-Q800F",
        "1103.1",
    )
    assert result.configuration_url is None
    local_device.model = " HW-Q800F|extra "
    local_device.manufacturer = " Samsung "
    local_device.firmware_version = "1103.1"
    assert build_device_info(local_device)["model"] == "HW-Q800F"


async def test_media_player_never_advertises_or_implements_playback(local_device):
    entity = SoundbarMediaPlayer(local_device)
    for feature in ("PLAY", "PAUSE", "STOP", "NEXT_TRACK", "PREVIOUS_TRACK"):
        assert not entity.supported_features & getattr(
            MediaPlayerEntityFeature, feature
        )
    for method in ("play", "pause", "stop", "next_track", "previous_track"):
        assert f"async_media_{method}" not in type(entity).__dict__
    assert entity.media_title is None
    assert entity.media_artist is None
    assert entity.media_duration is None
    assert entity.media_image_url is None
    assert entity.media_image_hash is None


async def test_advanced_entities_require_local_profiles_and_discover_late(
    hass, local_device
):
    entry = make_entry(hass, local_device)
    added = []
    await number.async_setup_entry(hass, entry, added.extend)
    await switch.async_setup_entry(hass, entry, added.extend)
    assert not added
    local_device.ocf_woofer_profile = Q800F_WOOFER
    local_device.ocf_tone_profile = Q800F_TONE.get
    local_device.ocf_audio_profile = Q800F_AUDIO_SWITCHES.get
    for listener in tuple(local_device.listeners):
        listener()
    assert len(added) == 9
    assert all(not entity.available for entity in added)
    for listener in tuple(local_device.listeners):
        listener()
    assert len(added) == 9


async def test_eq_cleanup_only_removes_exact_owned_select(hass, local_device):
    entry = make_entry(hass, local_device)
    foreign = make_entry(hass, SimpleNamespace(device_id="foreign"), "foreign")
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "select", DOMAIN, "saved-id_sw_eq_preset", config_entry=entry
    )
    keep = registry.async_get_or_create(
        "select", DOMAIN, "saved-id_sw_input_preset", config_entry=entry
    )
    async_sync_eq_entity(hass, entry)
    assert registry.async_get(old.entity_id) is None
    assert registry.async_get(keep.entity_id) is not None
    other = registry.async_get_or_create(
        "select", DOMAIN, "saved-id_sw_eq_preset", config_entry=foreign
    )
    async_sync_eq_entity(hass, entry)
    assert registry.async_get(other.entity_id).config_entry_id == foreign.entry_id


async def test_select_discovery_retains_rpc_controls_and_never_eq(hass, local_device):
    entry = make_entry(hass, local_device)
    added = []
    await select.async_setup_entry(hass, entry, added.extend)
    assert not added
    local_device.can_select_source = local_device.can_select_sound_mode = True
    for listener in tuple(local_device.listeners):
        listener()
    assert {type(entity).__name__ for entity in added} == {
        "InputSelectEntity",
        "SoundModeSelectEntity",
    }
    assert {entity.unique_id for entity in added} == {
        "saved-id_sw_input_preset",
        "saved-id_sw_sound_mode_preset",
    }


async def test_rpc_availability_does_not_depend_on_ocf(hass, local_device):
    entry = make_entry(hass, local_device)
    entity = SoundbarMediaPlayer(local_device)
    register_device_update_listener(entry, local_device, [entity])
    assert entity.available
    local_device.local_available = False
    for listener in tuple(local_device.listeners):
        listener()
    assert not entity.available


async def test_switch_state_is_readback_not_optimistic(local_device):
    profile = Q800F_AUDIO_SWITCHES["night_mode"]
    entity = switch.SoundbarSwitchAdvancedAudio(
        local_device,
        profile.suffix,
        lambda: local_device.night_mode,
        local_device.set_night_mode,
        local_device.set_night_mode,
        profile.icon,
        field=profile.field,
    )
    entity.async_write_ha_state = lambda: None
    assert entity.is_on is None
    await entity.async_turn_on()
    local_device.set_night_mode.assert_awaited_once_with(True)
    assert entity.is_on is None
    local_device.night_mode = True
    assert entity.is_on is True


@pytest.mark.parametrize(
    ("action", "backend", "value"),
    (
        ("async_select_sound_mode", "select_sound_mode", "Surround"),
        ("async_set_woofer_level", "set_woofer", -7),
        ("async_set_night_mode", "set_night_mode", True),
        ("async_set_bass_mode", "set_bass_mode", True),
        ("async_set_voice_mode", "set_voice_amplifier", True),
        ("async_set_space_fit_sound", "set_space_fit_sound", True),
    ),
)
async def test_kept_actions_use_guarded_device_methods(
    local_device, action, backend, value
):
    setattr(local_device, backend, AsyncMock())
    entity = SoundbarMediaPlayer(local_device)
    entity.async_write_ha_state = lambda: None
    await getattr(entity, action)(value)
    getattr(local_device, backend).assert_awaited_once_with(value)


async def test_local_configuration_repair_opens_existing_reconfigure(
    hass, local_device
):
    from homeassistant.components import repairs
    from homeassistant.components.repairs import FlowType
    from homeassistant.setup import async_setup_component

    from custom_components.samsung_soundbar.ocf_repairs import (
        update_local_configuration_issue,
    )

    assert await async_setup_component(hass, DOMAIN, {})
    entry = make_entry(hass, local_device)
    update_local_configuration_issue(hass, entry, True)
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs.repairs_flow_manager(hass)
    result = await manager.async_init(
        DOMAIN, data={"issue_id": f"local_configuration_{entry.entry_id}"}
    )
    assert result["step_id"] == "choose"
    result = await manager.async_configure(result["flow_id"], {})
    assert result["reason"] == "reconfigure"
    assert result["next_flow"][0] is FlowType.CONFIG_FLOW
    pending = hass.config_entries.flow.async_get(result["next_flow"][1])
    assert pending["context"]["entry_id"] == entry.entry_id
    assert pending["context"]["source"] == "reconfigure"
