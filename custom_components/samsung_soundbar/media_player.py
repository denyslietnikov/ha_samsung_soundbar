import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaType,
)
from homeassistant.components.media_player.const import MediaPlayerEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api_extension.SoundbarDevice import SoundbarDevice
from .device_info import build_device_info
from .entity_updates import register_device_update_listener
from .models import SoundbarConfigEntry

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "Samsung Soundbar"
CONF_MAX_VOLUME = "max_volume"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: SoundbarConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    device = config_entry.runtime_data.device
    entities = [SoundbarMediaPlayer(device)]
    register_device_update_listener(config_entry, device, entities)
    async_add_entities(entities)


class SoundbarMediaPlayer(MediaPlayerEntity):
    _soundbar_local_only = True

    def __init__(self, device: SoundbarDevice):
        self.device = device
        self._attr_unique_id = f"{self.device.device_id}_mp"
        self._attr_device_class = MediaPlayerDeviceClass.SPEAKER

        self._attr_device_info = build_device_info(self.device)

    async def async_update(self):
        await self.device.update()

    # ---------- GENERAL SETTINGS ------------

    @property
    def supported_features(self):
        features = MediaPlayerEntityFeature(0)
        if self.device.can_turn_on_off:
            features |= MediaPlayerEntityFeature.TURN_OFF
            features |= MediaPlayerEntityFeature.TURN_ON
        if self.device.can_control_volume:
            features |= MediaPlayerEntityFeature.VOLUME_STEP
            features |= MediaPlayerEntityFeature.VOLUME_SET
        if self.device.can_mute_volume:
            features |= MediaPlayerEntityFeature.VOLUME_MUTE
        if self.device.can_select_source:
            features |= MediaPlayerEntityFeature.SELECT_SOURCE
        if self.device.can_select_sound_mode:
            features |= MediaPlayerEntityFeature.SELECT_SOUND_MODE
        return features

    @property
    def name(self):
        return self.device.device_name or DEFAULT_NAME

    @property
    def media_title(self) -> str | None:
        return self.device.media_title

    @property
    def media_artist(self) -> str | None:
        return self.device.media_artist

    @property
    def media_album_name(self) -> str | None:
        return self.device.media_album_name

    @property
    def media_content_type(self) -> MediaType | None:
        return MediaType.MUSIC if self.media_title else None

    # ---------- POWER ON/OFF ------------

    @property
    def state(self):
        return self.device.state

    async def async_turn_off(self):
        await self.device.switch_off()
        self.async_write_ha_state()

    async def async_turn_on(self):
        await self.device.switch_on()
        self.async_write_ha_state()

    # ---------- VOLUME ------------
    @property
    def volume_level(self):
        return self.device.volume_level

    @property
    def is_volume_muted(self):
        return self.device.volume_muted

    async def async_set_volume_level(self, volume):
        await self.device.set_volume(volume)
        self.async_write_ha_state()

    async def async_mute_volume(self, mute):
        await self.device.mute_volume(mute)
        self.async_write_ha_state()

    async def async_volume_up(self):
        await self.device.volume_up()
        self.async_write_ha_state()

    async def async_volume_down(self):
        await self.device.volume_down()
        self.async_write_ha_state()

    # ---------- INPUT SOURCES ------------

    @property
    def source(self):
        return self.device.input_source

    @property
    def source_list(self):
        if not self.device.can_select_source:
            return None
        return self.device.supported_input_sources

    async def async_select_source(self, source):
        await self.device.select_source(source)
        self.async_write_ha_state()

    # ---------- SOUND MODE ------------

    @property
    def sound_mode(self) -> str | None:
        return self.device.sound_mode

    @property
    def sound_mode_list(self) -> list[str] | None:
        if not self.device.can_select_sound_mode:
            return None
        return self.device.supported_soundmodes

    async def async_select_sound_mode(self, sound_mode):
        await self.device.select_sound_mode(sound_mode)
        self.async_write_ha_state()

    # ---------- SERVICE_UTILITY ------------

    async def async_set_woofer_level(self, level: int):
        await self.device.set_woofer(level)

    async def async_set_bass_mode(self, enabled: bool):
        await self.device.set_bass_mode(enabled)

    async def async_set_voice_mode(self, enabled: bool):
        await self.device.set_voice_amplifier(enabled)

    async def async_set_night_mode(self, enabled: bool):
        await self.device.set_night_mode(enabled)

    async def async_set_space_fit_sound(self, enabled: bool):
        await self.device.set_space_fit_sound(enabled)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        attrs: dict[str, Any] = {"control_mode": self.device.control_mode}
        if self.device.local_rpc_enabled:
            attrs.update(
                {
                    "local_available": self.device.local_available,
                    "local_last_error": self.device.local_last_error,
                    "local_codec": self.device.local_codec,
                    "local_identifier": self.device.local_identifier,
                }
            )
        return attrs
