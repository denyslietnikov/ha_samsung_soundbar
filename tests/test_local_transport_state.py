"""RPC and optional OCF state have independent local owners."""

from unittest import TestCase

from custom_components.samsung_soundbar.state import SoundbarState


class TestLocalTransportState(TestCase):
    def setUp(self):
        self.now = 1.0
        self.state = SoundbarState(ocf=True, clock=lambda: self.now)

    def test_only_local_transports_exist_and_rpc_controls_availability(self):
        self.assertEqual(set(self.state.records), {"rpc", "ocf"})
        self.assertEqual(set(self.state.transport_available), {"rpc", "ocf"})
        self.state.transport_available["ocf"] = True
        self.assertFalse(self.state.snapshot().available)
        self.state.transport_available["rpc"] = True
        self.assertTrue(self.state.snapshot().available)

    def test_rpc_core_ocf_settings_and_metadata_keep_their_owners(self):
        self.state.merge("rpc", {"power": "on", "bass_mode": False})
        self.state.merge("ocf", {"power": "off", "bass_mode": True})
        self.assertEqual(self.state.value("power"), "on")
        self.assertIs(self.state.value("bass_mode"), True)
        metadata = {
            "media_title": "Track",
            "media_artist": "Artist",
            "media_coverart_url": "",
            "playback_status": "playing",
            "sound_from_mode": 1,
            "media_duration": 120,
            "media_position": 20,
            "capability_hygiene": {},
        }
        self.state.merge("rpc", metadata)
        for key, value in metadata.items():
            if key in {"media_title", "media_artist"}:
                self.assertIsNone(self.state.value(key))
                self.assertNotIn(key, self.state.snapshot().values)
                continue
            self.assertEqual(self.state.value(key), value)
            self.assertEqual(self.state.snapshot().values[key], value)

    def test_partial_reads_do_not_refresh_omitted_or_null_fields(self):
        self.state.merge("rpc", {"volume_level": 0.08, "power": "on"})
        self.state.merge("ocf", {"bass_mode": True, "night_mode": False})
        self.now = 20
        self.state.merge("rpc", {"power": "on", "volume_level": None})
        self.state.merge("ocf", {"bass_mode": None})
        self.assertEqual(self.state.diagnostics()["volume_level"]["age_seconds"], 19)
        self.assertEqual(self.state.diagnostics()["bass_mode"]["age_seconds"], 19)
        self.now = 122
        self.assertIsNone(self.state.value("volume_level"))
        self.assertIsNone(self.state.value("bass_mode"))
        self.assertEqual(self.state.value("power"), "on")

    def test_streaming_ttl_and_static_identity_are_preserved(self):
        self.state.merge(
            "rpc",
            {
                "local_identifier": "22_AV_HW-Q800F",
                "input_source": "WIFI",
                "sound_from_detail_name": "AirPlay",
            },
        )
        self.now = 17
        self.assertIsNone(self.state.value("sound_from_detail_name"))
        self.assertEqual(self.state.value("input_source"), "WIFI")
        self.now = 200
        self.assertEqual(self.state.value("local_identifier"), "22_AV_HW-Q800F")
        self.assertIsNone(self.state.value("input_source"))

    def test_rpc_write_guard_rejects_old_reads_and_expires(self):
        self.state.merge("rpc", {"sound_mode": "Standard"})
        self.now = 2
        self.state.expect({"sound_mode": "Surround"})
        self.state.merge("rpc", {"sound_mode": "Game Pro"}, observed_at=1)
        self.assertEqual(self.state.value("sound_mode"), "Surround")
        self.state.merge("ocf", {"sound_mode": "Surround"})
        self.assertIn("sound_mode", self.state.pending)
        self.state.merge("rpc", {"sound_mode": "Standard"})
        self.now = 8
        self.assertEqual(self.state.value("sound_mode"), "Standard")

    def test_ocf_does_not_publish_optimistic_values(self):
        self.state.merge("ocf", {"bass_mode": False})
        self.state.expect({"bass_mode": True}, source="ocf", write_only=True)
        self.assertIs(self.state.value("bass_mode"), False)
        self.assertNotIn("bass_mode", self.state.pending)

    def test_rpc_only_ignores_ocf_even_with_retained_client_data(self):
        state = SoundbarState()
        state.merge("ocf", {"bass_mode": True})
        state.merge("rpc", {"bass_mode": True})
        self.assertIsNone(state.value("bass_mode"))
        self.assertEqual(state.records["ocf"], {})

    def test_ocf_invalidation_is_field_scoped_and_does_not_touch_rpc(self):
        self.state.merge("rpc", {"power": "on"})
        self.state.merge("ocf", {"bass_mode": True, "night_mode": False})
        self.state.invalidate("ocf", {"bass_mode"})
        self.assertIsNone(self.state.value("bass_mode"))
        self.assertIs(self.state.value("night_mode"), False)
        self.assertEqual(self.state.value("power"), "on")

    def test_older_observation_cannot_overwrite_newer_readback(self):
        self.state.merge("rpc", {"volume_level": 0.1}, observed_at=2)
        self.state.merge("rpc", {"volume_level": 0.08}, observed_at=1)
        self.assertEqual(self.state.value("volume_level"), 0.1)
