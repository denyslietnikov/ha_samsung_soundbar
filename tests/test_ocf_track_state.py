"""Private text ownership, bounded freshness and source-generation revocation."""

from unittest import TestCase

from custom_components.samsung_soundbar.state import SoundbarState

AIRPLAY = {"power": "on", "input_source": "WIFI", "sound_from_detail_name": "AirPlay"}
TEXT = {"media_title": "Track", "media_artist": "Artist", "media_album_name": "Album"}


class TestTrackState(TestCase):
    def setUp(self):
        self.now = 1.0
        self.state = SoundbarState(ocf=True, clock=lambda: self.now)
        self.state.transport_available.update(rpc=True, ocf=True)
        self.state.merge("rpc", AIRPLAY)

    def put_text(self):
        self.state.merge("ocf", TEXT)

    def test_ocf_is_exclusive_owner_with_short_ttl_and_no_rpc_fallback(self):
        self.state.merge("rpc", TEXT)
        self.assertIsNone(self.state.value("media_title"))
        self.put_text()
        self.assertEqual(self.state.value("media_title"), "Track")
        self.assertEqual(self.state.resolve("media_title").source, "ocf")
        self.assertEqual(self.state.resolve("media_title").ttl, 15.0)
        self.now = 17
        self.state.merge("rpc", AIRPLAY)
        self.assertIsNone(self.state.value("media_title"))

    def test_same_context_refresh_does_not_clear_text_or_advance_generation(self):
        self.put_text()
        context = self.state.media_context()
        self.now = 3
        self.state.merge("rpc", {**AIRPLAY, "volume_level": 0.1})
        self.assertEqual(self.state.media_context(), context)
        self.assertEqual(self.state.value("media_title"), "Track")

    def test_a_to_b_to_a_invalidates_text_and_old_context_token(self):
        self.put_text()
        before = self.state.media_context()
        self.state.merge(
            "rpc", {"input_source": "BT", "sound_from_detail_name": "External Device"}
        )
        self.assertIsNone(self.state.media_context())
        self.state.merge("rpc", AIRPLAY)
        self.assertNotEqual(self.state.media_context(), before)
        self.assertIsNone(self.state.value("media_title"))

    def test_out_of_order_rpc_read_cannot_clear_current_text(self):
        self.put_text()
        before = self.state.media_context()
        self.state.merge("rpc", {"power": "off"}, observed_at=0.0)
        self.assertEqual(self.state.media_context(), before)
        self.assertEqual(self.state.value("media_title"), "Track")

    def test_explicit_context_writes_and_invalidations_revoke_text(self):
        for action in ("begin", "expect", "invalidate"):
            with self.subTest(action=action):
                self.setUp()
                self.put_text()
                before = self.state.media_context()
                if action == "begin":
                    self.state.begin_write({"input_source"}, source="rpc")
                elif action == "expect":
                    self.state.expect({"input_source": "BT"}, source="rpc")
                else:
                    self.state.invalidate("rpc", {"input_source"})
                self.assertIsNone(self.state.value("media_title"))
                self.assertNotEqual(self.state.media_context(), before)

    def test_pending_context_cannot_authorize_new_text(self):
        self.put_text()
        self.state.expect({"power": "on"}, source="rpc")
        self.state.merge("ocf", TEXT)
        self.assertIsNone(self.state.media_context())
        self.assertIsNone(self.state.value("media_title"))

    def test_stale_context_recovery_cannot_revive_old_metadata(self):
        self.put_text()
        before = self.state.media_context()
        self.now = 17
        self.assertIsNone(self.state.media_context())
        self.state.merge("rpc", AIRPLAY)
        self.assertNotEqual(self.state.media_context(), before)
        self.assertIsNone(self.state.value("media_title"))

    def test_other_ocf_field_failures_do_not_clear_metadata(self):
        self.put_text()
        before = self.state.media_context()
        self.state.invalidate("ocf", {"bass_mode"})
        self.assertEqual(self.state.media_context(), before)
        self.assertEqual(self.state.value("media_title"), "Track")

    def test_native_roon_text_is_ocf_owned_and_expires_independently(self):
        self.state.merge("rpc", {**AIRPLAY, "sound_from_detail_name": "Roon"})
        self.assertIsNotNone(self.state.media_context())
        self.state.merge("rpc", TEXT)
        self.assertIsNone(self.state.value("media_title"))
        self.put_text()
        self.assertEqual(self.state.value("media_title"), "Track")
        self.assertEqual(self.state.resolve("media_title").source, "ocf")
        self.assertEqual(self.state.resolve("media_title").ttl, 15.0)
        self.now = 17
        self.state.merge("rpc", {**AIRPLAY, "sound_from_detail_name": "Roon"})
        self.assertIsNone(self.state.value("media_title"))

    def test_provider_change_revokes_text_even_when_normalized_input_is_wifi(self):
        for before, after in (("AirPlay", "Roon"), ("Roon", "AirPlay")):
            with self.subTest(before=before, after=after):
                self.setUp()
                self.state.merge("rpc", {"sound_from_detail_name": before})
                self.put_text()
                token = self.state.media_context()
                self.assertIsNotNone(token)
                self.state.merge("rpc", {"sound_from_detail_name": after})
                self.assertIsNotNone(self.state.media_context())
                self.assertNotEqual(self.state.media_context(), token)
                self.assertIsNone(self.state.value("media_title"))
                self.state.merge("rpc", {"sound_from_detail_name": before})
                self.assertNotEqual(self.state.media_context(), token)

    def test_unverified_streaming_never_surfaces_retained_roon_metadata(self):
        for detail in ("Google Cast", "External Device", "Spotify", [], False, 1):
            with self.subTest(detail=detail):
                self.setUp()
                self.state.merge("rpc", {"sound_from_detail_name": "Roon"})
                self.put_text()
                self.assertEqual(self.state.value("media_title"), "Track")
                self.state.merge("rpc", {"sound_from_detail_name": detail})
                self.state.merge("ocf", TEXT)
                self.assertIsNone(self.state.media_context())
                self.assertIsNone(self.state.value("media_title"))

    def test_local_only_never_accepts_ocf_or_rpc_track_text(self):
        self.state.use_ocf = False
        self.state.merge("rpc", TEXT)
        self.put_text()
        self.assertIsNone(self.state.media_context())
        self.assertIsNone(self.state.value("media_title"))
