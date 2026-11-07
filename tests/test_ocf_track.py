"""UI track text is validated separately from redacted diagnostics."""

import importlib.util
from copy import deepcopy
from pathlib import Path
from unittest import TestCase


class TestOcfTrack(TestCase):
    def setUp(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "custom_components/samsung_soundbar/ocf_track.py"
        )
        self.assertTrue(path.is_file(), "pure track projector is missing")
        spec = importlib.util.spec_from_file_location("test_pure_ocf_track", path)
        self.track = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.track)

    def test_only_title_artist_album_are_projected_without_mutating_payload(self):
        payload = {
            "title": "  Caf\u00e9 \U0001f3b5  ",
            "artist": " \u041c\u0443\u0437\u0438\u043a\u0430 ",
            "album": "\u00a0\u65e5\u672c\u8a9e\u00a0",
            "thumbnail": "private-image",
            "trackUri": "private-uri",
            "duration": 123,
            "source": "private-source",
        }
        before = deepcopy(payload)
        self.assertEqual(
            self.track.project_track_text(payload),
            {
                "media_title": "Caf\u00e9 \U0001f3b5",
                "media_artist": "\u041c\u0443\u0437\u0438\u043a\u0430",
                "media_album_name": "\u65e5\u672c\u8a9e",
            },
        )
        self.assertEqual(payload, before)

    def test_title_is_required_and_invalid_title_discards_optional_text(self):
        class Text(str):
            pass

        for title in (
            None,
            "",
            "   ",
            1,
            True,
            [],
            {},
            b"Track",
            Text("Track"),
            "x" * 1025,
            "\tTrack",
            "Track\n",
            "a\x00b",
            "a\x7fb",
            "a\x85b",
            "a\ud800b",
            "a\udfffb",
        ):
            with self.subTest(title=repr(title)):
                self.assertEqual(
                    self.track.project_track_text(
                        {"title": title, "artist": "Artist", "album": "Album"}
                    ),
                    {},
                )
        self.assertEqual(self.track.project_track_text({"artist": "Artist"}), {})

    def test_missing_empty_or_invalid_optional_fields_do_not_survive_snapshot(self):
        for optional in (None, "", " ", 1, False, [], {}, "x" * 1025, "a\nb", "\ud800"):
            with self.subTest(optional=repr(optional)):
                self.assertEqual(
                    self.track.project_track_text(
                        {"title": " Track ", "artist": optional, "album": optional}
                    ),
                    {"media_title": "Track"},
                )
        self.assertEqual(
            self.track.project_track_text({"title": "Track", "album": "Album"}),
            {"media_title": "Track", "media_album_name": "Album"},
        )

    def test_limit_is_1024_raw_characters_without_truncation(self):
        title = "\U0001f3b5" * 1024
        self.assertEqual(
            self.track.project_track_text({"title": title}), {"media_title": title}
        )
        self.assertEqual(self.track.project_track_text({"title": " " + title}), {})

    def test_non_object_payloads_are_empty_and_wrong_protocol_names_are_ignored(self):
        for payload in (None, [], (), 1, True, "private-text"):
            with self.subTest(payload=payload):
                self.assertEqual(self.track.project_track_text(payload), {})
        self.assertEqual(
            self.track.project_track_text(
                {"x.com.samsung.networkaudio.title": "Track", "Title": "Track"}
            ),
            {},
        )

    def test_exported_contract(self):
        self.assertEqual(self.track.TRACK_HREF, "/sec/networkaudio/av/audioTrackData")
        self.assertEqual(
            self.track.MEDIA_TEXT_FIELDS,
            frozenset({"media_title", "media_artist", "media_album_name"}),
        )
        self.assertIs(type(self.track.MEDIA_TEXT_FIELDS), frozenset)
        self.assertEqual(self.track.MEDIA_TEXT_TTL, 15.0)
        self.assertEqual(self.track.MEDIA_POLL_INTERVAL, 5.0)
