import asyncio
from collections.abc import Awaitable, Callable, Iterable
import datetime
import json
import logging
import re
from typing import Any, TypeVar

from aiohttp import ClientResponseError
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from pysmartthings.exceptions import (
    SmartThingsAuthenticationFailedError,
    SmartThingsCommandError,
    SmartThingsConnectionError,
    SmartThingsForbiddenError,
)

from .const import SpeakerIdentifier, RearSpeakerMode
from ..const import (
    CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
    CONTROL_MODE_SMARTTHINGS_CLOUD,
    DOMAIN,
)
from ..local_rpc import (
    LOCAL_SOUND_MODE_VALUES,
    LocalRpcError,
    LocalSoundbarRpcClient,
)

log = logging.getLogger(__name__)

_T = TypeVar("_T")
_AUTH_ERROR_STATUSES = {401, 403}
_TRANSIENT_ERROR_STATUSES = {
    408,
    429,
    500,
    502,
    503,
    504,
    520,
    524,
}
_OPTIMISTIC_MUTE_TIMEOUT = datetime.timedelta(seconds=60)
_ARTWORK_RETRY_INTERVAL = datetime.timedelta(minutes=5)
_ARTWORK_URL_KEY_HINTS = ("art", "cover", "image", "thumbnail")
_LOCAL_SOURCE_TO_HA = {
    "HDMI_IN1": "HDMI1",
    "HDMI_IN2": "HDMI2",
    "E_ARC": "TV ARC",
    "ARC": "TV ARC",
    "D_IN": "D.IN",
    "BT": "BT",
    "WIFI_IDLE": "WIFI",
}
_HA_SOURCE_TO_LOCAL = {
    **{value: key for key, value in _LOCAL_SOURCE_TO_HA.items()},
    "HDMI1": "HDMI_IN1",
    "HDMI2": "HDMI_IN2",
    "TV ARC": "E_ARC",
    "ARC": "ARC",
    "E_ARC": "E_ARC",
    "D.IN": "D_IN",
    "D_IN": "D_IN",
    "WIFI": "WIFI_IDLE",
    "WIFI_IDLE": "WIFI_IDLE",
}
_LOCAL_SOUND_MODE_TO_HA = {
    "STANDARD": "Standard",
    "SURROUND": "Surround",
    "GAME": "Game",
    "ADAPTIVE": "Adaptive Sound",
    "MOVIE": "Movie",
    "MUSIC": "Music",
    "CLEARVOICE": "Clear Voice",
    "DTS_VIRTUAL_X": "DTS Virtual X",
}
_HA_SOUND_MODE_TO_LOCAL = {
    **{value: key for key, value in _LOCAL_SOUND_MODE_TO_HA.items()},
    **{value.upper(): value for value in LOCAL_SOUND_MODE_VALUES},
    "ADAPTIVE SOUND": "ADAPTIVE",
    "CLEAR VOICE": "CLEARVOICE",
    "DTS VIRTUAL X": "DTS_VIRTUAL_X",
}


class SoundbarDevice:
    def __init__(
            self,
            device: Any,
            session,
            max_volume: int,
            device_name: str,
            auth_provider=None,
            enable_eq: bool = False,
            enable_soundmode: bool = False,
            enable_advanced_audio: bool = False,
            enable_woofer: bool = False,
            control_mode: str = CONTROL_MODE_SMARTTHINGS_CLOUD,
            local_rpc: LocalSoundbarRpcClient | None = None,
            local_fallback_to_cloud: bool = True,
    ):
        self.device = device
        self._device_id = self.device.device_id
        self.__auth_provider = auth_provider
        self.__session = session
        self.__device_name = device_name
        self.__control_mode = control_mode
        self.__local_rpc = local_rpc
        self.__local_fallback_to_cloud = local_fallback_to_cloud
        self.__local_status: dict[str, Any] = {}
        self.__local_available = False
        self.__local_last_error: str | None = None

        self.__enable_soundmode = enable_soundmode
        self.__supported_soundmodes = []
        self.__active_soundmode = ""
        self.__soundmode_supported = False

        self.__enable_woofer = enable_woofer
        self.__woofer_level = 0
        self.__woofer_connection = ""
        self.__woofer_supported = False

        self.__enable_eq = enable_eq
        self.__active_eq_preset = ""
        self.__supported_eq_presets = []
        self.__eq_action = ""
        self.__eq_bands = []
        self.__equalizer_supported = False

        self.__enable_advanced_audio = enable_advanced_audio
        self.__voice_amplifier = 0
        self.__night_mode = 0
        self.__bass_mode = 0
        self.__advanced_audio_supported = False

        self.__media_title = ""
        self.__media_artist = ""
        self.__media_cover_url: str | None = None
        self.__media_cover_url_update_time: datetime.datetime | None = None
        self.__media_artwork_lookup_time: datetime.datetime | None = None
        self.__old_media_key = ""
        self.__last_execute_payload_dump: dict[str, Any] | None = None
        self.__unsupported_execute_payload_hrefs: set[str] = set()
        self.__optimistic_mute: bool | None = None
        self.__optimistic_mute_updated_at: datetime.datetime | None = None

        self.__max_volume = max_volume

    async def update(self):
        if self.__auth_provider is not None:
            await self.__auth_provider.async_get_access_token()

        await self.__call_smartthings(
            self.device.status.refresh,
            "refresh device status",
        )
        self.__sync_optimistic_mute()
        await self.__update_local_status()

        await self._update_media()

        if self.__enable_soundmode and not self.hybrid_mode:
            await self._update_soundmode()
        if self.__enable_advanced_audio:
            await self._update_advanced_audio()
        if self.__enable_woofer:
            await self._update_woofer()
        if self.__enable_eq:
            await self._update_equalizer()

    async def __call_smartthings(
        self,
        action: Callable[[], Awaitable[_T]],
        description: str,
    ) -> _T:
        """Call SmartThings once, refresh auth on 401/403, then fail for reauth."""
        try:
            return await action()
        except (
            SmartThingsAuthenticationFailedError,
            SmartThingsForbiddenError,
        ) as err:
            if self.__auth_provider is not None:
                log.debug(
                    "[%s] SmartThings auth failed during %s; forcing token refresh",
                    DOMAIN,
                    description,
                )
                await self.__auth_provider.async_get_access_token(force_refresh=True)
                try:
                    return await action()
                except (
                    SmartThingsAuthenticationFailedError,
                    SmartThingsForbiddenError,
                ) as retry_err:
                    raise ConfigEntryAuthFailed(
                        "SmartThings authorization is no longer valid"
                    ) from retry_err

            raise ConfigEntryAuthFailed(
                "SmartThings authorization is no longer valid"
            ) from err
        except SmartThingsConnectionError as err:
            raise ConfigEntryNotReady(
                "SmartThings service is temporarily unavailable"
            ) from err
        except SmartThingsCommandError as err:
            raise HomeAssistantError(
                f"SmartThings rejected {description}: {err}"
            ) from err

    @property
    def hybrid_mode(self) -> bool:
        return bool(
            self.__control_mode == CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS
            and self.__local_rpc is not None
        )

    @property
    def control_mode(self) -> str:
        return self.__control_mode

    @property
    def local_available(self) -> bool:
        return self.__local_available

    @property
    def local_last_error(self) -> str | None:
        return self.__local_last_error

    @property
    def local_codec(self) -> str | None:
        return self.__local_value("codec")

    @property
    def local_identifier(self) -> str | None:
        return self.__local_value("identifier")

    async def __update_local_status(self) -> None:
        if not self.hybrid_mode or self.__local_rpc is None:
            return

        try:
            self.__local_status = await self.__local_rpc.status()
            self.__local_available = True
            self.__local_last_error = None
        except LocalRpcError as err:
            self.__local_available = False
            self.__local_last_error = str(err)
            log.debug(
                "[%s] Local RPC status update failed for %s: %s",
                DOMAIN,
                self.device_name,
                err,
            )

    async def __try_local_rpc(
        self,
        action: Callable[[LocalSoundbarRpcClient], Awaitable[Any]],
        description: str,
    ) -> bool:
        if not self.hybrid_mode or self.__local_rpc is None:
            return False

        try:
            await action(self.__local_rpc)
            self.__local_available = True
            self.__local_last_error = None
            await self.__update_local_status()
            return True
        except (LocalRpcError, ValueError) as err:
            self.__local_available = False
            self.__local_last_error = str(err)
            log.warning(
                "[%s] Local RPC failed during %s for %s: %s",
                DOMAIN,
                description,
                self.device_name,
                err,
            )
            if self.__local_fallback_to_cloud:
                return False
            raise HomeAssistantError(
                f"Local soundbar RPC failed during {description}: {err}"
            ) from err

    def __local_value(self, key: str) -> Any:
        if not self.hybrid_mode or not self.__local_available:
            return None
        value = self.__local_status.get(key)
        return value if value not in ("", None) else None

    @staticmethod
    def __ha_source_from_local(source: str | None) -> str | None:
        if source is None:
            return None
        return _LOCAL_SOURCE_TO_HA.get(source, source)

    @staticmethod
    def __local_source_from_ha(source: str) -> str:
        return _HA_SOURCE_TO_LOCAL.get(source, source)

    @staticmethod
    def __ha_sound_mode_from_local(sound_mode: str | None) -> str | None:
        if sound_mode is None:
            return None
        return _LOCAL_SOUND_MODE_TO_HA.get(sound_mode, sound_mode)

    @staticmethod
    def __local_sound_mode_from_ha(sound_mode: str) -> str:
        return _HA_SOUND_MODE_TO_LOCAL.get(sound_mode, sound_mode.upper())

    async def _update_media(self):
        audio_track_status = self.device.status._attributes.get("audioTrackData")
        audio_track_data = self.__coerce_media_payload(
            getattr(audio_track_status, "value", None)
        )
        if not isinstance(audio_track_data, dict):
            self.__clear_media_metadata()
            return

        artist = self.__first_media_value(
            audio_track_data,
            ("artist", "artistName", "albumArtist", "albumArtistName", "author"),
        )
        title = self.__first_media_value(
            audio_track_data,
            ("title", "trackTitle", "songTitle", "name", "mediaTitle"),
        )
        album = self.__first_media_value(
            audio_track_data,
            ("album", "albumName", "collectionName"),
        )
        media_key = f"{artist}\0{title}\0{album}"

        self.__media_artist = artist
        self.__media_title = title

        if not artist and not title:
            self.__clear_media_artwork(media_key)
            return

        direct_artwork_url = self.__extract_media_artwork_url(audio_track_data)
        if media_key == self.__old_media_key:
            if direct_artwork_url is not None:
                self.__set_media_artwork(direct_artwork_url)
                return
            if self.__media_cover_url is not None:
                return
            if not self.__should_retry_artwork_lookup():
                return

        self.__old_media_key = media_key
        if direct_artwork_url is not None:
            self.__set_media_artwork(direct_artwork_url)
            return

        self.__media_artwork_lookup_time = datetime.datetime.now()
        self.__set_media_artwork(
            await self.get_song_title_artwork(artist, title, album)
        )

    def __set_media_artwork(self, artwork_url: str | None) -> None:
        if self.__media_cover_url == artwork_url:
            return
        self.__media_cover_url = artwork_url
        self.__media_cover_url_update_time = (
            datetime.datetime.now() if artwork_url is not None else None
        )

    def __should_retry_artwork_lookup(self) -> bool:
        if self.__media_artwork_lookup_time is None:
            return True
        return (
            datetime.datetime.now() - self.__media_artwork_lookup_time
            >= _ARTWORK_RETRY_INTERVAL
        )

    @classmethod
    def __coerce_media_payload(cls, value: Any) -> Any:
        value = cls.__maybe_json(value)
        if isinstance(value, dict):
            for key in ("audioTrackData", "trackData", "media", "data", "value"):
                nested = cls.__maybe_json(value.get(key))
                if isinstance(nested, dict):
                    return nested
        return value

    def __clear_media_metadata(self) -> None:
        self.__media_artist = ""
        self.__media_title = ""
        self.__clear_media_artwork("")

    def __clear_media_artwork(self, media_key: str) -> None:
        if self.__old_media_key == media_key and self.__media_cover_url is None:
            return
        self.__old_media_key = media_key
        self.__media_cover_url = None
        self.__media_cover_url_update_time = None

    @staticmethod
    def __clean_media_value(value: Any) -> str:
        return str(value).strip() if value is not None else ""

    @classmethod
    def __first_media_value(
        cls,
        audio_track_data: dict[str, Any],
        keys: tuple[str, ...],
    ) -> str:
        for key in keys:
            value = cls.__clean_media_value(audio_track_data.get(key))
            if value:
                return value
        return ""

    @classmethod
    def __extract_media_artwork_url(cls, audio_track_data: dict[str, Any]) -> str | None:
        for value, is_artwork_value in cls.__walk_media_items(audio_track_data):
            if not is_artwork_value:
                continue
            artwork_url = cls.__normalize_artwork_url(value)
            if artwork_url is not None:
                return artwork_url
        return None

    @classmethod
    def __walk_media_items(cls, value: Any, is_artwork_value: bool = False):
        value = cls.__maybe_json(value)
        if isinstance(value, dict):
            for key, nested_value in value.items():
                nested_is_artwork = is_artwork_value or any(
                    hint in str(key).lower() for hint in _ARTWORK_URL_KEY_HINTS
                )
                yield nested_value, nested_is_artwork
                yield from cls.__walk_media_items(nested_value, nested_is_artwork)
        elif isinstance(value, list):
            for nested_value in value:
                yield from cls.__walk_media_items(nested_value, is_artwork_value)

    @staticmethod
    def __normalize_artwork_url(url: Any) -> str | None:
        if not isinstance(url, str):
            return None

        normalized = url.strip()
        if not normalized:
            return None
        if normalized.startswith("//"):
            normalized = f"https:{normalized}"

        if not normalized.startswith(("http://", "https://")):
            return None

        return re.sub(
            r"/(\d+x\d+bb)\.(jpg|jpeg|png|webp)(\?.*)?$",
            r"/600x600bb.\2\3",
            normalized,
            flags=re.IGNORECASE,
        )

    async def async_request_execute_payload(
        self,
        href: str,
        required_keys: str | Iterable[str],
        initial_sleep: float = 0.1,
        retry_sleep: float = 0.2,
        max_retries: int = 10,
    ) -> dict[str, Any] | None:
        """Request an execute href and poll until all required keys are present.

        Returns the payload dict, or None if the required keys do not appear within
        max_retries polling attempts.  Logs unexpected (non-Samsung) keys at debug
        level so new properties can be discovered without crashing.
        """
        keys = (
            (required_keys,)
            if isinstance(required_keys, str)
            else tuple(required_keys)
        )

        if href in self.__unsupported_execute_payload_hrefs:
            log.debug(
                "[%s] async_request_execute_payload: skipping unsupported hidden "
                "payload href %r",
                DOMAIN,
                href,
            )
            return None

        await self.update_execution_data([href])
        await asyncio.sleep(initial_sleep)
        payload = await self.get_execute_status()
        retry = 0
        missing_keys = self.__missing_payload_keys(payload, keys)
        while missing_keys and retry < max_retries:
            await asyncio.sleep(retry_sleep)
            payload = await self.get_execute_status()
            missing_keys = self.__missing_payload_keys(payload, keys)
            retry += 1
        if missing_keys:
            self.__unsupported_execute_payload_hrefs.add(href)
            log.debug(
                "[%s] async_request_execute_payload: hidden payload href %r did "
                "not expose required keys %s after %d retries; payload keys: %s",
                DOMAIN,
                href,
                missing_keys,
                max_retries,
                sorted(payload),
            )
            return None
        unknown = [
            k for k in payload
            if not k.startswith("x.com.samsung.networkaudio.")
        ]
        if unknown:
            log.debug(
                "[%s] async_request_execute_payload: unexpected keys in payload "
                "for href %r: %s",
                DOMAIN,
                href,
                unknown,
            )
        return payload

    @staticmethod
    def __missing_payload_keys(
        payload: dict[str, Any],
        keys: Iterable[str],
    ) -> list[str]:
        return [key for key in keys if key not in payload]

    async def _update_soundmode(self):
        payload = await self.async_request_execute_payload(
            "/sec/networkaudio/soundmode",
            (
                "x.com.samsung.networkaudio.supportedSoundmode",
                "x.com.samsung.networkaudio.soundmode",
            ),
            initial_sleep=1,
            retry_sleep=1,
        )
        if payload is None:
            return
        self.__supported_soundmodes = payload[
            "x.com.samsung.networkaudio.supportedSoundmode"
        ]
        self.__active_soundmode = payload["x.com.samsung.networkaudio.soundmode"]
        self.__soundmode_supported = True

    async def _update_woofer(self):
        payload = await self.async_request_execute_payload(
            "/sec/networkaudio/woofer",
            (
                "x.com.samsung.networkaudio.woofer",
                "x.com.samsung.networkaudio.connection",
            ),
        )
        if payload is None:
            return
        self.__woofer_level = payload["x.com.samsung.networkaudio.woofer"]
        self.__woofer_connection = payload["x.com.samsung.networkaudio.connection"]
        self.__woofer_supported = True

    async def _update_equalizer(self):
        payload = await self.async_request_execute_payload(
            "/sec/networkaudio/eq",
            (
                "x.com.samsung.networkaudio.EQname",
                "x.com.samsung.networkaudio.supportedList",
                "x.com.samsung.networkaudio.action",
                "x.com.samsung.networkaudio.EQband",
            ),
        )
        if payload is None:
            return
        self.__active_eq_preset = payload["x.com.samsung.networkaudio.EQname"]
        self.__supported_eq_presets = payload[
            "x.com.samsung.networkaudio.supportedList"
        ]
        self.__eq_action = payload["x.com.samsung.networkaudio.action"]
        self.__eq_bands = payload["x.com.samsung.networkaudio.EQband"]
        self.__equalizer_supported = True

    async def _update_advanced_audio(self):
        payload = await self.async_request_execute_payload(
            "/sec/networkaudio/advancedaudio",
            (
                "x.com.samsung.networkaudio.nightmode",
                "x.com.samsung.networkaudio.bassboost",
                "x.com.samsung.networkaudio.voiceamplifier",
            ),
        )
        if payload is None:
            return
        self.__night_mode = payload["x.com.samsung.networkaudio.nightmode"]
        self.__bass_mode = payload["x.com.samsung.networkaudio.bassboost"]
        self.__voice_amplifier = payload["x.com.samsung.networkaudio.voiceamplifier"]
        self.__advanced_audio_supported = True

    @property
    def status(self):
        return self.device.status

    # ------------ DEVICE INFORMATION ----------

    @property
    def manufacturer(self):
        return self.device.status.ocf_manufacturer_name

    @property
    def model(self):
        return self.device.status.ocf_model_number

    @property
    def firmware_version(self):
        return self.device.status.ocf_firmware_version

    @property
    def device_id(self):
        return self.device.device_id

    @property
    def device_name(self):
        return self.__device_name

    def has_status_capability(self, capability: str) -> bool:
        has_capability = getattr(self.device.status, "has_capability", None)
        if callable(has_capability):
            return bool(has_capability(capability))
        return bool(self.device.status.attributes.get(capability))

    @property
    def can_turn_on_off(self) -> bool:
        return self.hybrid_mode or self.has_status_capability("switch")

    @property
    def can_control_volume(self) -> bool:
        return self.hybrid_mode or self.has_status_capability("audioVolume")

    @property
    def can_mute_volume(self) -> bool:
        return self.hybrid_mode or self.has_status_capability("audioMute")

    @property
    def can_control_playback(self) -> bool:
        return self.has_status_capability("mediaPlayback")

    @property
    def can_select_sound_mode(self) -> bool:
        if self.hybrid_mode:
            return True
        return bool(
            self.__enable_soundmode
            and self.__soundmode_supported
            and self.supported_soundmodes
        )

    @property
    def can_select_equalizer_preset(self) -> bool:
        return bool(
            self.__enable_eq
            and self.__equalizer_supported
            and self.supported_equalizer_presets
        )

    @property
    def can_control_woofer_level(self) -> bool:
        return bool(self.__enable_woofer and self.__woofer_supported)

    @property
    def can_control_advanced_audio(self) -> bool:
        return self.__enable_advanced_audio

    @property
    def has_advanced_audio_state(self) -> bool:
        return self.__advanced_audio_supported

    # ------------ ON / OFF ------------

    @property
    def state(self) -> str:
        local_power = self.__local_value("power")
        if local_power is not None:
            if local_power == "powerOff":
                return "off"
            if self.device.status.playback_status == "playing":
                return "playing"
            if self.device.status.playback_status == "paused":
                return "paused"
            return "on"

        if self.device.status.switch:
            if self.device.status.playback_status == "playing":
                return "playing"
            if self.device.status.playback_status == "paused":
                return "paused"
            else:
                return "on"
        else:
            return "off"

    async def switch_off(self):
        if await self.__try_local_rpc(lambda local: local.power_off(), "switch off"):
            return

        await self.__call_smartthings(
            lambda: self.device.switch_off(True),
            "switch off",
        )

    async def switch_on(self):
        if await self.__try_local_rpc(lambda local: local.power_on(), "switch on"):
            return

        await self.__call_smartthings(
            lambda: self.device.switch_on(True),
            "switch on",
        )

    # ------------ VOLUME --------------

    @property
    def volume_level(self) -> float:
        local_volume = self.__local_value("volume")
        if local_volume is not None:
            if local_volume > self.__max_volume:
                return 1.0
            return local_volume / self.__max_volume

        vol = self.device.status.volume
        if vol > self.__max_volume:
            return 1.0
        return self.device.status.volume / self.__max_volume

    @property
    def volume_muted(self) -> bool:
        local_mute = self.__local_value("mute")
        if local_mute is not None:
            return bool(local_mute)

        if self.__optimistic_mute is not None:
            return self.__optimistic_mute
        return self.device.status.mute

    async def set_volume(self, volume: float):
        """
        Sets the volume to a certain level.
        This respects the max volume and hovers between
        :param volume: between 0 and 1
        """
        local_level = int(volume * self.__max_volume)
        if await self.__try_local_rpc(
            lambda local: local.set_volume(local_level),
            "set volume",
        ):
            return

        await self.__call_smartthings(
            lambda: self.device.set_volume(int(volume * self.__max_volume), True),
            "set volume",
        )

    async def mute_volume(self, mute: bool):
        if self.hybrid_mode:
            if mute == self.volume_muted:
                return
            if await self.__try_local_rpc(
                lambda local: local.mute_toggle(),
                "mute volume",
            ):
                self.__local_status["mute"] = mute
                return

        if mute:
            await self.__call_smartthings(
                lambda: self.device.mute(True),
                "mute volume",
            )
        else:
            await self.__call_smartthings(
                lambda: self.device.unmute(True),
                "unmute volume",
            )
        self.__set_optimistic_mute(mute)

    def __set_optimistic_mute(self, mute: bool) -> None:
        self.__optimistic_mute = mute
        self.__optimistic_mute_updated_at = datetime.datetime.now()

    def __sync_optimistic_mute(self) -> None:
        if self.__optimistic_mute is None:
            return

        if self.device.status.mute == self.__optimistic_mute:
            self.__optimistic_mute = None
            self.__optimistic_mute_updated_at = None
            return

        if (
            self.__optimistic_mute_updated_at is not None
            and datetime.datetime.now() - self.__optimistic_mute_updated_at
            > _OPTIMISTIC_MUTE_TIMEOUT
        ):
            self.__optimistic_mute = None
            self.__optimistic_mute_updated_at = None

    async def volume_up(self):
        if await self.__try_local_rpc(lambda local: local.volume_up(), "volume up"):
            return

        await self.__call_smartthings(
            lambda: self.device.volume_up(True),
            "volume up",
        )

    async def volume_down(self):
        if await self.__try_local_rpc(lambda local: local.volume_down(), "volume down"):
            return

        await self.__call_smartthings(
            lambda: self.device.volume_down(True),
            "volume down",
        )

    # ------------ WOOFER LEVEL -------------

    @property
    def woofer_level(self) -> int:
        return self.__woofer_level

    @property
    def woofer_connection(self) -> str:
        return self.__woofer_connection

    async def set_woofer(self, level: int):
        await self.set_custom_execution_data(
            href="/sec/networkaudio/woofer",
            property="x.com.samsung.networkaudio.woofer",
            value=level,
        )
        self.__woofer_level = level

    # ------------ INPUT SOURCE -------------

    @property
    def input_source(self):
        local_source = self.__local_value("input_source")
        if local_source is not None:
            return self.__ha_source_from_local(local_source)

        if self.media_app_name in ("AirPlay", "Spotify"):
            return "WIFI"
        return self.device.status.input_source

    @property
    def sound_from_detail_name(self) -> str | None:
        return self.device.status.sound_from_detail_name

    @property
    def sound_from_mode(self) -> int | None:
        return self.device.status.sound_from_mode

    @property
    def supported_input_sources(self):
        if self.hybrid_mode:
            sources = list(self.device.status.supported_input_sources or [])
            if not sources:
                sources = [
                    self.__ha_source_from_local(source)
                    for source in ("D_IN", "HDMI_IN1", "BT", "E_ARC", "WIFI_IDLE")
                ]
        else:
            sources = list(self.device.status.supported_input_sources or [])

        sources = [source for source in sources if source]
        current = self.input_source
        if current and current not in sources:
            sources.append(current)
        return list(dict.fromkeys(sources))

    @property
    def can_select_source(self) -> bool:
        if self.hybrid_mode:
            return True
        return self.__can_select_source_cloud()

    def __can_select_source_cloud(self) -> bool:
        can_set_input_source = getattr(self.device, "can_set_input_source", None)
        if can_set_input_source is not None:
            return bool(can_set_input_source)

        status = getattr(self.device, "status", None)
        has_capability = getattr(status, "has_capability", None)
        if callable(has_capability):
            return bool(
                has_capability("mediaInputSource")
                or has_capability("samsungvd.mediaInputSource")
            )

        return bool(self.supported_input_sources)

    async def select_source(self, source: str):
        if self.hybrid_mode:
            local_source = self.__local_source_from_ha(source)
            if await self.__try_local_rpc(
                lambda local: local.select_input(local_source),
                "select input source",
            ):
                self.__local_status["input_source"] = local_source
                return

            if not self.__can_select_source_cloud():
                raise HomeAssistantError(
                    "Local input source control is unavailable and "
                    "SmartThings input source is read-only for this soundbar"
                )

        if not self.__can_select_source_cloud():
            raise HomeAssistantError(
                "Input source is read-only for this soundbar in the SmartThings API"
            )

        await self.__call_smartthings(
            lambda: self.device.set_input_source(source, True),
            "select input source",
        )

    # ------------- SOUND MODE --------------
    @property
    def sound_mode(self):
        local_sound_mode = self.__local_value("sound_mode")
        if local_sound_mode is not None:
            return self.__ha_sound_mode_from_local(local_sound_mode)
        return self.__active_soundmode

    @property
    def supported_soundmodes(self):
        if self.hybrid_mode:
            current = self.sound_mode
            sound_modes = [
                self.__ha_sound_mode_from_local(sound_mode)
                for sound_mode in LOCAL_SOUND_MODE_VALUES
            ]
            sound_modes = [sound_mode for sound_mode in sound_modes if sound_mode]
            if current and current not in sound_modes:
                sound_modes.append(current)
            return list(dict.fromkeys(sound_modes))
        return self.__supported_soundmodes

    async def select_sound_mode(self, sound_mode: str):
        if self.hybrid_mode:
            local_sound_mode = self.__local_sound_mode_from_ha(sound_mode)
            if await self.__try_local_rpc(
                lambda local: local.set_sound_mode(local_sound_mode),
                "select sound mode",
            ):
                self.__local_status["sound_mode"] = local_sound_mode
                return

        await self.set_custom_execution_data(
            href="/sec/networkaudio/soundmode",
            property="x.com.samsung.networkaudio.soundmode",
            value=sound_mode,
        )

    # ------------- ADVANCED AUDIO ---------------

    @property
    def night_mode(self) -> bool:
        return True if self.__night_mode == 1 else False

    async def set_night_mode(self, value: bool):
        await self.set_custom_execution_data(
            href="/sec/networkaudio/advancedaudio",
            property="x.com.samsung.networkaudio.nightmode",
            value=1 if value else 0,
        )
        self.__night_mode = 1 if value else 0

    @property
    def bass_mode(self) -> bool:
        return True if self.__bass_mode == 1 else False

    async def set_bass_mode(self, value: bool):
        await self.set_custom_execution_data(
            href="/sec/networkaudio/advancedaudio",
            property="x.com.samsung.networkaudio.bassboost",
            value=1 if value else 0,
        )
        self.__bass_mode = 1 if value else 0

    @property
    def voice_amplifier(self) -> bool:
        return True if self.__voice_amplifier == 1 else False

    async def set_voice_amplifier(self, value: bool):
        await self.set_custom_execution_data(
            href="/sec/networkaudio/advancedaudio",
            property="x.com.samsung.networkaudio.voiceamplifier",
            value=1 if value else 0,
        )
        self.__voice_amplifier = 1 if value else 0

    # ------------ EQUALIZER --------------

    @property
    def active_equalizer_preset(self):
        return self.__active_eq_preset

    @property
    def supported_equalizer_presets(self):
        return self.__supported_eq_presets

    @property
    def equalizer_action(self):
        return self.__eq_action

    @property
    def equalizer_bands(self):
        return self.__eq_bands

    async def set_equalizer_preset(self, preset: str):
        await self.set_custom_execution_data(
            href="/sec/networkaudio/eq",
            property="x.com.samsung.networkaudio.EQname",
            value=preset,
        )

    # ------------- MEDIA ----------------
    @property
    def media_title(self):
        return self.__media_title

    @property
    def media_artist(self):
        return self.__media_artist

    @property
    def media_coverart_url(self) -> str | None:
        return self.__media_cover_url

    @property
    def media_coverart_hash(self) -> str | None:
        if self.__media_cover_url is None:
            return None
        if self.__media_cover_url_update_time is None:
            return self.__media_cover_url
        return (
            f"{self.__media_cover_url}:"
            f"{self.__media_cover_url_update_time.timestamp()}"
        )

    @property
    def media_duration(self) -> int | None:
        attr = self.device.status.attributes.get("totalTime", None)
        if attr:
            return attr.value

    @property
    def media_position(self) -> int | None:
        attr = self.device.status.attributes.get("elapsedTime", None)
        if attr:
            return attr.value

    async def media_play(self):
        await self.__call_smartthings(
            lambda: self.device.play(True),
            "media play",
        )

    async def media_pause(self):
        await self.__call_smartthings(
            lambda: self.device.pause(True),
            "media pause",
        )

    async def media_stop(self):
        await self.__call_smartthings(
            lambda: self.device.stop(True),
            "media stop",
        )

    async def media_next_track(self):
        await self.__call_smartthings(
            lambda: self.device.command("main", "mediaPlayback", "fastForward"),
            "media next track",
        )

    async def media_previous_track(self):
        await self.__call_smartthings(
            lambda: self.device.command("main", "mediaPlayback", "rewind"),
            "media previous track",
        )

    @property
    def media_app_name(self):
        detail_status = self.device.status.attributes.get("detailName", None)
        if detail_status is not None:
            return detail_status.value
        return None

    @property
    def media_coverart_updated(self) -> datetime.datetime | None:
        return self.__media_cover_url_update_time

    # ------------ Speaker Level ----------------

    async def set_speaker_level(self, speaker: SpeakerIdentifier, level: int):
        await self.set_custom_execution_data(
            href="/sec/networkaudio/channelVolume",
            property="x.com.samsung.networkaudio.channelVolume",
            value=[{"name": speaker.value, "value": level}],
        )

    async def set_rear_speaker_mode(self, mode: RearSpeakerMode):
        await self.set_custom_execution_data(
            href="/sec/networkaudio/surroundspeaker",
            property="x.com.samsung.networkaudio.currentRearPosition",
            value=mode.value,
        )

    # ------------ OTHER FUNCTIONS ------------

    async def set_active_voice_amplifier(self, enabled: bool):
        await self.set_custom_execution_data(
            href="/sec/networkaudio/activeVoiceAmplifier",
            property="x.com.samsung.networkaudio.activeVoiceAmplifier",
            value=1 if enabled else 0
        )

    async def set_space_fit_sound(self, enabled: bool):
        await self.set_custom_execution_data(
            href="/sec/networkaudio/spacefitSound",
            property="x.com.samsung.networkaudio.spacefitSound",
            value=1 if enabled else 0
        )

    # ------------ SUPPORT FUNCTIONS ------------

    async def update_execution_data(self, argument: str):
        return await self.__call_smartthings(
            lambda: self.device.command("main", "execute", "execute", argument),
            "update execution data",
        )

    async def set_custom_execution_data(self, href: str, property: str, value):
        argument = [href, {property: value}]
        await self.__call_smartthings(
            lambda: self.device.command("main", "execute", "execute", argument),
            "set custom execution data",
        )

    async def async_dump_execute_payload(
        self,
        hrefs: Iterable[str],
        sleep_time: float = 0.3,
        max_retries: int = 10,
        write_probe: tuple[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Request execute hrefs and return raw payloads for diagnostics."""
        payloads: dict[str, Any] = {}
        errors: dict[str, str] = {}
        command_results: dict[str, Any] = {}
        write_probe_results: dict[str, Any] = {}
        write_probe_raw_statuses: dict[str, Any] = {}
        raw_statuses: dict[str, Any] = {}
        raw_device_statuses: dict[str, Any] = {}

        for href in hrefs:
            try:
                command_result = await self.__post_execute_command_raw([href])
                command_results[href] = command_result
                if command_result["status"] >= 400:
                    errors[href] = self.__command_error_message(command_result)
                    continue
                payload: dict[str, Any] = {}
                for _ in range(max_retries + 1):
                    await asyncio.sleep(sleep_time)
                    status_data = await self.get_execute_status_raw()
                    raw_statuses[href] = status_data
                    payload = self.__extract_execute_payload(status_data)
                    if payload:
                        break
            except (ConfigEntryAuthFailed, ConfigEntryNotReady):
                raise
            except Exception as err:  # noqa: BLE001 - diagnostic dump should continue
                log.exception(
                    "[%s] Failed to dump execute payload for device %s href %s",
                    DOMAIN,
                    self.device_name,
                    href,
                )
                errors[href] = str(err)
                continue

            if not payload:
                device_status = await self.get_device_status_raw()
                raw_device_statuses[href] = device_status
                payload = self.__extract_execute_payload_from_device_status(
                    device_status,
                    href,
                )
                if not payload:
                    errors[href] = (
                        "execute status did not return payload "
                        f"after {max_retries + 1} polling attempts"
                    )
                    await self.__run_execute_write_probe(
                        href,
                        write_probe,
                        write_probe_results,
                        write_probe_raw_statuses,
                        sleep_time,
                    )
                    continue

            payloads[href] = payload
            log.info(
                "[%s] Execute payload for device %s href %s: %s",
                DOMAIN,
                self.device_name,
                href,
                payload,
            )

            await self.__run_execute_write_probe(
                href,
                write_probe,
                write_probe_results,
                write_probe_raw_statuses,
                sleep_time,
            )

        self.__last_execute_payload_dump = {
            "device_id": self.device_id,
            "device_name": self.device_name,
            "payloads": payloads,
            "errors": errors,
            "command_results": command_results,
            "write_probe": (
                None
                if write_probe is None
                else {"property": write_probe[0], "value": write_probe[1]}
            ),
            "write_probe_results": write_probe_results,
            "write_probe_raw_statuses": write_probe_raw_statuses,
            "q800f_ui_status": self.__q800f_ui_status_summary(),
            "raw_statuses": raw_statuses,
            "raw_device_statuses": raw_device_statuses,
            "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        return self.__last_execute_payload_dump

    async def __run_execute_write_probe(
        self,
        href: str,
        write_probe: tuple[str, Any] | None,
        write_probe_results: dict[str, Any],
        write_probe_raw_statuses: dict[str, Any],
        sleep_time: float,
    ) -> None:
        if write_probe is None:
            return

        write_property, write_value = write_probe
        write_probe_results[href] = await self.__post_execute_command_raw(
            [href, {write_property: write_value}],
        )
        await asyncio.sleep(sleep_time)
        write_probe_raw_statuses[href] = await self.get_execute_status_raw()

    def __q800f_ui_status_summary(self) -> dict[str, Any]:
        status = self.device.status
        return {
            "source": {
                "value": status.input_source,
                "supported_sources": status.supported_input_sources,
                "writable": getattr(self.device, "can_set_input_source", False),
            },
            "volume": {"value": status.volume, "mute": status.mute},
            "sound_from": {
                "detail_name": status.sound_from_detail_name,
                "mode": status.sound_from_mode,
            },
            "sound_mode": self.__status_capability_summary(
                "samsungvd.audioSoundMode",
                ("soundMode", "supportedSoundModes"),
            ),
            "execute": self.__status_capability_summary("execute", ("data",)),
        }

    def __status_capability_summary(
        self,
        capability: str,
        attributes: tuple[str, ...],
    ) -> dict[str, Any]:
        return {
            attribute: self.device.status._value(capability, attribute)
            for attribute in attributes
        }

    @property
    def last_execute_payload_dump(self) -> dict[str, Any] | None:
        return self.__last_execute_payload_dump

    async def async_dump_status_summary(
        self,
        include_null: bool = False,
    ) -> dict[str, Any]:
        """Return a compact summary of SmartThings status capabilities."""
        status_data = await self.get_device_status_raw()
        components = status_data.get("components")
        summary: dict[str, dict[str, dict[str, Any]]] = {}
        non_null_paths: list[str] = []

        if isinstance(components, dict):
            for component, capabilities in components.items():
                component_summary = self.__summarize_component_status(
                    component,
                    capabilities,
                    include_null=include_null,
                    non_null_paths=non_null_paths,
                )
                if component_summary:
                    summary[component] = component_summary

        return {
            "device_id": self.device_id,
            "device_name": self.device_name,
            "include_null": include_null,
            "components": summary,
            "non_null_paths": non_null_paths,
            "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }

    @staticmethod
    def __summarize_component_status(
        component: str,
        capabilities: Any,
        include_null: bool,
        non_null_paths: list[str],
    ) -> dict[str, dict[str, Any]]:
        if not isinstance(capabilities, dict):
            return {}

        component_summary: dict[str, dict[str, Any]] = {}
        for capability, attributes in capabilities.items():
            if not isinstance(attributes, dict):
                continue

            attribute_summary: dict[str, Any] = {}
            for attribute, status in attributes.items():
                value = status.get("value") if isinstance(status, dict) else status
                if value is None and not include_null:
                    continue

                attribute_summary[attribute] = SoundbarDevice.__summarize_status(
                    status,
                    value,
                )
                if value is not None:
                    non_null_paths.append(f"{component}.{capability}.{attribute}")

            if attribute_summary:
                component_summary[capability] = attribute_summary

        return component_summary

    @staticmethod
    def __summarize_status(status: Any, value: Any) -> dict[str, Any]:
        if not isinstance(status, dict):
            return {"value": value}

        result = {"value": value}
        for key in ("unit", "timestamp"):
            if key in status:
                result[key] = status[key]
        return result

    async def __post_execute_command_raw(
        self,
        arguments: list[Any],
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        url = f"https://api.smartthings.com/v1/devices/{self._device_id}/commands"
        request_body = {
            "commands": [
                {
                    "component": "main",
                    "capability": "execute",
                    "command": "execute",
                    "arguments": arguments,
                }
            ]
        }

        resp = await self.__session.post(
            url,
            headers=await self.__auth_headers(force_refresh=force_refresh),
            json=request_body,
        )

        if resp.status in _AUTH_ERROR_STATUSES and self.__auth_provider is not None:
            resp.release()
            return await self.__post_execute_command_raw(arguments, force_refresh=True)

        if resp.status in _AUTH_ERROR_STATUSES:
            resp.release()
            raise ConfigEntryAuthFailed(
                "SmartThings authorization is no longer valid"
            )

        if resp.status in _TRANSIENT_ERROR_STATUSES:
            status = resp.status
            body = await resp.text()
            resp.release()
            raise ConfigEntryNotReady(
                f"SmartThings service returned transient status {status}: {body}"
            )

        response_body = await self.__response_body(resp)
        return {
            "status": resp.status,
            "request": request_body,
            "response": response_body,
        }

    async def get_execute_status(self):
        status_data = await self.get_execute_status_raw()
        return self.__extract_execute_payload(status_data)

    async def get_execute_status_raw(self) -> dict[str, Any]:
        url = (
            "https://api.smartthings.com/v1/devices/"
            f"{self._device_id}/components/main/capabilities/execute/status"
        )
        resp = await self.__get_execute_status_response(url)

        if resp.status in _AUTH_ERROR_STATUSES and self.__auth_provider is not None:
            resp.release()
            resp = await self.__get_execute_status_response(url, force_refresh=True)

        if resp.status in _AUTH_ERROR_STATUSES:
            resp.release()
            raise ConfigEntryAuthFailed(
                "SmartThings authorization is no longer valid"
            )

        if resp.status in _TRANSIENT_ERROR_STATUSES:
            status = resp.status
            resp.release()
            raise ConfigEntryNotReady(
                f"SmartThings service returned transient status {status}"
            )

        try:
            resp.raise_for_status()
        except ClientResponseError as err:
            self.__raise_for_http_status(err)
        status_data = await resp.json(content_type=None)
        if isinstance(status_data, dict):
            return status_data
        return {"raw": status_data}

    async def get_device_status_raw(self) -> dict[str, Any]:
        url = f"https://api.smartthings.com/v1/devices/{self._device_id}/status"
        resp = await self.__get_status_response(url)
        status_data = await resp.json(content_type=None)
        if isinstance(status_data, dict):
            return status_data
        return {"raw": status_data}

    @staticmethod
    def __extract_execute_payload(status_data: dict[str, Any]) -> dict[str, Any]:
        data = SoundbarDevice.__maybe_json(status_data.get("data"))
        value = (
            SoundbarDevice.__maybe_json(data.get("value"))
            if isinstance(data, dict)
            else None
        )

        candidates = (
            status_data.get("payload"),
            data.get("payload") if isinstance(data, dict) else None,
            value.get("payload") if isinstance(value, dict) else None,
            value,
            data,
        )
        for candidate in candidates:
            candidate = SoundbarDevice.__maybe_json(candidate)
            if not isinstance(candidate, dict):
                continue
            if "payload" in candidate:
                nested_payload = SoundbarDevice.__maybe_json(candidate["payload"])
                if isinstance(nested_payload, dict):
                    return nested_payload
            if any(key.startswith("x.com.samsung.networkaudio.") for key in candidate):
                return candidate

        return {}

    @staticmethod
    def __extract_execute_payload_from_device_status(
        status_data: dict[str, Any],
        href: str,
    ) -> dict[str, Any]:
        for candidate in SoundbarDevice.__walk_dicts(status_data):
            data = SoundbarDevice.__maybe_json(candidate.get("data"))
            if not isinstance(data, dict) or data.get("href") != href:
                continue
            payload = SoundbarDevice.__extract_execute_payload(candidate)
            if payload:
                return payload
        return {}

    @staticmethod
    def __walk_dicts(value: Any):
        if isinstance(value, dict):
            yield value
            for nested_value in value.values():
                yield from SoundbarDevice.__walk_dicts(nested_value)
        elif isinstance(value, list):
            for nested_value in value:
                yield from SoundbarDevice.__walk_dicts(nested_value)

    @staticmethod
    def __maybe_json(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value

    @staticmethod
    def __json_safe(value: Any) -> Any:
        return json.loads(json.dumps(value, default=str))

    @staticmethod
    def __raise_for_http_status(err: ClientResponseError) -> None:
        """Map SmartThings HTTP errors to Home Assistant config-entry errors."""
        status = err.status
        if status in _AUTH_ERROR_STATUSES:
            raise ConfigEntryAuthFailed(
                "SmartThings authorization is no longer valid"
            ) from err
        if status in _TRANSIENT_ERROR_STATUSES:
            raise ConfigEntryNotReady(
                f"SmartThings service returned transient status {err.status}"
            ) from err
        raise err

    async def __get_execute_status_response(
        self, url: str, force_refresh: bool = False
    ):
        return await self.__get_status_response(url, force_refresh=force_refresh)

    async def __get_status_response(self, url: str, force_refresh: bool = False):
        request_headers = await self.__auth_headers(force_refresh=force_refresh)
        resp = await self.__session.get(url, headers=request_headers)

        if resp.status in _AUTH_ERROR_STATUSES and self.__auth_provider is not None:
            resp.release()
            return await self.__get_status_response(url, force_refresh=True)

        if resp.status in _AUTH_ERROR_STATUSES:
            resp.release()
            raise ConfigEntryAuthFailed(
                "SmartThings authorization is no longer valid"
            )

        if resp.status in _TRANSIENT_ERROR_STATUSES:
            status = resp.status
            resp.release()
            raise ConfigEntryNotReady(
                f"SmartThings service returned transient status {status}"
            )

        try:
            resp.raise_for_status()
        except ClientResponseError as err:
            self.__raise_for_http_status(err)
        return resp

    async def __auth_headers(self, force_refresh: bool = False) -> dict[str, str]:
        if self.__auth_provider is not None:
            api_key = await self.__auth_provider.async_get_access_token(
                force_refresh=force_refresh
            )
        else:
            api_key = self.device._api.token

        return {"Authorization": "Bearer " + api_key}

    @staticmethod
    async def __response_body(resp) -> Any:
        body_text = await resp.text()
        if not body_text:
            return None
        try:
            return json.loads(body_text)
        except json.JSONDecodeError:
            return body_text

    @staticmethod
    def __command_error_message(command_result: dict[str, Any]) -> str:
        response = command_result.get("response")
        if isinstance(response, dict):
            error = response.get("error")
            if isinstance(error, dict):
                code = error.get("code", "SmartThingsCommandError")
                message = error.get("message", "Command failed")
                return f"{code}: {message}"
        return f"SmartThings command failed with HTTP {command_result['status']}"

    async def get_song_title_artwork(
        self,
        artist: str,
        title: str,
        album: str = "",
    ) -> str | None:
        """
        This function loads a Music Art Cover from iTunes based on
        the title and the artist
        :param artist: string
        :param title: string
        :return: url as string
        """
        query_term = " ".join(value for value in (artist, title, album) if value)
        if not query_term:
            return None

        if artwork_url := await self.__get_itunes_artwork(query_term):
            return artwork_url
        return await self.__get_deezer_artwork(query_term)

    async def __get_itunes_artwork(self, query_term: str) -> str | None:
        try:
            async with self.__session.get(
                "https://itunes.apple.com/search",
                params={
                    "term": query_term,
                    "media": "music",
                    "entity": "musicTrack",
                    "limit": 1,
                },
            ) as resp:
                if resp.status != 200:
                    log.debug(
                        "[%s] iTunes artwork lookup failed with status %s",
                        DOMAIN,
                        resp.status,
                    )
                    return None
                resp_dict = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001 - artwork lookup must not break updates
            log.debug("[%s] iTunes artwork lookup failed: %s", DOMAIN, exc)
            return None

        results = resp_dict.get("results", [])
        if not results:
            return None
        return self.__normalize_artwork_url(results[0].get("artworkUrl100"))

    async def __get_deezer_artwork(self, query_term: str) -> str | None:
        try:
            async with self.__session.get(
                "https://api.deezer.com/search",
                params={"q": query_term, "limit": 1},
            ) as resp:
                if resp.status != 200:
                    log.debug(
                        "[%s] Deezer artwork lookup failed with status %s",
                        DOMAIN,
                        resp.status,
                    )
                    return None
                resp_dict = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001 - artwork lookup must not break updates
            log.debug("[%s] Deezer artwork lookup failed: %s", DOMAIN, exc)
            return None

        results = resp_dict.get("data", [])
        if not results:
            return None
        album = results[0].get("album")
        if not isinstance(album, dict):
            return None
        return self.__normalize_artwork_url(
            album.get("cover_xl")
            or album.get("cover_big")
            or album.get("cover_medium")
            or album.get("cover")
        )

    @property
    def retrieve_data(self):
        return {
            "status": self.state,
            "device_information": {
                "model": self.model,
                "manufacture": self.manufacturer,
                "firmware_version": self.firmware_version,
                "device_id": self.device_id,
            },
            "volume": {"level": self.volume_level, "muted": self.volume_muted},
            "woofer": {
                "level": self.woofer_level,
                "connection": self.woofer_connection,
            },
            "source": {
                "active_source": self.input_source,
                "supported_sources": self.supported_input_sources,
            },
            "sound_mode": {
                "active_sound_mode": self.sound_mode,
                "supported_sound_modes": self.supported_soundmodes,
            },
            "advanced_audio": {
                "night_mode": self.night_mode,
                "bass_mode": self.bass_mode,
                "voice_amplifier": self.voice_amplifier,
            },
            "equalizer": {
                "active_preset": self.active_equalizer_preset,
                "supported_presets": self.supported_equalizer_presets,
                "action": self.equalizer_action,
                "bands": self.equalizer_bands,
            },
            "media": {
                "media_title": self.media_title,
                "media_artist": self.media_artist,
                "media_cover_url": self.media_coverart_url,
                "media_duration": self.media_duration,
                "media_position": self.media_position,
            },
        }
