"""Pure opt-in media diagnostics never disclose device metadata or identifiers."""

import importlib.util
import json
from copy import deepcopy
from pathlib import Path
from unittest import TestCase

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "samsung_soundbar"
    / "ocf_media.py"
)
BASE = "/sec/networkaudio/"
PREFIX = "x.com.samsung.networkaudio."
HREFS = tuple(
    BASE + suffix
    for suffix in (
        "soundFrom",
        "playback",
        "musicinfo",
        "av/audioTrackData",
        "av/mediaPlayback",
        "av/mediaTrackControl",
        "av/mediaPlaybackShuffle",
        "av/mediaPlaybackRepeat",
    )
)
SECRET = "PRIVATE_METADATA_4684300b-32ae-4438-9887-5339c6e40000"


class TestOcfMedia(TestCase):
    def setUp(self):
        self.assertTrue(MODULE_PATH.is_file(), "pure media projector is missing")
        spec = importlib.util.spec_from_file_location(
            "test_pure_ocf_media", MODULE_PATH
        )
        self.media = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.media)

    def fields(self, suffix, payload):
        result = self.media.project_media_diagnostic(BASE + suffix, payload)
        self.assertEqual(set(result), {"fields"})
        return result["fields"]

    def test_exact_href_allowlist_is_immutable_and_ordered(self):
        self.assertIs(type(self.media.MEDIA_HREFS), tuple)
        self.assertEqual(self.media.MEDIA_HREFS, HREFS)

    def test_unknown_href_and_non_dictionary_payload_raise_safe_value_error(self):
        for href in (SECRET, "/oic/sec/cred", BASE + "woofer", None, [], 1):
            with self.subTest(href=href), self.assertRaises(ValueError) as error:
                self.media.project_media_diagnostic(href, {})
            self.assertNotIn(SECRET, str(error.exception))
        for payload in (None, [], (), SECRET, 1, True):
            with self.subTest(payload=payload), self.assertRaises(ValueError) as error:
                self.media.project_media_diagnostic(HREFS[0], payload)
            self.assertNotIn(SECRET, str(error.exception))

    def test_playback_status_has_only_fixed_allowlisted_values(self):
        for status in ("play", "pause", "stop", "playing", "paused", "stopped", "idle"):
            with self.subTest(status=status):
                self.assertEqual(
                    self.fields("playback", {PREFIX + "status": status}),
                    {"status": {"state": "value", "value": status}},
                )

    def test_observed_q800f_uppercase_status_is_preserved_not_interpreted(self):
        for status in ("PLAYING", "STOPPED", "PAUSED"):
            with self.subTest(status=status):
                self.assertEqual(
                    self.fields("playback", {PREFIX + "status": status})["status"],
                    {"state": "value", "value": status},
                )
        self.assertEqual(
            self.fields("playback", {PREFIX + "status": "BUFFERING"})["status"],
            {"state": "unrecognized"},
        )

    def test_enum_missing_invalid_empty_and_unrecognized_are_distinct(self):
        for payload, expected in (
            ({}, "missing"),
            ({PREFIX + "status": None}, "invalid"),
            ({PREFIX + "status": True}, "invalid"),
            ({PREFIX + "status": []}, "invalid"),
            ({PREFIX + "status": ""}, "empty"),
            ({PREFIX + "status": SECRET}, "unrecognized"),
            ({PREFIX + "status": "PLAY"}, "unrecognized"),
        ):
            with self.subTest(expected=expected, payload=payload):
                self.assertEqual(
                    self.fields("playback", payload)["status"], {"state": expected}
                )

    def test_media_playback_lists_and_counters(self):
        fields = self.fields(
            "av/mediaPlayback",
            {
                "modes": ["play"],
                "supportedModes": ["play", "pause", "stop", "fastforward", "rewind"],
                "position": 0,
                "index": 2**31 - 1,
                "trackIndex": SECRET,
            },
        )
        self.assertEqual(
            fields,
            {
                "modes": {"state": "value", "value": ["play"]},
                "supportedModes": {
                    "state": "value",
                    "value": ["play", "pause", "stop", "fastforward", "rewind"],
                },
                "position": {"state": "value", "value": 0},
                "index": {"state": "value", "value": 2**31 - 1},
            },
        )

    def test_numeric_fields_are_exact_nonnegative_bounded_integers(self):
        for suffix, key in (
            ("av/mediaPlayback", "position"),
            ("av/mediaPlayback", "index"),
            ("av/audioTrackData", "duration"),
            ("av/audioTrackData", "trackNumber"),
        ):
            for invalid in (
                True,
                False,
                -1,
                2**31,
                "0",
                0.0,
                None,
                [],
                {},
                float("nan"),
                float("inf"),
            ):
                with self.subTest(suffix=suffix, key=key, invalid=invalid):
                    field = self.fields(suffix, {key: invalid})[key]
                    self.assertEqual(field, {"state": "invalid"})
            for valid in (0, 25, 2**31 - 1):
                with self.subTest(suffix=suffix, key=key, valid=valid):
                    self.assertEqual(
                        self.fields(suffix, {key: valid})[key],
                        {"state": "value", "value": valid},
                    )

    def test_enum_lists_are_bounded_unique_typed_and_never_partially_exported(self):
        for key in ("modes", "supportedModes"):
            for value, state in (
                ([], "empty"),
                (["play", "play"], "invalid"),
                (["play"] * 9, "invalid"),
                (["play", 1], "invalid"),
                ([True], "invalid"),
                ([[]], "invalid"),
                (["play", SECRET], "unrecognized"),
                (None, "invalid"),
                ("play", "invalid"),
                (("play",), "invalid"),
            ):
                with self.subTest(key=key, value=value):
                    self.assertEqual(
                        self.fields("av/mediaPlayback", {key: value})[key],
                        {"state": state},
                    )

    def test_shuffle_and_repeat_have_separate_allowlists(self):
        for suffix, modes in (
            ("av/mediaPlaybackShuffle", ("enabled", "disabled")),
            (
                "av/mediaPlaybackRepeat",
                ("none", "all", "one", "disabled", "enabled", "repeatall", "repeatone"),
            ),
        ):
            for mode in modes:
                with self.subTest(suffix=suffix, mode=mode):
                    self.assertEqual(
                        self.fields(suffix, {"mode": mode}),
                        {"mode": {"state": "value", "value": mode}},
                    )
        self.assertEqual(
            self.fields("av/mediaPlaybackShuffle", {"mode": "repeatall"}),
            {"mode": {"state": "unrecognized"}},
        )

    def test_metadata_strings_are_presence_only_including_urls_and_uuids(self):
        for key in ("title", "artist", "album", "thumbnail", "source", "trackUri"):
            for content, state in (
                ("", "empty"),
                (SECRET, "present"),
                ("https://example.invalid/" + SECRET, "present"),
                ("x" * 4096, "present"),
            ):
                with self.subTest(key=key, state=state):
                    field = self.fields("av/audioTrackData", {key: content})[key]
                    self.assertEqual(field, {"state": state})
            for invalid in (None, 0, True, [], {}, "x" * 4097):
                with self.subTest(key=key, invalid_type=type(invalid)):
                    self.assertEqual(
                        self.fields("av/audioTrackData", {key: invalid})[key],
                        {"state": "invalid"},
                    )

    def test_soundfrom_projects_only_numeric_fields_and_presence(self):
        fields = self.fields(
            "soundFrom",
            {
                PREFIX + "sbMode": 25,
                PREFIX + "name": SECRET,
                PREFIX + "soundFrom": {
                    "status": 1,
                    "deviceType": 4,
                    "name": SECRET,
                    "connectionType": SECRET,
                    "di": SECRET,
                    "duid": SECRET,
                    "ip": SECRET,
                    "mac": SECRET,
                    "sbMode": 999,
                },
            },
        )
        self.assertEqual(
            fields,
            {
                "sbMode": {"state": "value", "value": 25},
                "name": {"state": "present"},
                "soundFrom.status": {"state": "value", "value": 1},
                "soundFrom.deviceType": {"state": "value", "value": 4},
                "soundFrom.name": {"state": "present"},
                "soundFrom.connectionType": {"state": "present"},
            },
        )

    def test_soundfrom_numeric_fields_reject_booleans_and_bounds(self):
        for key in ("status", "deviceType"):
            for invalid in (True, False, None, -1, 2**31, "1", 1.0):
                with self.subTest(key=key, invalid=invalid):
                    self.assertEqual(
                        self.fields(
                            "soundFrom", {PREFIX + "soundFrom": {key: invalid}}
                        )["soundFrom." + key],
                        {"state": "invalid"},
                    )
        for invalid in (True, False, None, -1, 2**31, "1", 1.0):
            with self.subTest(sbMode=invalid):
                self.assertEqual(
                    self.fields("soundFrom", {PREFIX + "sbMode": invalid})["sbMode"],
                    {"state": "invalid"},
                )

    def test_musicinfo_reports_fixed_control_presence_not_commands_or_urls(self):
        controls = (
            "play",
            "next",
            "prev",
            "skip_forward",
            "skip_backward",
            "shuffle",
            "repeat",
            "loop",
            "thumbs_up",
            "thumbs_down",
        )
        payload = {
            "control": {key: SECRET for key in controls},
            "contentProvider": {"name": "", "logo": SECRET},
        }
        payload["control"][SECRET] = SECRET
        fields = self.fields("musicinfo", payload)
        self.assertEqual(
            fields,
            {
                **{"control." + key: {"state": "present"} for key in controls},
                "contentProvider.name": {"state": "empty"},
            },
        )

    def test_missing_nested_fields_differ_from_invalid_parent_objects(self):
        for suffix, parent, field in (
            ("soundFrom", PREFIX + "soundFrom", "soundFrom.status"),
            ("musicinfo", "control", "control.play"),
            ("musicinfo", "contentProvider", "contentProvider.name"),
        ):
            with self.subTest(suffix=suffix, parent=parent):
                self.assertEqual(self.fields(suffix, {})[field], {"state": "missing"})
                self.assertEqual(
                    self.fields(suffix, {parent: {}})[field], {"state": "missing"}
                )
                for invalid in (None, [], SECRET, 1, True):
                    self.assertEqual(
                        self.fields(suffix, {parent: invalid})[field],
                        {"state": "invalid"},
                    )

    def test_track_control_without_reported_fields_does_not_invent_commands(self):
        self.assertEqual(
            self.fields("av/mediaTrackControl", {"rt": [SECRET], "if": [SECRET]}), {}
        )
        self.assertEqual(
            self.fields(
                "av/mediaTrackControl",
                {
                    "action": SECRET,
                    "actions": [SECRET],
                    "supportedActions": [SECRET],
                },
            ),
            {},
        )

    def test_whitespace_only_metadata_is_empty(self):
        for value in (" ", "\t\r\n", " \t \n "):
            with self.subTest(value=value):
                for key in (
                    "title",
                    "artist",
                    "album",
                    "thumbnail",
                    "source",
                    "trackUri",
                ):
                    self.assertEqual(
                        self.fields("av/audioTrackData", {key: value})[key],
                        {"state": "empty"},
                    )
                fields = self.fields(
                    "musicinfo",
                    {
                        "control": {"play": value},
                        "contentProvider": {"name": value},
                    },
                )
                self.assertEqual(fields["control.play"], {"state": "empty"})
                self.assertEqual(fields["contentProvider.name"], {"state": "empty"})
                fields = self.fields(
                    "soundFrom",
                    {
                        PREFIX + "name": value,
                        PREFIX + "soundFrom": {"name": value, "connectionType": value},
                    },
                )
                for key in ("name", "soundFrom.name", "soundFrom.connectionType"):
                    self.assertEqual(fields[key], {"state": "empty"})

    def test_unknown_and_known_sensitive_values_never_leak_and_output_is_bounded(self):
        payload = {
            key: SECRET
            for key in (
                "title",
                "artist",
                "album",
                "thumbnail",
                "source",
                "trackUri",
                "di",
                "duid",
                "UUID",
                "private",
                "rt",
                "if",
            )
        }
        payload.update(
            {SECRET + str(index): {"secret": SECRET} for index in range(1000)}
        )
        for href in HREFS:
            with self.subTest(href=href):
                result = self.media.project_media_diagnostic(href, payload)
                output = json.dumps(result)
                self.assertNotIn(SECRET, output)
                self.assertLess(len(output), 2048)
                self.assertFalse(set(result) & {"raw", "payload", "uuid", "supported"})

    def test_input_is_unchanged_and_exported_lists_are_independent(self):
        payload = {
            "modes": ["play"],
            "supportedModes": ["play", "pause"],
            "private": {"key": SECRET},
        }
        before = deepcopy(payload)
        fields = self.fields("av/mediaPlayback", payload)
        self.assertEqual(payload, before)
        fields["supportedModes"]["value"].append("stop")
        self.assertEqual(payload, before)

    def test_plain_or_prefixed_lookalikes_do_not_replace_fixed_protocol_paths(self):
        self.assertEqual(
            self.fields("playback", {"status": "play"})["status"], {"state": "missing"}
        )
        self.assertEqual(
            self.fields("av/audioTrackData", {PREFIX + "title": SECRET})["title"],
            {"state": "missing"},
        )
