"""Real HA level sliders, canonical grants, readback and isolated availability."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.samsung_soundbar.const import DOMAIN
from custom_components.samsung_soundbar.local_ocf import OcfError, OcfWriteResult
from custom_components.samsung_soundbar.ocf_level_profiles import (
    CHANNEL_HREF,
    PREFIX,
    Q800F_LEVELS,
    SYNC_HREF,
)

from . import test_ocf_state as ocf_fixtures
from .test_lifecycle import registered

ocf = ocf_fixtures.ocf
CHANNEL_PROPERTY = PREFIX + "channelVolume"
SYNC_PROPERTY = PREFIX + "audiosync"
CHANNELS = {
    "channel_level_center": ("Spk_Center", -2),
    "channel_level_side": ("Spk_Side", -1),
    "channel_level_frontTop": ("Spk_Front_Top", 0),
    "channel_level_rear": ("Spk_Rear", 1),
    "channel_level_rearTop": ("Spk_Rear_Top", 2),
}


def unique(field):
    return "existing-cloud-id_num_" + field


def level_ids(hass, entry):
    return {
        field: registered(hass, entry)[unique(field)][0]
        for field in Q800F_LEVELS
        if unique(field) in registered(hass, entry)
    }


@pytest.fixture
def levels(ocf):
    payloads = {
        CHANNEL_HREF: {
            PREFIX + "isSupported": 1,
            CHANNEL_PROPERTY: [
                {"name": wire, "value": value, "status": 1}
                for wire, value in CHANNELS.values()
            ]
            + [
                {"name": "Spk_Wide", "value": 0, "status": -1},
                {"name": "Spk_Rear_Side", "value": 0, "status": -1},
            ],
        },
        SYNC_HREF: {PREFIX + "isSupported": 1, SYNC_PROPERTY: 50},
        "/sec/networkaudio/tone": {PREFIX + "bass": 2, PREFIX + "treble": -2},
        "/sec/networkaudio/woofer": {
            PREFIX + "woofer": -6,
            PREFIX + "connection": "on",
        },
    }

    async def read(href):
        return deepcopy(payloads.get(href, {}))

    async def write(href, payload, *, setting_profile):
        assert setting_profile is Q800F_LEVELS[setting_profile.field]
        assert href == setting_profile.href
        assert set(payload) == {setting_profile.property}
        value = payload[setting_profile.property]
        if setting_profile.wire_name is not None:
            assert len(value) == 1
            assert set(value[0]) == {"name", "value"}
            assert value[0]["name"] == setting_profile.wire_name
            row = next(
                row
                for row in payloads[href][CHANNEL_PROPERTY]
                if row["name"] == setting_profile.wire_name
            )
            row["value"] = value[0]["value"]
        else:
            payloads[href][setting_profile.property] = value
        return OcfWriteResult(True, True, False, 68)

    ocf.async_read = AsyncMock(side_effect=read)
    ocf.async_write = AsyncMock(side_effect=write)
    return SimpleNamespace(client=ocf, payloads=payloads, read=read)


async def setup(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def refresh(hass, entry):
    await entry.runtime_data.coordinator.async_refresh_all()
    await hass.async_block_till_done()


async def set_value(hass, entity_id, value):
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
    )
    await hass.async_block_till_done()


async def test_native_slider_metadata_and_only_verified_controls(hass, entry, levels):
    await setup(hass, entry)
    ids = level_ids(hass, entry)
    assert set(ids) == set(Q800F_LEVELS)
    for field, entity_id in ids.items():
        state = hass.states.get(entity_id)
        expected = CHANNELS[field][1] if field in CHANNELS else 50
        assert float(state.state) == expected
        profile = Q800F_LEVELS[field]
        assert (
            state.attributes["min"],
            state.attributes["max"],
            state.attributes["step"],
            state.attributes["mode"],
        ) == (profile.minimum, profile.maximum, profile.step, "slider")
        assert state.attributes.get("unit_of_measurement") == profile.unit
        assert not state.attributes.get("assumed_state", False)
    assert hass.states.get(ids["audio_sync"]).attributes["step"] == 25
    registry = registered(hass, entry)
    assert not any(
        "_num_channel_level_wide" in key or "_num_channel_level_rearSide" in key
        for key in registry
    )
    assert "existing-cloud-id_sw_woofer_level" in registry
    assert "existing-cloud-id_num_bass_level" in registry
    assert "existing-cloud-id_num_treble_level" in registry


@pytest.mark.parametrize(
    "language,names",
    [
        (
            "en",
            [
                "Channels: Center",
                "Channels: Side",
                "Channels: Front Top",
                "Channels: Rear",
                "Channels: Rear Top",
                "Sound: Audio Sync",
            ],
        ),
        (
            "de",
            [
                "Kanäle: Center",
                "Kanäle: Seite",
                "Kanäle: Vorne oben",
                "Kanäle: Hinten",
                "Kanäle: Hinten oben",
                "Klang: Audio-Synchronisierung",
            ],
        ),
    ],
)
async def test_translated_names_preserve_registered_ids(
    hass, entry, levels, language, names
):
    hass.config.language = language
    registry = er.async_get(hass)
    saved = {
        field: registry.async_get_or_create(
            "number",
            DOMAIN,
            unique(field),
            config_entry=entry,
            suggested_object_id="saved_" + field,
        ).entity_id
        for field in Q800F_LEVELS
    }
    await setup(hass, entry)
    assert level_ids(hass, entry) == saved
    for (field, entity_id), name in zip(saved.items(), names, strict=True):
        assert registry.async_get(entity_id).translation_key == field
        assert (
            hass.states.get(entity_id).attributes["friendly_name"]
            == "Soundbar Q800F " + name
        )
    registry.async_update_entity(saved["audio_sync"], name="My delay")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert level_ids(hass, entry) == saved
    assert (
        hass.states.get(saved["audio_sync"]).attributes["friendly_name"] == "My delay"
    )


@pytest.mark.parametrize("field", Q800F_LEVELS)
async def test_native_service_writes_exactly_one_setting(hass, entry, levels, field):
    await setup(hass, entry)
    before = deepcopy(levels.payloads)
    profile = Q800F_LEVELS[field]
    await set_value(hass, level_ids(hass, entry)[field], profile.maximum)
    expected = deepcopy(before)
    if field in CHANNELS:
        next(
            row
            for row in expected[CHANNEL_HREF][CHANNEL_PROPERTY]
            if row["name"] == profile.wire_name
        )["value"] = profile.maximum
        payload = {
            profile.property: [{"name": profile.wire_name, "value": profile.maximum}]
        }
    else:
        expected[SYNC_HREF][SYNC_PROPERTY] = profile.maximum
        payload = {profile.property: profile.maximum}
    assert levels.payloads == expected
    levels.client.async_write.assert_awaited_once_with(
        profile.href, payload, setting_profile=profile
    )
    assert (
        float(hass.states.get(level_ids(hass, entry)[field]).state) == profile.maximum
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("channel_level_center", -7),
        ("channel_level_center", 7),
        ("channel_level_center", 0.5),
        ("audio_sync", -1),
        ("audio_sync", 1),
        ("audio_sync", 26),
        ("audio_sync", 301),
        ("audio_sync", 1.5),
    ],
)
async def test_invalid_values_never_write(hass, entry, levels, field, value):
    await setup(hass, entry)
    with pytest.raises(HomeAssistantError):
        await set_value(hass, level_ids(hass, entry)[field], value)
    levels.client.async_write.assert_not_awaited()


async def test_independent_late_discovery_and_polled_external_change(
    hass, entry, levels
):
    rows = levels.payloads[CHANNEL_HREF][CHANNEL_PROPERTY]
    rear = next(row for row in rows if row["name"] == "Spk_Rear")
    rows.remove(rear)
    sync = levels.payloads.pop(SYNC_HREF)
    await setup(hass, entry)
    assert set(level_ids(hass, entry)) == set(CHANNELS) - {"channel_level_rear"}
    rows.append(rear)
    await refresh(hass, entry)
    assert set(level_ids(hass, entry)) == set(CHANNELS)
    levels.payloads[SYNC_HREF] = sync
    await refresh(hass, entry)
    ids = level_ids(hass, entry)
    assert set(ids) == set(Q800F_LEVELS)
    rear["value"] = -4
    sync[SYNC_PROPERTY] = 125
    await refresh(hass, entry)
    assert float(hass.states.get(ids["channel_level_rear"]).state) == -4
    assert float(hass.states.get(ids["audio_sync"]).state) == 125
    assert level_ids(hass, entry) == ids
    levels.client.async_write.assert_not_awaited()


@pytest.mark.parametrize("failure", ["status", "support", "schema", "off", "ocf"])
async def test_failure_isolation_keeps_ids_and_never_wakes(
    hass, entry, transports, levels, failure
):
    transports.rpc.power_on = AsyncMock()
    await setup(hass, entry)
    ids = level_ids(hass, entry)
    all_ids = registered(hass, entry)
    if failure == "status":
        levels.payloads[CHANNEL_HREF][CHANNEL_PROPERTY][0]["status"] = 0
    elif failure == "support":
        levels.payloads[SYNC_HREF][PREFIX + "isSupported"] = 0
    elif failure == "schema":
        levels.payloads[CHANNEL_HREF][CHANNEL_PROPERTY].append(
            {"name": "Spk_Unverified", "value": 0, "status": 1}
        )
    elif failure == "off":
        transports.rpc.status.return_value = {
            **transports.rpc.status.return_value,
            "power": "powerOff",
        }
    else:
        levels.client.available = False
        levels.client.async_read.side_effect = OcfError("auth")
    await refresh(hass, entry)
    assert registered(hass, entry) == all_ids
    for field, entity_id in ids.items():
        unavailable = (
            failure in ("off", "ocf")
            or (failure == "status" and field == "channel_level_center")
            or (failure == "support" and field == "audio_sync")
        )
        assert (hass.states.get(entity_id).state == "unavailable") == unavailable
    assert entry.runtime_data.device.local_available
    levels.client.async_write.assert_not_awaited()
    transports.rpc.power_on.assert_not_awaited()


async def test_rpc_only_creates_no_level_entities(hass, entry, transports):
    await setup(hass, entry)
    assert level_ids(hass, entry) == {}


@pytest.mark.parametrize("status", (0, -1))
async def test_cold_unavailable_fields_are_not_inferred_from_model(
    hass, entry, levels, status
):
    levels.payloads[CHANNEL_HREF][CHANNEL_PROPERTY][0]["status"] = status
    levels.payloads[SYNC_HREF][PREFIX + "isSupported"] = 0
    await setup(hass, entry)
    ids = level_ids(hass, entry)
    assert set(ids) == set(CHANNELS) - {"channel_level_center"}
    levels.client.async_write.assert_not_awaited()
    levels.payloads[CHANNEL_HREF][CHANNEL_PROPERTY][0]["status"] = 1
    await refresh(hass, entry)
    assert set(level_ids(hass, entry)) == set(CHANNELS)
    levels.payloads[SYNC_HREF][PREFIX + "isSupported"] = 1
    await refresh(hass, entry)
    assert set(level_ids(hass, entry)) == set(Q800F_LEVELS)


async def test_channel_schema_change_does_not_create_undeclared_controls(
    hass, entry, levels
):
    await setup(hass, entry)
    before = registered(hass, entry)
    rows = levels.payloads[CHANNEL_HREF][CHANNEL_PROPERTY]
    for row in rows:
        if row["name"] in ("Spk_Wide", "Spk_Rear_Side"):
            row["status"] = 1
    rows.append({"name": "Spk_Unverified", "value": 2, "status": 1})
    await refresh(hass, entry)
    assert registered(hass, entry) == before
    assert len(level_ids(hass, entry)) == 6
    levels.client.async_write.assert_not_awaited()


async def test_missing_readback_is_never_published_as_zero(hass, entry, levels):
    from custom_components.samsung_soundbar.number import SoundbarLevelNumberEntity

    await setup(hass, entry)
    entity_id = level_ids(hass, entry)["audio_sync"]
    levels.payloads[SYNC_HREF].pop(SYNC_PROPERTY)
    await refresh(hass, entry)
    entity = SoundbarLevelNumberEntity(
        entry.runtime_data.device, Q800F_LEVELS["audio_sync"]
    )
    assert entity.native_value is None
    assert hass.states.get(entity_id).state == "unavailable"
    levels.client.async_write.assert_not_awaited()


async def test_forget_reimport_and_reload_keep_ids(hass, entry, levels):
    await setup(hass, entry)
    ids = level_ids(hass, entry)
    assert len(ids) == 6
    before = registered(hass, entry)
    data = dict(entry.data)
    data.pop("ocf_configured")
    hass.config_entries.async_update_entry(entry, data=data)
    await hass.async_block_till_done()
    assert registered(hass, entry) == before
    for entity_id in ids.values():
        assert hass.states.get(entity_id).state == "unavailable"
    hass.config_entries.async_update_entry(entry, data={**data, "ocf_configured": True})
    await hass.async_block_till_done()
    assert registered(hass, entry) == before
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert registered(hass, entry) == before
    assert float(hass.states.get(ids["audio_sync"]).state) == 50


async def test_rejected_write_does_not_publish_optimistic_value(hass, entry, levels):
    await setup(hass, entry)
    entity_id = level_ids(hass, entry)["audio_sync"]
    levels.client.async_write.side_effect = None
    levels.client.async_write.return_value = OcfWriteResult(False, False, True, None)
    with pytest.raises(HomeAssistantError):
        await set_value(hass, entity_id, 100)
    assert float(hass.states.get(entity_id).state) == 50
    levels.client.async_write.assert_awaited_once()


async def test_queue_rechecks_status_before_native_write(hass, entry, levels):
    await setup(hass, entry)
    coordinator = entry.runtime_data.coordinator
    entity_id = level_ids(hass, entry)["channel_level_center"]
    async with coordinator._write_lock:
        task = asyncio.create_task(set_value(hass, entity_id, 3))
        await asyncio.sleep(0)
        levels.payloads[CHANNEL_HREF][CHANNEL_PROPERTY][0]["status"] = 0
    with pytest.raises(HomeAssistantError):
        await task
    levels.client.async_write.assert_not_awaited()
    assert hass.states.get(entity_id).state == "unavailable"
