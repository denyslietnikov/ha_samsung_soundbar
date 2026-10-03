"""Explicit Cloud exclusions must not disable LAN or infer missing features."""

import asyncio
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.exceptions import HomeAssistantError
from pysmartthings import Attribute, Capability, Status

from custom_components.samsung_soundbar import sensor
from custom_components.samsung_soundbar.api_extension.smartthings_compat import (
    SmartThingsDeviceCompat,
)
from custom_components.samsung_soundbar.api_extension.SoundbarDevice import (
    SoundbarDevice,
)
from custom_components.samsung_soundbar.const import (
    CONF_ENTRY_DEVICE_ID,
    CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
)
from custom_components.samsung_soundbar.coordinator import SoundbarCoordinator
from custom_components.samsung_soundbar.entity_updates import (
    register_device_update_listener,
)
from custom_components.samsung_soundbar.switch import SoundbarSwitchAdvancedAudio


def status(**values):
    return {key: Status(value=value) for key, value in values.items()}


class TestCapabilityHygiene(IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = MagicMock()
        self.api.get_device_status = AsyncMock()
        self.api.execute_device_command = AsyncMock()
        self.device = SmartThingsDeviceCompat(self.api, SimpleNamespace(device_id="id"))
        self.status = self.device.status

    async def test_filters_components_and_enum_capabilities_without_mutating_api_data(
        self,
    ):
        main = {
            Capability.CUSTOM_DISABLED_COMPONENTS: {
                Attribute.DISABLED_COMPONENTS: Status(value=["rear"])
            },
            Capability.CUSTOM_DISABLED_CAPABILITIES: {
                Attribute.DISABLED_CAPABILITIES: Status(value=["audioVolume"])
            },
            Capability.AUDIO_VOLUME: {Attribute.VOLUME: Status(value=8)},
            "audioMute": status(mute="muted"),
        }
        data = {"main": main, "rear": {"audioVolume": status(volume=20)}}
        self.api.get_device_status.return_value = data
        await self.status.refresh()
        self.assertNotIn("rear", self.status._components)
        self.assertFalse(self.status.has_capability("audioVolume"))
        self.assertIsNone(self.status.attribute_value("audioVolume", "volume"))
        self.assertNotIn("volume", self.status.attributes)
        self.assertTrue(self.status.mute)
        self.assertIn(Capability.AUDIO_VOLUME, main)
        self.assertIn("rear", data)
        with self.assertRaisesRegex(HomeAssistantError, "explicitly disabled"):
            await self.device.set_volume(10)
        self.api.execute_device_command.assert_not_awaited()

    async def test_missing_null_metadata_retains_exclusions_empty_list_clears_them(
        self,
    ):
        self.api.get_device_status.return_value = {
            "main": {
                "custom.disabledCapabilities": status(
                    disabledCapabilities=["audioVolume"]
                ),
                "audioVolume": status(volume=8),
            }
        }
        await self.status.refresh()
        for metadata in (
            {},
            {"custom.disabledCapabilities": status(disabledCapabilities=None)},
        ):
            self.api.get_device_status.return_value = {
                "main": {
                    **metadata,
                    "audioVolume": status(volume=9),
                }
            }
            await self.status.refresh()
            self.assertFalse(self.status.has_capability("audioVolume"))
        self.api.get_device_status.return_value = {
            "main": {
                "custom.disabledCapabilities": status(disabledCapabilities=[]),
                "audioVolume": status(volume=10),
            }
        }
        await self.status.refresh()
        self.assertEqual(self.status.volume, 10)
        self.assertTrue(self.status.has_capability("audioVolume"))

    async def test_absent_capability_retains_known_support_but_not_fabricated_readback(
        self,
    ):
        self.api.get_device_status.return_value = {
            "main": {
                "samsungvd.soundFrom": status(detailName="AirPlay"),
            }
        }
        await self.status.refresh()
        self.api.get_device_status.return_value = {"main": {}}
        await self.status.refresh()
        self.assertTrue(self.status.has_capability("samsungvd.soundFrom"))
        self.assertIsNone(self.status.sound_from_detail_name)
        self.assertFalse(self.status.is_disabled("execute"))

    async def test_exclusions_are_component_scoped_and_block_old_push(self):
        self.api.get_device_status.return_value = {
            "main": {"audioVolume": status(volume=8)},
            "rear": {
                "custom.disabledCapabilities": status(
                    disabledCapabilities=["audioVolume"]
                ),
                "audioVolume": status(volume=20),
            },
        }
        await self.status.refresh()
        self.status.apply_event(
            SimpleNamespace(
                component_id="rear",
                capability="audioVolume",
                attribute="volume",
                value=30,
            )
        )
        self.assertTrue(self.status.has_capability("audioVolume"))
        self.assertFalse(self.status.has_capability("audioVolume", "rear"))
        self.assertEqual(self.status.volume, 8)

    async def test_hybrid_lan_survives_cloud_exclusion_and_switch_becomes_unavailable(
        self,
    ):
        self.api.get_device_status.return_value = {
            "main": {
                "custom.disabledCapabilities": status(
                    disabledCapabilities=["switch", "audioVolume", "execute"]
                ),
            }
        }
        await self.status.refresh()
        device = SoundbarDevice(
            self.device,
            MagicMock(),
            100,
            "Q800F",
            control_mode=CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
            local_rpc=MagicMock(),
            enable_advanced_audio=True,
        )
        hass, entry = MagicMock(), MagicMock()
        hass.loop = asyncio.get_event_loop()
        hass.is_stopping = False
        with patch.object(SoundbarCoordinator, "_schedule_refresh"):
            coordinator = SoundbarCoordinator(hass, entry, device)
            coordinator.receive("local", {"power": "on", "volume_level": 0.08})
            coordinator.receive(
                "cloud", {"power": "off", "volume_level": 0.16, "bass_mode": True}
            )
            coordinator.transport_changed("local", True)
            coordinator.transport_changed("cloud", True)
            switch = SoundbarSwitchAdvancedAudio(
                device,
                "bassmode",
                lambda: device.bass_mode,
                device.set_bass_mode,
                device.set_bass_mode,
            )
            register_device_update_listener(entry, device, [switch])
            self.assertEqual(device.state, "on")
            self.assertEqual(device.volume_level, 0.08)
            self.assertTrue(device.can_control_volume)
            self.assertTrue(device.can_select_sound_mode)
            self.assertFalse(device.can_control_advanced_audio)
            self.assertFalse(device.bass_mode)
            self.assertFalse(switch.available)
            await coordinator.async_shutdown()

    async def test_metadata_only_update_notifies_existing_entities_and_removes_cloud_cache(
        self,
    ):
        self.api.get_device_status.return_value = {"main": {}}
        await self.status.refresh()
        device = SoundbarDevice(
            self.device,
            MagicMock(),
            100,
            "Q800F",
            control_mode=CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
            local_rpc=MagicMock(),
            enable_advanced_audio=True,
        )
        device._update_advanced_audio = AsyncMock()
        hass, entry = MagicMock(), MagicMock()
        hass.loop = asyncio.get_event_loop()
        hass.is_stopping = False
        with patch.object(SoundbarCoordinator, "_schedule_refresh"):
            coordinator = SoundbarCoordinator(hass, entry, device)
            coordinator.transport_changed("cloud", True)
            coordinator.state.expect({"bass_mode": True}, write_only=True)
            switch = SoundbarSwitchAdvancedAudio(
                device,
                "bassmode",
                lambda: device.bass_mode,
                device.set_bass_mode,
                device.set_bass_mode,
            )
            switch.hass = hass
            switch.async_write_ha_state = MagicMock()
            register_device_update_listener(entry, device, [switch])
            self.assertTrue(switch.available)
            self.api.get_device_status.return_value = {
                "main": {
                    "custom.disabledCapabilities": status(
                        disabledCapabilities=["execute"]
                    ),
                }
            }
            await device.update_cloud_status()
            self.assertFalse(switch.available)
            self.assertNotIn("bass_mode", coordinator.state.write_only)
            self.assertIsNone(coordinator.state.value("bass_mode"))
            switch.async_write_ha_state.assert_called()
            device._update_advanced_audio.assert_not_awaited()
            await coordinator.async_shutdown()

    async def test_malformed_optional_list_is_not_treated_as_reenable(self):
        self.api.get_device_status.return_value = {
            "main": {
                "custom.disabledCapabilities": status(disabledCapabilities=["execute"]),
            }
        }
        await self.status.refresh()
        for value in ("execute", [123], {"value": []}):
            self.api.get_device_status.return_value = {
                "main": {
                    "custom.disabledCapabilities": status(disabledCapabilities=value),
                }
            }
            await self.status.refresh()
            self.assertTrue(self.status.is_disabled("execute"))

    async def test_hybrid_sound_from_entity_survives_missing_optional_cloud_attribute(
        self,
    ):
        self.api.get_device_status.return_value = {"main": {}}
        await self.status.refresh()
        device = SoundbarDevice(
            self.device,
            MagicMock(),
            100,
            "Q800F",
            control_mode=CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
            local_rpc=MagicMock(),
        )
        hass, entry = MagicMock(), MagicMock()
        entry.runtime_data = SimpleNamespace(device=device)
        entry.data = {CONF_ENTRY_DEVICE_ID: "id"}
        entities = []
        await sensor.async_setup_entry(hass, entry, entities.extend)
        self.assertIn("id_sensor_sound_from", [entity.unique_id for entity in entities])

    async def test_disabled_main_component_filters_every_capability_and_push(self):
        self.api.get_device_status.return_value = {
            "main": {
                "custom.disabledComponents": status(disabledComponents=["main"]),
                "switch": status(switch="on"),
            }
        }
        await self.status.refresh()
        self.assertTrue(self.status.is_component_disabled("main"))
        self.assertEqual(self.status.attributes, {})
        self.status.apply_event(
            SimpleNamespace(
                component_id="main",
                capability="switch",
                attribute="switch",
                value="on",
            )
        )
        self.assertEqual(self.status.attributes, {})
        with self.assertRaises(HomeAssistantError):
            await self.device.switch_on()
