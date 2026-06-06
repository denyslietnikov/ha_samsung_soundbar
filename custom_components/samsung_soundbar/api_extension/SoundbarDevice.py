import asyncio
from collections.abc import Awaitable, Callable, Iterable
import datetime
import json
import logging
import re
from typing import Any, TypeVar

from aiohttp import ClientResponseError
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from pysmartthings.exceptions import (
    SmartThingsAuthenticationFailedError,
    SmartThingsConnectionError,
    SmartThingsForbiddenError,
)

from .const import SpeakerIdentifier, RearSpeakerMode
from ..const import DOMAIN

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
    ):
        self.device = device
        self._device_id = self.device.device_id
        self.__auth_provider = auth_provider
        self.__session = session
        self.__device_name = device_name

        self.__enable_soundmode = enable_soundmode
        self.__supported_soundmodes = []
        self.__active_soundmode = ""

        self.__enable_woofer = enable_woofer
        self.__woofer_level = 0
        self.__woofer_connection = ""

        self.__enable_eq = enable_eq
        self.__active_eq_preset = ""
        self.__supported_eq_presets = []
        self.__eq_action = ""
        self.__eq_bands = []

        self.__enable_advanced_audio = enable_advanced_audio
        self.__voice_amplifier = 0
        self.__night_mode = 0
        self.__bass_mode = 0

        self.__media_title = ""
        self.__media_artist = ""
        self.__media_cover_url: str | None = None
        self.__media_cover_url_update_time: datetime.datetime | None = None
        self.__old_media_key = ""
        self.__last_execute_payload_dump: dict[str, Any] | None = None

        self.__max_volume = max_volume

    async def update(self):
        if self.__auth_provider is not None:
            await self.__auth_provider.async_get_access_token()

        await self.__call_smartthings(
            self.device.status.refresh,
            "refresh device status",
        )

        await self._update_media()

        if self.__enable_soundmode:
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

    async def _update_media(self):
        audio_track_status = self.device.status._attributes.get("audioTrackData")
        audio_track_data = getattr(audio_track_status, "value", None)
        if not isinstance(audio_track_data, dict):
            self.__clear_media_metadata()
            return

        artist = self.__clean_media_value(audio_track_data.get("artist"))
        title = self.__clean_media_value(audio_track_data.get("title"))
        media_key = f"{artist}\0{title}"

        self.__media_artist = artist
        self.__media_title = title

        if not artist and not title:
            self.__clear_media_artwork(media_key)
            return

        if media_key == self.__old_media_key:
            return

        self.__old_media_key = media_key
        self.__media_cover_url_update_time = datetime.datetime.now()
        self.__media_cover_url = self.__extract_media_artwork_url(audio_track_data)
        if self.__media_cover_url is None:
            self.__media_cover_url = await self.get_song_title_artwork(artist, title)

    def __clear_media_metadata(self) -> None:
        self.__media_artist = ""
        self.__media_title = ""
        self.__clear_media_artwork("")

    def __clear_media_artwork(self, media_key: str) -> None:
        if self.__old_media_key == media_key and self.__media_cover_url is None:
            return
        self.__old_media_key = media_key
        self.__media_cover_url = None
        self.__media_cover_url_update_time = datetime.datetime.now()

    @staticmethod
    def __clean_media_value(value: Any) -> str:
        return str(value).strip() if value is not None else ""

    def __extract_media_artwork_url(
        self, audio_track_data: dict[str, Any]
    ) -> str | None:
        for key in (
            "albumArtUrl",
            "albumArtURI",
            "albumArtUri",
            "artworkUrl",
            "imageUrl",
            "thumbnailUrl",
            "coverArtUrl",
            "coverUrl",
        ):
            artwork_url = self.__normalize_artwork_url(audio_track_data.get(key))
            if artwork_url is not None:
                return artwork_url
        return None

    @staticmethod
    def __normalize_artwork_url(url: Any) -> str | None:
        if not isinstance(url, str):
            return None

        normalized = url.strip()
        if not normalized:
            return None
        if normalized.startswith("//"):
            normalized = f"https:{normalized}"
        elif normalized.startswith("http://"):
            normalized = f"https://{normalized.removeprefix('http://')}"

        if not normalized.startswith("https://"):
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
            log.error(
                "[%s] async_request_execute_payload: required keys %s not found "
                "in payload for href %r after %d retries; payload keys: %s",
                DOMAIN,
                missing_keys,
                href,
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

    # ------------ ON / OFF ------------

    @property
    def state(self) -> str:
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
        await self.__call_smartthings(
            lambda: self.device.switch_off(True),
            "switch off",
        )

    async def switch_on(self):
        await self.__call_smartthings(
            lambda: self.device.switch_on(True),
            "switch on",
        )

    # ------------ VOLUME --------------

    @property
    def volume_level(self) -> float:
        vol = self.device.status.volume
        if vol > self.__max_volume:
            return 1.0
        return self.device.status.volume / self.__max_volume

    @property
    def volume_muted(self) -> bool:
        return self.device.status.mute

    async def set_volume(self, volume: float):
        """
        Sets the volume to a certain level.
        This respects the max volume and hovers between
        :param volume: between 0 and 1
        """
        await self.__call_smartthings(
            lambda: self.device.set_volume(int(volume * self.__max_volume), True),
            "set volume",
        )

    async def mute_volume(self, mute: bool):
        if mute:
            await self.__call_smartthings(
                lambda: self.device.unmute(True),
                "unmute volume",
            )
        else:
            await self.__call_smartthings(
                lambda: self.device.mute(True),
                "mute volume",
            )

    async def volume_up(self):
        await self.__call_smartthings(
            lambda: self.device.volume_up(True),
            "volume up",
        )

    async def volume_down(self):
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
        if self.media_app_name in ("AirPlay", "Spotify"):
            return "wifi"
        return self.device.status.input_source

    @property
    def supported_input_sources(self):
        sources = list(self.device.status.supported_input_sources or [])
        current = self.input_source
        if current and current not in sources:
            sources.append(current)
        return sources

    async def select_source(self, source: str):
        await self.__call_smartthings(
            lambda: self.device.set_input_source(source, True),
            "select input source",
        )

    # ------------- SOUND MODE --------------
    @property
    def sound_mode(self):
        return self.__active_soundmode

    @property
    def supported_soundmodes(self):
        return self.__supported_soundmodes

    async def select_sound_mode(self, sound_mode: str):
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
    ) -> dict[str, Any]:
        """Request execute hrefs and return raw payloads for diagnostics."""
        payloads: dict[str, Any] = {}
        errors: dict[str, str] = {}
        command_results: dict[str, Any] = {}
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
                    continue

            payloads[href] = payload
            log.info(
                "[%s] Execute payload for device %s href %s: %s",
                DOMAIN,
                self.device_name,
                href,
                payload,
            )

        self.__last_execute_payload_dump = {
            "device_id": self.device_id,
            "device_name": self.device_name,
            "payloads": payloads,
            "errors": errors,
            "command_results": command_results,
            "raw_statuses": raw_statuses,
            "raw_device_statuses": raw_device_statuses,
            "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        return self.__last_execute_payload_dump

    @property
    def last_execute_payload_dump(self) -> dict[str, Any] | None:
        return self.__last_execute_payload_dump

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

    async def get_song_title_artwork(self, artist: str, title: str) -> str | None:
        """
        This function loads a Music Art Cover from iTunes based on
        the title and the artist
        :param artist: string
        :param title: string
        :return: url as string
        """
        query_term = " ".join(value for value in (artist, title) if value)
        if not query_term:
            return None

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
