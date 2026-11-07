"""Read-only channel schema and AudioSync support projection contracts."""

from copy import deepcopy
from unittest import TestCase

from custom_components.samsung_soundbar.ocf_state import (
    BASE,
    OCF_FIELDS,
    PREFIX,
    RESOURCE_FIELDS,
    project_ocf,
)
from custom_components.samsung_soundbar.state import SoundbarState

CHANNEL_HREF = BASE + "channelVolume"
SYNC_HREF = BASE + "audiosync"
RAW_NAMES = {
    "center": "Spk_Center",
    "side": "Spk_Side",
    "wide": "Spk_Wide",
    "frontTop": "Spk_Front_Top",
    "rear": "Spk_Rear",
    "rearTop": "Spk_Rear_Top",
    "rearSide": "Spk_Rear_Side",
}


def channel_payload():
    return {
        PREFIX + "isSupported": 1,
        PREFIX + "channelVolume": [
            {
                "name": raw,
                "value": {"rear": 4, "rearTop": 6}.get(name, 0),
                "status": -1 if name in {"wide", "rearSide"} else 1,
            }
            for name, raw in RAW_NAMES.items()
        ],
    }


class TestOcfChannelSchema(TestCase):
    def project(self, payload):
        return project_ocf(CHANNEL_HREF, payload)

    def test_authenticated_q800f_names_preserve_stable_fields_and_status(self):
        result = self.project(channel_payload())
        expected = {"channel_schema_valid": True}
        for name in RAW_NAMES:
            unsupported = name in {"wide", "rearSide"}
            expected["channel_status_" + name] = -1 if unsupported else 1
            if not unsupported:
                expected["channel_level_" + name] = {"rear": 4, "rearTop": 6}.get(
                    name, 0
                )
        self.assertEqual(result.values, expected)
        self.assertIs(result.supported, True)
        self.assertEqual(
            set(result.invalidated),
            {
                "channel_level_wide",
                "channel_level_rearSide",
                "channel_level_front",
                "channel_status_front",
                "channel_level_top",
                "channel_status_top",
            },
        )

    def test_each_canonical_channel_maps_independently(self):
        for canonical, raw in RAW_NAMES.items():
            with self.subTest(canonical=canonical):
                payload = channel_payload()
                payload[PREFIX + "channelVolume"] = [
                    {"name": raw, "value": -3, "status": 1}
                ]
                result = self.project(payload)
                self.assertEqual(
                    result.values,
                    {
                        "channel_schema_valid": True,
                        "channel_status_" + canonical: 1,
                        "channel_level_" + canonical: -3,
                    },
                )
                self.assertEqual(
                    set(result.invalidated),
                    RESOURCE_FIELDS[CHANNEL_HREF] - result.values.keys(),
                )

    def test_unsupported_row_invalidates_level_without_fake_zero_or_off(self):
        for status in (0, -1):
            with self.subTest(status=status):
                payload = channel_payload()
                payload[PREFIX + "channelVolume"][0].update(status=status, value=6)
                result = self.project(payload)
                self.assertIs(result.values["channel_schema_valid"], True)
                self.assertEqual(result.values["channel_status_center"], status)
                self.assertNotIn("channel_level_center", result.values)
                self.assertIn("channel_level_center", result.invalidated)

    def test_authoritative_list_invalidates_omitted_channel_level_and_status(self):
        payload = channel_payload()
        payload[PREFIX + "channelVolume"] = payload[PREFIX + "channelVolume"][:1]
        result = self.project(payload)
        self.assertIs(result.values["channel_schema_valid"], True)
        self.assertNotIn("channel_level_rear", result.values)
        self.assertNotIn("channel_status_rear", result.values)
        self.assertIn("channel_level_rear", result.invalidated)
        self.assertIn("channel_status_rear", result.invalidated)

    def test_schema_requires_explicit_integer_support_one(self):
        for support in (None, True, False, "1", 1.0, 0, -1, 2):
            with self.subTest(support=support):
                payload = channel_payload()
                payload[PREFIX + "isSupported"] = support
                self.assertIs(
                    self.project(payload).values["channel_schema_valid"], False
                )
        payload = channel_payload()
        del payload[PREFIX + "isSupported"]
        self.assertIs(self.project(payload).values["channel_schema_valid"], False)
        payload["isSupported"] = 1
        self.assertIs(self.project(payload).values["channel_schema_valid"], True)

    def test_explicit_unsupported_resource_does_not_export_levels(self):
        payload = channel_payload()
        payload[PREFIX + "isSupported"] = 0
        result = self.project(payload)
        self.assertIs(result.supported, False)
        self.assertEqual(result.values, {"channel_schema_valid": False})

    def test_minimal_write_payload_cannot_fabricate_read_schema_or_support(self):
        result = self.project(
            {PREFIX + "channelVolume": [{"name": "Spk_Center", "value": 1}]}
        )
        self.assertIsNone(result.supported)
        self.assertEqual(result.values, {"channel_schema_valid": False})
        self.assertIn("channel_level_center", result.invalidated)

    def test_malformed_or_incomplete_list_never_grants_schema(self):
        for rows in (
            None,
            {},
            (),
            "bad",
            [],
            [None],
            [{}],
            [{"name": "Spk_Center", "status": 1}],
            [{"name": "Spk_Center", "value": 0}],
            [{"value": 0, "status": 1}],
            [{"name": "", "value": 0, "status": 1}],
            [{"name": [], "value": 0, "status": 1}],
            [{"name": f"Future{i}", "value": 0, "status": 1} for i in range(33)],
        ):
            with self.subTest(rows=rows):
                payload = channel_payload()
                payload[PREFIX + "channelVolume"] = rows
                self.assertIs(
                    self.project(payload).values["channel_schema_valid"], False
                )
        payload = channel_payload()
        del payload[PREFIX + "channelVolume"]
        self.assertIs(self.project(payload).values["channel_schema_valid"], False)

    def test_invalid_row_types_fail_closed_not_bool_as_integer(self):
        for key, invalid_values in {
            "value": (True, False, "0", 0.0, None, [], {}),
            "status": (True, False, "on", "off", "1", 1.0, None, 2, -2, [], {}),
        }.items():
            for invalid in invalid_values:
                with self.subTest(key=key, invalid=invalid):
                    payload = channel_payload()
                    payload[PREFIX + "channelVolume"][0][key] = invalid
                    result = self.project(payload)
                    self.assertIs(result.values["channel_schema_valid"], False)
                    self.assertNotIn("channel_level_center", result.values)
                    self.assertIn("channel_level_center", result.invalidated)
                    self.assertNotIn("channel_status_center", result.values)

    def test_duplicate_raw_name_or_alias_never_grants_schema_or_wins_by_order(self):
        for alias in ("Spk_Center", "center"):
            for reverse in (False, True):
                with self.subTest(alias=alias, reverse=reverse):
                    payload = channel_payload()
                    rows = payload[PREFIX + "channelVolume"]
                    rows.append({"name": alias, "value": 5, "status": 1})
                    if reverse:
                        rows.reverse()
                    result = self.project(payload)
                    self.assertIs(result.values["channel_schema_valid"], False)
                    self.assertNotIn("channel_level_center", result.values)
                    self.assertNotIn("channel_status_center", result.values)
                    self.assertIn("channel_level_center", result.invalidated)

    def test_legacy_names_remain_read_compatible_but_never_grant_schema(self):
        payload = channel_payload()
        payload[PREFIX + "channelVolume"] = [
            {"name": name, "value": 2, "status": 1}
            for name in (*RAW_NAMES, "front", "top")
        ]
        result = self.project(payload)
        self.assertIs(result.values["channel_schema_valid"], False)
        for name in (*RAW_NAMES, "front", "top"):
            self.assertEqual(result.values["channel_level_" + name], 2)

    def test_mixed_naming_does_not_validate_or_project_legacy_alias(self):
        payload = channel_payload()
        payload[PREFIX + "channelVolume"][1]["name"] = "side"
        result = self.project(payload)
        self.assertIs(result.values["channel_schema_valid"], False)
        self.assertNotIn("channel_level_side", result.values)

    def test_unknown_rows_and_extra_fields_stay_private_and_input_is_unchanged(self):
        payload = channel_payload()
        payload[PREFIX + "channelVolume"].append(
            {
                "name": "Future_private_name",
                "value": 7,
                "status": 1,
                "private": "DO_NOT_EXPORT",
            }
        )
        payload[PREFIX + "channelVolume"][0]["private"] = "DO_NOT_EXPORT"
        before = deepcopy(payload)
        result = self.project(payload)
        self.assertIs(result.values["channel_schema_valid"], True)
        self.assertNotIn("Future_private_name", repr(result))
        self.assertNotIn("DO_NOT_EXPORT", repr(result))
        self.assertEqual(payload, before)

    def test_unknown_rows_are_still_fully_validated(self):
        for rows in (
            [{"name": "Future", "value": True, "status": 1}],
            [{"name": "Future", "value": 0, "status": "on"}],
            [{"name": "Future", "value": 0, "status": 1}] * 2,
        ):
            with self.subTest(rows=rows):
                payload = channel_payload()
                payload[PREFIX + "channelVolume"].extend(rows)
                self.assertIs(
                    self.project(payload).values["channel_schema_valid"], False
                )

    def test_all_safe_fields_are_registered_for_ownership_and_ttl(self):
        channels = {"channel_schema_valid"}
        for name in (*RAW_NAMES, "front", "top"):
            channels.update({"channel_level_" + name, "channel_status_" + name})
        self.assertEqual(RESOURCE_FIELDS[CHANNEL_HREF], channels)
        self.assertEqual(
            RESOURCE_FIELDS[SYNC_HREF], {"audio_sync", "audio_sync_supported"}
        )
        self.assertTrue(channels | {"audio_sync", "audio_sync_supported"} <= OCF_FIELDS)


class TestOcfAudioSyncSupport(TestCase):
    def test_verified_read_projects_value_and_explicit_support(self):
        result = project_ocf(
            SYNC_HREF, {PREFIX + "isSupported": 1, PREFIX + "audiosync": 0}
        )
        self.assertEqual(result.values, {"audio_sync": 0, "audio_sync_supported": True})

    def test_support_zero_is_false_not_a_fake_sync_value(self):
        result = project_ocf(
            SYNC_HREF, {PREFIX + "isSupported": 0, PREFIX + "audiosync": 7}
        )
        self.assertEqual(result.values, {"audio_sync_supported": False})
        self.assertIs(result.supported, False)

    def test_missing_or_untyped_support_does_not_manufacture_support(self):
        for payload in (
            {},
            *(
                {PREFIX + "isSupported": value}
                for value in (None, True, False, "1", 1.0, -1, 2)
            ),
        ):
            with self.subTest(payload=payload):
                result = project_ocf(SYNC_HREF, {**payload, PREFIX + "audiosync": 5})
                self.assertNotIn("audio_sync_supported", result.values)
                self.assertIsNone(result.supported)
                self.assertEqual(result.values, {})
                self.assertEqual(
                    set(result.invalidated), {"audio_sync", "audio_sync_supported"}
                )

    def test_untyped_sync_value_does_not_become_zero(self):
        for value in (None, True, False, "0", 0.0):
            with self.subTest(value=value):
                result = project_ocf(
                    SYNC_HREF, {PREFIX + "isSupported": 1, PREFIX + "audiosync": value}
                )
                self.assertEqual(result.values, {"audio_sync_supported": True})
                self.assertEqual(result.invalidated, ("audio_sync",))

    def test_missing_sync_invalidates_only_old_value_not_explicit_support(self):
        result = project_ocf(SYNC_HREF, {PREFIX + "isSupported": 1})
        self.assertEqual(result.values, {"audio_sync_supported": True})
        self.assertEqual(result.invalidated, ("audio_sync",))

    def test_support_zero_invalidates_old_sync_value(self):
        result = project_ocf(SYNC_HREF, {PREFIX + "isSupported": 0})
        self.assertEqual(result.values, {"audio_sync_supported": False})
        self.assertEqual(result.invalidated, ("audio_sync",))


class TestOcfAuthoritativeGetCache(TestCase):
    def setUp(self):
        self.now = 1.0
        self.state = SoundbarState(ocf=True, clock=lambda: self.now)

    def receive(self, href, payload):
        result = project_ocf(href, payload)
        self.assertFalse(set(result.invalidated) & result.values.keys())
        self.state.invalidate("ocf", result.invalidated)
        self.state.merge("ocf", result.values)
        return result

    def test_missing_channel_cannot_use_cached_level_under_new_valid_schema(self):
        self.receive(CHANNEL_HREF, channel_payload())
        self.assertEqual(self.state.value("channel_level_rear"), 4)
        self.now = 10
        payload = channel_payload()
        payload[PREFIX + "channelVolume"] = payload[PREFIX + "channelVolume"][:1]
        self.receive(CHANNEL_HREF, payload)
        self.assertIs(self.state.value("channel_schema_valid"), True)
        self.assertIsNone(self.state.value("channel_level_rear"))
        self.assertIsNone(self.state.value("channel_status_rear"))
        self.assertEqual(self.state.value("channel_level_center"), 0)

    def test_channel_global_support_loss_immediately_revokes_all_observations(self):
        for missing, support in [
            (True, None),
            *((False, value) for value in (None, True, False, "1", 1.0, 0, -1, 2)),
        ]:
            with self.subTest(missing=missing, support=support):
                self.receive(CHANNEL_HREF, channel_payload())
                payload = channel_payload()
                if missing:
                    del payload[PREFIX + "isSupported"]
                else:
                    payload[PREFIX + "isSupported"] = support
                result = self.receive(CHANNEL_HREF, payload)
                self.assertEqual(result.values, {"channel_schema_valid": False})
                self.assertEqual(
                    set(result.invalidated),
                    RESOURCE_FIELDS[CHANNEL_HREF] - {"channel_schema_valid"},
                )
                for field in RESOURCE_FIELDS[CHANNEL_HREF] - {"channel_schema_valid"}:
                    self.assertIsNone(self.state.value(field), field)
                self.assertIs(self.state.value("channel_schema_valid"), False)

    def test_missing_or_invalid_channel_list_revokes_previous_observations(self):
        for missing, rows in [
            (True, None),
            *((False, value) for value in (None, [], {}, "bad")),
        ]:
            with self.subTest(missing=missing, rows=rows):
                self.receive(CHANNEL_HREF, channel_payload())
                payload = {PREFIX + "isSupported": 1}
                if not missing:
                    payload[PREFIX + "channelVolume"] = rows
                self.receive(CHANNEL_HREF, payload)
                for field in RESOURCE_FIELDS[CHANNEL_HREF] - {"channel_schema_valid"}:
                    self.assertIsNone(self.state.value(field), field)
                self.assertIs(self.state.value("channel_schema_valid"), False)

    def test_sync_get_missing_or_bad_value_cannot_retain_cached_forty(self):
        for missing, value in [
            (True, None),
            *(
                (False, value)
                for value in (None, True, False, "40", 40.0, [], {}, 1001, -1001)
            ),
        ]:
            with self.subTest(missing=missing, value=value):
                self.receive(
                    SYNC_HREF, {PREFIX + "isSupported": 1, PREFIX + "audiosync": 40}
                )
                self.assertEqual(self.state.value("audio_sync"), 40)
                payload = {PREFIX + "isSupported": 1}
                if not missing:
                    payload[PREFIX + "audiosync"] = value
                self.receive(SYNC_HREF, payload)
                self.assertIsNone(self.state.value("audio_sync"))
                self.assertIs(self.state.value("audio_sync_supported"), True)

    def test_sync_get_missing_or_bad_support_revokes_cached_authorization_and_value(
        self,
    ):
        for missing, support in [
            (True, None),
            *((False, value) for value in (None, True, False, "1", 1.0, 0, -1, 2)),
        ]:
            with self.subTest(missing=missing, support=support):
                self.receive(
                    SYNC_HREF, {PREFIX + "isSupported": 1, PREFIX + "audiosync": 40}
                )
                payload = {PREFIX + "audiosync": 40}
                if not missing:
                    payload[PREFIX + "isSupported"] = support
                result = self.receive(SYNC_HREF, payload)
                self.assertIsNone(self.state.value("audio_sync"))
                expected = False if type(support) is int and support == 0 else None
                self.assertIs(self.state.value("audio_sync_supported"), expected)
                self.assertIsNot(result.supported, True)
