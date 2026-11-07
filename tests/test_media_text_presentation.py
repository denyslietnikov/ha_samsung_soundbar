"""Track text is native HA presentation, never a transport fallback or dump."""

from unittest import IsolatedAsyncioTestCase
from unittest.mock import MagicMock

import test_coordinator as fixtures

from custom_components.samsung_soundbar.media_player import SoundbarMediaPlayer


class TestMediaTextPresentation(IsolatedAsyncioTestCase):
    setUp = fixtures.TestCoordinator.setUp
    make_device = fixtures.TestCoordinator.make_device

    def test_no_coordinator_never_exposes_legacy_track_text(self):
        device, _coordinator = self.make_device()
        device.coordinator = None
        device.device.status.media_title = "Legacy title"
        device.device.status.media_artist = "Legacy artist"
        device.device.status.media_album_name = "Legacy album"

        self.assertIsNone(device.media_title)
        self.assertIsNone(device.media_artist)
        self.assertIsNone(device.media_album_name)
        self.assertEqual(self.rpc.mock_calls, [])

    def test_facade_uses_only_coordinator_text_and_honors_revocation(self):
        device, coordinator = self.make_device()
        values = {
            "media_title": "Private title",
            "media_artist": "Private artist",
            "media_album_name": "Private album",
        }
        coordinator.media_text_value = MagicMock(side_effect=values.get)
        device.device.status.media_title = "Legacy title"

        self.assertEqual(device.media_title, "Private title")
        self.assertEqual(device.media_artist, "Private artist")
        self.assertEqual(device.media_album_name, "Private album")
        values.clear()
        self.assertIsNone(device.media_title)
        self.assertIsNone(device.media_artist)
        self.assertIsNone(device.media_album_name)
        self.assertEqual(self.rpc.mock_calls, [])

    def test_native_properties_are_live_without_extra_attributes_or_features(self):
        device, coordinator = self.make_device()
        values = {}
        coordinator.media_text_value = MagicMock(side_effect=values.get)
        entity = SoundbarMediaPlayer(device)
        features = entity.supported_features
        self.assertEqual(entity.unique_id, "soundbar-id_mp")
        self.assertIsNone(entity.media_title)
        self.assertIsNone(entity.media_artist)
        self.assertIsNone(entity.media_album_name)

        values.update(
            media_title="Private title",
            media_artist="Private artist",
            media_album_name="Private album",
        )
        self.assertEqual(entity.media_title, "Private title")
        self.assertEqual(entity.media_artist, "Private artist")
        self.assertEqual(entity.media_album_name, "Private album")
        self.assertEqual(entity.supported_features, features)
        self.assertIsNone(entity.state)
        for field in values:
            self.assertNotIn(field, entity.extra_state_attributes)
        for field in (
            "media_content_id",
            "media_duration",
            "media_position",
            "media_image_url",
            "media_image_hash",
        ):
            self.assertIsNone(getattr(entity, field))

        values.clear()
        self.assertIsNone(entity.media_title)
        self.assertIsNone(entity.media_artist)
        self.assertIsNone(entity.media_album_name)
        self.assertEqual(self.rpc.mock_calls, [])

    def test_support_data_redacts_text_without_reading_native_properties(self):
        device, coordinator = self.make_device()
        values = {
            "media_title": "Private title",
            "media_artist": "Private artist",
            "media_album_name": "Private album",
        }
        coordinator.media_text_value = MagicMock(side_effect=values.get)

        result = device.retrieve_data

        for field, text in values.items():
            self.assertNotIn(text, repr(result))
            self.assertIn(field, result["media"])
            self.assertIsNone(result["media"][field])
        coordinator.media_text_value.assert_not_called()

    async def test_discovery_snapshot_redacts_text_with_and_without_nulls(self):
        device, coordinator = self.make_device()
        values = {
            "media_title": "Private title",
            "media_artist": "Private artist",
            "media_album_name": "Private album",
        }
        coordinator.media_text_value = MagicMock(side_effect=values.get)
        entity = SoundbarMediaPlayer(device)
        self.assertEqual(entity.media_title, "Private title")
        self.assertEqual(entity.media_artist, "Private artist")
        self.assertEqual(entity.media_album_name, "Private album")
        coordinator.media_text_value.reset_mock()

        for include_null in (True, False):
            with self.subTest(include_null=include_null):
                result = await device.async_dump_discovery_snapshot(
                    include_null=include_null
                )
                self.assertIn("ocf", result)
                self.assertIn("coordinator", result)
                for field, text in values.items():
                    self.assertNotIn(text, repr(result))
                    if include_null:
                        self.assertIn(field, result["resolved"]["media"])
                        self.assertIsNone(result["resolved"]["media"][field])
                    else:
                        self.assertNotIn(field, result["resolved"]["media"])
        coordinator.media_text_value.assert_not_called()
