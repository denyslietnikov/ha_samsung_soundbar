# ruff: noqa: N999
"""Local RPC core and verified OCF audio controls for Samsung soundbars."""

import asyncio
import datetime
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from ..const import (
    CONTROL_MODE_HYBRID_LOCAL_OCF,
    CONTROL_MODE_LOCAL_ONLY,
    CONTROL_MODES,
    DOMAIN,
)
from ..coordinator import coordinated_command
from ..local_ocf import OcfError
from ..local_rpc import (
    LOCAL_SOUND_MODE_VALUES,
    LocalRpcCommandError,
    LocalRpcError,
    LocalSoundbarRpcClient,
    split_local_readback,
)
from ..ocf_equalizer import OcfEqualizer
from ..ocf_profiles import (
    Q800F_AUDIO_SWITCHES,
    Q800F_TONE,
    Q800F_WOOFER,
    WOOFER_FIELD,
    WOOFER_HREF,
    validate_tone_level,
    validate_woofer_level,
)
from ..ocf_state import OCF_FIELDS

log = logging.getLogger(__name__)
_OPTIMISTIC_SOUND_MODE_TIMEOUT = datetime.timedelta(seconds=60)

_LOCAL_SOUND_MODE_CONFIRMATIONS = 2

_LOCAL_VOLUME_MAX = 100

_LOCAL_FAST_POLL_MIN_AGE = datetime.timedelta(seconds=1)

_LOCAL_STREAMING_SOURCE_CACHE_TIMEOUT = datetime.timedelta(seconds=15)

_LOCAL_STATUS_CACHE_TIMEOUT = datetime.timedelta(minutes=2)

_LOCAL_STREAMING_SOURCE_TO_DETAIL = {
    "AIRPLAY": "AirPlay",
    "CD": "Roon",
    "GOOGLE": "Google Cast",
    "ROON": "Roon",
    "SPOTIFY": "Spotify",
    "WIFI_AIRPLAY": "AirPlay",
    "WIFI_GOOGLE": "Google Cast",
    "WIFI_ROON": "Roon",
    "WIFI_SPOTIFY": "Spotify",
}

_LOCAL_SOURCE_TO_HA = {
    "HDMI_IN1": "HDMI1",
    "HDMI_IN2": "HDMI2",
    "ARC": "TV ARC/eARC",
    "EARC": "TV ARC/eARC",
    "E_ARC": "TV ARC/eARC",
    "TV_ARC": "TV ARC/eARC",
    "TV_EARC": "TV ARC/eARC",
    "TV_ARC_EARC": "TV ARC/eARC",
    "D_IN": "D.IN",
    "BT": "BT",
    "WIFI": "WIFI",
    "WIFI_AIRPLAY": "WIFI",
    "WIFI_GOOGLE": "WIFI",
    "WIFI_IDLE": "WIFI",
    "WIFI_ROON": "WIFI",
    "WIFI_SPOTIFY": "WIFI",
    "CD": "WIFI",
}

_HA_SOURCE_TO_LOCAL = {
    **{value: key for key, value in _LOCAL_SOURCE_TO_HA.items()},
    "HDMI1": "HDMI_IN1",
    "HDMI2": "HDMI_IN2",
    "TV ARC": "E_ARC",
    "TV eARC": "E_ARC",
    "TV ARC/eARC": "E_ARC",
    "ARC": "ARC",
    "E_ARC": "E_ARC",
    "EARC": "E_ARC",
    "D.IN": "D_IN",
    "D_IN": "D_IN",
    "WIFI": "WIFI_IDLE",
    "WIFI_IDLE": "WIFI_IDLE",
}

_LOCAL_SOUND_MODE_TO_HA = {
    "STANDARD": "Standard",
    "SURROUND": "Surround",
    "GAME": "Game Pro",
    "GAME_PRO": "Game Pro",
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
    "Game Pro": "GAME",
    "GAME PRO": "GAME",
    "Game": "GAME",
    "CLEAR VOICE": "CLEARVOICE",
    "DTS VIRTUAL X": "DTS_VIRTUAL_X",
}


class SoundbarDevice:
    def __init__(
        self,
        device,
        session,
        max_volume: int,
        device_name: str,
        control_mode: str = CONTROL_MODE_LOCAL_ONLY,
        local_rpc: LocalSoundbarRpcClient | None = None,
        suggested_area: str | None = None,
    ):
        if control_mode not in CONTROL_MODES:
            raise ValueError("Unsupported local control mode")
        self.device = device
        self.coordinator = None
        self.__device_name = device_name
        self.__control_mode = control_mode
        self.__local_rpc = local_rpc
        self.__suggested_area = suggested_area
        self.__max_volume = max_volume
        self.__ocf_woofer_profile = None
        self.__ocf_audio_profiles = {}
        self.__ocf_tone_profiles = {}
        self.ocf_equalizer = OcfEqualizer(self)
        self.__local_status = {}
        self.__local_status_updated_at = None
        self.__local_status_update_lock = asyncio.Lock()
        self.__local_available = False
        self.__local_last_error = None
        self.__local_field_errors = {}
        self.__update_listeners = set()
        self.__last_known_input_source = None
        self.__last_known_sound_mode = None
        self.__pending_local_sound_mode = None
        self.__pending_local_sound_mode_count = 0
        self.__optimistic_sound_mode = None
        self.__optimistic_sound_mode_updated_at = None

    @property
    def local_rpc_enabled(self):
        return self.__local_rpc is not None

    @property
    def ocf_enabled(self):
        return self.__control_mode == CONTROL_MODE_HYBRID_LOCAL_OCF

    @property
    def local_only(self):
        return self.__control_mode == CONTROL_MODE_LOCAL_ONLY

    @property
    def control_mode(self):
        return self.__control_mode

    @property
    def device_id(self):
        return self.device.device_id

    @property
    def device_name(self):
        return self.__device_name

    @property
    def manufacturer(self):
        return self.device.status.ocf_manufacturer_name

    @property
    def model(self):
        model = self.device.status.ocf_model_number
        hint = self.local_identifier
        if model:
            return model
        if isinstance(hint, str) and hint.rsplit("_", 1)[-1].startswith("HW-"):
            return hint.rsplit("_", 1)[-1]
        return None

    @property
    def firmware_version(self):
        return self.device.status.ocf_firmware_version

    @property
    def suggested_area(self):
        return self.__suggested_area

    @property
    def available(self):
        if self.coordinator is not None:
            return self.coordinator.state.snapshot().available
        return self.__local_available

    async def update(self):
        if self.coordinator is not None:
            await self.coordinator.async_request_refresh()
        else:
            await self.update_local_status(min_age=None)

    def __coordinated_value(self, key, default=None):
        if self.coordinator is None:
            return default
        if key in OCF_FIELDS and not self.coordinator.state.use_ocf:
            return None
        return self.coordinator.state.value(key, default)

    async def __try_local_rpc(
        self,
        action: Callable[[LocalSoundbarRpcClient], Awaitable[Any]],
        description: str,
    ):
        if self.__local_rpc is None:
            raise HomeAssistantError("Local RPC is not configured")
        try:
            await action(self.__local_rpc)
            self.__local_available = True
            self.__local_last_error = None
            if self.coordinator is None or not self.coordinator.in_command:
                await self.update_local_status(min_age=None)
        except (LocalRpcCommandError, ValueError) as error:
            await self.update_local_status(min_age=None)
            raise HomeAssistantError(
                f"Local RPC rejected {description}: {error}"
            ) from error
        except LocalRpcError as error:
            self.__local_available = False
            self.__local_last_error = str(error)
            if self.coordinator is not None:
                self.coordinator.transport_changed("rpc", False)
            raise HomeAssistantError(
                f"Local RPC failed during {description}: {error}"
            ) from error

    @property
    def can_turn_on_off(self):
        return self.local_rpc_enabled

    @property
    def can_control_volume(self):
        return self.local_rpc_enabled

    @property
    def can_mute_volume(self):
        return self.local_rpc_enabled

    @property
    def can_select_source(self):
        return self.local_rpc_enabled

    @property
    def can_select_sound_mode(self):
        return self.local_rpc_enabled

    @property
    def can_control_woofer_level(self):
        return self.ocf_woofer_profile is not None

    def can_control_audio_switch(self, field):
        return self.ocf_audio_profile(field) is not None

    @property
    def state(self):
        if self.coordinator is not None:
            return self.__coordinated_value("power")
        value = self.__local_value("power")
        return {"powerOn": "on", "powerOff": "off"}.get(value)

    @coordinated_command(lambda self: {"power": "on"})
    async def switch_on(self):
        await self.__try_local_rpc(lambda rpc: rpc.power_on(), "switch on")

    @coordinated_command(lambda self: {"power": "off"})
    async def switch_off(self):
        await self.__try_local_rpc(lambda rpc: rpc.power_off(), "switch off")

    @property
    def volume_scale(self):
        return _LOCAL_VOLUME_MAX

    @property
    def volume_level(self):
        if self.coordinator is not None:
            return self.__coordinated_value("volume_level")
        value = self.__local_value("volume")
        return (
            None if value is None else min(value, _LOCAL_VOLUME_MAX) / _LOCAL_VOLUME_MAX
        )

    @property
    def volume_muted(self):
        if self.coordinator is not None:
            return self.__coordinated_value("volume_muted")
        return self.__local_value("mute")

    def __volume_target(self, volume):
        if type(volume) not in (int, float) or not 0 <= volume <= 1:
            raise ServiceValidationError("Volume must be between zero and one")
        return min(round(volume * _LOCAL_VOLUME_MAX), self.__max_volume)

    @coordinated_command(
        lambda self, volume: {
            "volume_level": self.__volume_target(volume) / _LOCAL_VOLUME_MAX
        }
    )
    async def set_volume(self, volume):
        level = self.__volume_target(volume)
        await self.__try_local_rpc(lambda rpc: rpc.set_volume(level), "set volume")

    @coordinated_command(lambda self, mute: {"volume_muted": mute})
    async def mute_volume(self, mute):
        if type(mute) is not bool:
            raise ServiceValidationError("Mute must be a boolean")
        await self.__try_local_rpc(lambda rpc: rpc.set_mute(mute), "mute volume")

    @coordinated_command(read_fields=("volume_level",))
    async def volume_up(self):
        await self.__try_local_rpc(lambda rpc: rpc.volume_up(), "volume up")

    @coordinated_command(read_fields=("volume_level",))
    async def volume_down(self):
        await self.__try_local_rpc(lambda rpc: rpc.volume_down(), "volume down")

    @property
    def woofer_level(self):
        return self.__coordinated_value("woofer_level")

    @property
    def woofer_connection(self):
        return self.__coordinated_value("woofer_connection")

    @property
    def input_source(self):
        if self.coordinator is not None:
            return self.__coordinated_value(
                "input_source",
                self.__last_known_input_source if self.state == "off" else None,
            )
        value = self.__local_status_value("input_source", allow_cached=True)
        return (
            self.__ha_source_from_local(value)
            if value
            else self.__last_known_input_source
        )

    @property
    def supported_input_sources(self):
        sources = ["D.IN", "HDMI1", "BT", "TV ARC/eARC", "WIFI"]
        current = self.input_source
        if current and current not in sources:
            sources.append(current)
        return sources

    @coordinated_command(
        lambda self, source: {
            "input_source": self.__ha_source_from_local(
                self.__local_source_from_ha(source)
            )
        }
    )
    async def select_source(self, source):
        value = self.__local_source_from_ha(source)
        await self.__try_local_rpc(
            lambda rpc: rpc.select_input(value), "select input source"
        )

    @property
    def supported_soundmodes(self):
        values = [
            self.__ha_sound_mode_from_local(value) for value in LOCAL_SOUND_MODE_VALUES
        ]
        current = self.sound_mode
        if current and current not in values:
            values.append(current)
        return values

    @coordinated_command(
        lambda self, sound_mode: {
            "sound_mode": self.__ha_sound_mode_from_local(
                self.__local_sound_mode_from_ha(sound_mode)
            )
        }
    )
    async def select_sound_mode(self, sound_mode):
        value = self.__local_sound_mode_from_ha(sound_mode)
        await self.__try_local_rpc(
            lambda rpc: rpc.set_sound_mode(value), "select sound mode"
        )

    @property
    def sound_from_detail_name(self):
        if self.coordinator is not None:
            return self.__coordinated_value("sound_from_detail_name") or None
        if self.__local_value("power") == "powerOff":
            return None
        source = self.__local_value("input_source")
        return (
            _LOCAL_STREAMING_SOURCE_TO_DETAIL.get(source, "External Device")
            if source
            else None
        )

    @property
    def sound_from_mode(self):
        return None

    @property
    def media_app_name(self):
        return self.sound_from_detail_name

    @property
    def active_equalizer_preset(self):
        return self.ocf_equalizer.value("active_equalizer_preset")

    @property
    def supported_equalizer_presets(self):
        return (
            self.ocf_equalizer.options if self.ocf_equalizer.profile is not None else []
        )

    @property
    def equalizer_action(self):
        return self.ocf_equalizer.value("equalizer_action")

    @property
    def equalizer_bands(self):
        return self.ocf_equalizer.value("equalizer_band_levels")

    async def set_equalizer_preset(self, preset):
        await self.ocf_equalizer.async_select(preset)

    @property
    def media_title(self):
        return None

    @property
    def media_artist(self):
        return None

    @property
    def media_coverart_url(self):
        return None

    @property
    def media_coverart_hash(self):
        return None

    @property
    def media_duration(self):
        return None

    @property
    def media_position(self):
        return None

    async def async_dump_discovery_snapshot(self, include_null=True):
        if self.coordinator is not None:
            await self.coordinator.async_refresh_all()
        else:
            await self.update_local_status(min_age=None)
        result = {
            "device_id": self.device_id,
            "device_name": self.device_name,
            "control_mode": self.control_mode,
            "local": {
                "available": self.local_available,
                "last_error": self.local_last_error,
                "field_errors": dict(self.__local_field_errors),
                "status": dict(self.__local_status),
            },
            "resolved": self.retrieve_data,
            "ocf": self.coordinator.ocf_diagnostics()
            if self.coordinator is not None
            else {"configured": False},
            "coordinator": self.coordinator.diagnostics()
            if self.coordinator is not None
            else None,
            "updated_at": datetime.datetime.now(datetime.UTC).isoformat(
                timespec="seconds"
            ),
        }

        def without_null(value):
            if isinstance(value, dict):
                return {
                    key: without_null(item)
                    for key, item in value.items()
                    if item is not None
                }
            if isinstance(value, list):
                return [without_null(item) for item in value]
            return value

        return result if include_null else without_null(result)

    @property
    def night_mode(self):
        return self.__coordinated_value("night_mode")

    async def set_night_mode(self, value):
        await self.__set_audio_setting("night_mode", value)

    @property
    def bass_mode(self):
        return self.__coordinated_value("bass_mode")

    async def set_bass_mode(self, value):
        await self.__set_audio_setting("bass_mode", value)

    @property
    def voice_amplifier(self):
        return self.__coordinated_value("voice_amplifier")

    async def set_voice_amplifier(self, value):
        await self.__set_audio_setting("voice_amplifier", value)

    @property
    def virtual_sound(self):
        return self.__coordinated_value("virtual_sound")

    async def set_virtual_sound(self, value):
        await self.__set_audio_setting("virtual_sound", value)

    @property
    def moderate_bass(self):
        return self.__coordinated_value("moderate_bass")

    async def set_moderate_bass(self, value):
        await self.__set_audio_setting("moderate_bass", value)

    @property
    def space_fit_sound(self):
        return self.__coordinated_value("spacefit_sound")

    async def set_space_fit_sound(self, value):
        await self.__set_audio_setting("spacefit_sound", value)

    def add_update_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Add a listener notified after push updates."""
        if self.coordinator is not None:
            return self.coordinator.async_add_listener(listener)
        self.__update_listeners.add(listener)
        return lambda: self.__update_listeners.discard(listener)

    def __notify_update_listeners(self) -> None:
        if self.coordinator is not None:
            self.coordinator.publish()
            return
        for listener in tuple(self.__update_listeners):
            listener()

    @property
    def local_available(self) -> bool:
        if self.coordinator is not None:
            return self.coordinator.state.transport_available["rpc"]
        return self.__local_available

    @property
    def local_last_error(self) -> str | None:
        return self.__local_last_error

    @property
    def local_codec(self) -> str | None:
        if self.coordinator is not None:
            return self.__coordinated_value("local_codec")
        return self.__local_value("codec")

    @property
    def local_identifier(self) -> str | None:
        if self.coordinator is not None:
            return self.__coordinated_value("local_identifier")
        return self.__local_value("identifier")

    async def __update_local_status(self) -> None:
        if not self.local_rpc_enabled or self.__local_rpc is None:
            return

        previous_status = self.__local_status.copy()
        was_available = self.__local_available
        try:
            readback = dict(await self.__local_rpc.status())
            errors = getattr(self.__local_rpc, "status_errors", {})
            self.__local_field_errors = dict(errors) if isinstance(errors, dict) else {}
            local_status = {
                **previous_status,
                **{
                    key: value
                    for key, value in readback.items()
                    if self.__normalize_local_value(value) is not None
                },
            }
            sound_mode_readback_missing = False
            local_is_powered_off = (
                self.__normalize_local_value(local_status.get("power")) == "powerOff"
            )
            local_input_source = self.__normalize_local_value(
                local_status.get("input_source")
            )
            if local_input_source is not None:
                self.__last_known_input_source = self.__ha_source_from_local(
                    local_input_source
                )
            elif self.__last_known_input_source is not None:
                local_status["input_source"] = self.__local_source_from_ha(
                    self.__last_known_input_source
                )

            local_sound_mode = self.__normalize_local_value(
                local_status.get("sound_mode")
            )
            if local_is_powered_off:
                # Q800F can return stale, alternating sound modes after power-off.
                # Keep the last valid on-state value until the soundbar is on again.
                local_status.pop("sound_mode", None)
                local_sound_mode = None
                self.__pending_local_sound_mode = None
                self.__pending_local_sound_mode_count = 0
            if local_sound_mode is not None:
                local_sound_mode = self.__confirm_local_sound_mode(local_sound_mode)
                local_status["sound_mode"] = local_sound_mode
            elif self.__last_known_sound_mode is not None:
                local_status["sound_mode"] = self.__last_known_sound_mode
                sound_mode_readback_missing = True
            elif self.__optimistic_sound_mode is not None:
                local_status["sound_mode"] = self.__optimistic_sound_mode
                sound_mode_readback_missing = True

            if (
                self.__optimistic_sound_mode is not None
                and not self.__normalize_local_value(local_status.get("sound_mode"))
            ):
                local_status["sound_mode"] = self.__optimistic_sound_mode
                sound_mode_readback_missing = True
            self.__local_status = local_status
            self.__local_status_updated_at = datetime.datetime.now(datetime.UTC)
            self.__local_available = True
            self.__local_last_error = None
            self.__capture_local_state(
                {
                    key: local_status.get(key)
                    for key, value in readback.items()
                    if self.__normalize_local_value(value) is not None
                }
            )
            if sound_mode_readback_missing:
                self.__expire_optimistic_sound_mode()
            else:
                self.__sync_optimistic_sound_mode()
            if self.local_rpc_enabled and (
                not was_available or previous_status != self.__local_status
            ):
                self.__notify_update_listeners()
        except LocalRpcError as err:
            self.__local_available = False
            self.__local_last_error = str(err)
            if self.coordinator is not None:
                self.coordinator.transport_changed("rpc", False)
            if self.local_rpc_enabled and was_available != self.__local_available:
                self.__notify_update_listeners()
            log.debug(
                "[%s] Local RPC status update failed for %s: %s",
                DOMAIN,
                self.device_name,
                err,
            )

    async def update_local_status(
        self,
        min_age: datetime.timedelta | None = _LOCAL_FAST_POLL_MIN_AGE,
    ) -> None:
        """Refresh only the local JSON-RPC status in hybrid mode."""
        if min_age is not None and self.__has_fresh_local_status(min_age):
            return

        async with self.__local_status_update_lock:
            if min_age is not None and self.__has_fresh_local_status(min_age):
                return
            await self.__update_local_status()

    async def update_local_input_source(
        self,
        min_age: datetime.timedelta | None = _LOCAL_FAST_POLL_MIN_AGE,
    ) -> None:
        """Refresh fast-changing local state used by media player readback."""
        if not self.local_rpc_enabled or self.__local_rpc is None:
            return
        if min_age is not None and self.__has_fresh_local_media_status(min_age):
            return

        async with self.__local_status_update_lock:
            if min_age is not None and self.__has_fresh_local_media_status(min_age):
                return

            previous_status = self.__local_status.copy()
            was_available = self.__local_available
            try:
                values = await asyncio.gather(
                    self.__local_rpc.power_state(),
                    self.__local_rpc.input_source(),
                    self.__local_rpc.volume(),
                    self.__local_rpc.is_muted(),
                    self.__local_rpc.sound_mode(),
                    return_exceptions=True,
                )
                keys = ("power", "input_source", "volume", "mute", "sound_mode")
                readback, errors = split_local_readback(
                    dict(zip(keys, values, strict=True)), require_power=True
                )
                self.__local_field_errors = {
                    key: error
                    for key, error in self.__local_field_errors.items()
                    if key not in keys
                } | errors
                values = list(readback.values())
                (
                    local_power,
                    local_input_source,
                    local_volume,
                    local_mute,
                    local_sound_mode,
                ) = values
            except LocalRpcError as err:
                # The coordinator retains bounded cached fields, not a false
                # transport-health indication after a failed RPC request.
                self.__local_available = False
                self.__local_last_error = str(err)
                if self.coordinator is not None:
                    self.coordinator.transport_changed("rpc", False)
                if self.local_rpc_enabled and was_available != self.__local_available:
                    self.__notify_update_listeners()
                log.debug(
                    "[%s] Local RPC media state update failed for %s: %s",
                    DOMAIN,
                    self.device_name,
                    err,
                )
                return

            local_power = self.__normalize_local_value(local_power)
            if local_power is not None:
                self.__local_status["power"] = local_power

            local_volume = self.__normalize_local_value(local_volume)
            if local_volume is not None:
                self.__local_status["volume"] = local_volume

            local_mute = self.__normalize_local_value(local_mute)
            if local_mute is not None:
                self.__local_status["mute"] = local_mute

            local_sound_mode = self.__normalize_local_value(local_sound_mode)
            if local_power == "powerOff":
                self.__pending_local_sound_mode = None
                self.__pending_local_sound_mode_count = 0
                self.__local_status.pop("sound_mode", None)
            elif local_sound_mode is not None:
                self.__local_status["sound_mode"] = self.__confirm_local_sound_mode(
                    local_sound_mode
                )
            elif self.__last_known_sound_mode is not None:
                self.__local_status["sound_mode"] = self.__last_known_sound_mode

            local_input_source = self.__normalize_local_value(local_input_source)
            if local_input_source is not None:
                self.__local_status["input_source"] = local_input_source
                self.__last_known_input_source = self.__ha_source_from_local(
                    local_input_source
                )
            elif self.__last_known_input_source is not None:
                self.__local_status["input_source"] = self.__local_source_from_ha(
                    self.__last_known_input_source
                )

            self.__local_status_updated_at = datetime.datetime.now(datetime.UTC)
            self.__local_available = True
            self.__local_last_error = None
            self.__capture_local_state(
                {
                    key: self.__local_status.get(key)
                    for key, value in zip(
                        ("power", "input_source", "volume", "mute", "sound_mode"),
                        values,
                        strict=True,
                    )
                    if self.__normalize_local_value(value) is not None
                }
            )
            if not was_available or previous_status != self.__local_status:
                self.__notify_update_listeners()

    def __capture_local_state(self, readback: dict[str, Any]) -> None:
        if self.coordinator is None:
            return
        values = {}
        for key, value in readback.items():
            if value is None:
                continue
            if key == "power":
                values["power"] = "off" if value == "powerOff" else "on"
            elif key == "volume":
                values["volume_level"] = value / _LOCAL_VOLUME_MAX
            elif key == "mute":
                values["volume_muted"] = value
            elif key == "input_source":
                values["input_source"] = self.__ha_source_from_local(value)
                values["sound_from_detail_name"] = (
                    _LOCAL_STREAMING_SOURCE_TO_DETAIL.get(value, "External Device")
                )
            elif key == "sound_mode" and readback.get("power") != "powerOff":
                values["sound_mode"] = self.__ha_sound_mode_from_local(value)
            elif key in ("codec", "identifier"):
                if (
                    key == "identifier"
                    and self.__coordinated_value("local_identifier") == value
                ):
                    continue
                values[f"local_{key}"] = value
        if readback.get("power") == "powerOff":
            values["sound_from_detail_name"] = ""
        self.coordinator.state.transport_available["rpc"] = True
        self.coordinator.receive("rpc", values)

    async def read_local_fields(self, fields: set[str]) -> None:
        """Read only fields affected by a successful local command."""
        methods = {
            "power": ("power", self.__local_rpc.power_state),
            "volume_level": ("volume", self.__local_rpc.volume),
            "volume_muted": ("mute", self.__local_rpc.is_muted),
            "input_source": ("input_source", self.__local_rpc.input_source),
            "sound_mode": ("sound_mode", self.__local_rpc.sound_mode),
        }
        readback = {}
        try:
            async with self.__local_status_update_lock:
                for field in fields:
                    if field not in methods:
                        continue
                    key, method = methods[field]
                    try:
                        readback[key] = await method()
                    except LocalRpcError as err:
                        readback[key] = err
                readback, errors = split_local_readback(readback)
                self.__local_field_errors = {
                    key: error
                    for key, error in self.__local_field_errors.items()
                    if key not in readback
                } | errors
                self.__local_status.update(
                    {key: value for key, value in readback.items() if value is not None}
                )
                self.__capture_local_state(readback)
                self.__local_available = True
                self.__local_last_error = None
        except LocalRpcError as err:
            self.__local_available = False
            self.__local_last_error = str(err)
            self.coordinator.transport_changed("rpc", False)
            raise

    def __local_value(self, key: str) -> Any:
        return self.__local_status_value(key, allow_cached=False)

    def __local_status_value(self, key: str, allow_cached: bool = False) -> Any:
        if not self.local_rpc_enabled or not self.__local_available:
            if not allow_cached:
                return None
            if not self.__has_recent_local_status():
                return None
        value = self.__local_status.get(key)
        return self.__normalize_local_value(value)

    def __has_recent_local_status(self) -> bool:
        if not self.local_rpc_enabled or not self.__local_status:
            return False
        if self.__local_available:
            return True
        if self.__local_status_updated_at is None:
            return False
        return (
            datetime.datetime.now(datetime.UTC) - self.__local_status_updated_at
            <= _LOCAL_STATUS_CACHE_TIMEOUT
        )

    def __has_fresh_local_status(self, max_age: datetime.timedelta) -> bool:
        if (
            not self.local_rpc_enabled
            or not self.__local_available
            or self.__local_status_updated_at is None
        ):
            return False
        return (
            datetime.datetime.now(datetime.UTC) - self.__local_status_updated_at
            <= max_age
        )

    def __has_cached_local_status(self, max_age: datetime.timedelta) -> bool:
        if (
            not self.local_rpc_enabled
            or not self.__local_status
            or self.__local_status_updated_at is None
        ):
            return False
        return (
            datetime.datetime.now(datetime.UTC) - self.__local_status_updated_at
            <= max_age
        )

    def __has_fresh_local_media_status(self, max_age: datetime.timedelta) -> bool:
        if not self.__has_fresh_local_status(max_age):
            return False
        return "power" in self.__local_status and "input_source" in self.__local_status

    @staticmethod
    def __normalize_local_value(value: Any) -> Any:
        if value in ("", None):
            return None
        if isinstance(value, str) and value.strip().lower() == "unknown":
            return None
        return value

    @staticmethod
    def __ha_source_from_local(source: str | None) -> str | None:
        if source is None:
            return None
        source_key = SoundbarDevice.__source_map_key(source)
        return _LOCAL_SOURCE_TO_HA.get(source_key, source)

    @staticmethod
    def __local_source_from_ha(source: str) -> str:
        source = source.strip()
        return _HA_SOURCE_TO_LOCAL.get(source, source)

    @staticmethod
    def __source_map_key(source: str) -> str:
        return (
            source.strip().upper().replace("-", "_").replace("/", "_").replace(" ", "_")
        )

    @staticmethod
    def __ha_sound_mode_from_local(sound_mode: str | None) -> str | None:
        if sound_mode is None:
            return None
        return _LOCAL_SOUND_MODE_TO_HA.get(sound_mode, sound_mode)

    @staticmethod
    def __local_sound_mode_from_ha(sound_mode: str) -> str:
        return _HA_SOUND_MODE_TO_LOCAL.get(sound_mode, sound_mode.upper())

    def __local_streaming_sound_from_detail_name(self) -> str | None:
        if not self.local_rpc_enabled:
            return None

        local_source = self.__local_streaming_input_source()
        if not isinstance(local_source, str):
            return None

        normalized_source = local_source.strip().upper()
        if not normalized_source:
            return None

        direct_match = _LOCAL_STREAMING_SOURCE_TO_DETAIL.get(normalized_source)
        if direct_match is not None:
            return direct_match

        for source_part, detail_name in _LOCAL_STREAMING_SOURCE_TO_DETAIL.items():
            if source_part in normalized_source:
                return detail_name

        return None

    def __local_streaming_input_source(self) -> str | None:
        if not self.__has_cached_local_status(_LOCAL_STREAMING_SOURCE_CACHE_TIMEOUT):
            return None
        return self.__normalize_local_value(self.__local_status.get("input_source"))

    def remember_input_source(self, source: str | None) -> None:
        """Remember the last HA input source for off/unavailable local readback."""
        if self.__normalize_local_value(source) is None:
            return
        self.__last_known_input_source = source

    def remember_sound_mode(self, sound_mode: str | None) -> None:
        """Remember the last HA sound mode for off/unavailable local readback."""
        if self.__normalize_local_value(sound_mode) is None:
            return
        self.__last_known_sound_mode = self.__local_sound_mode_from_ha(sound_mode)
        self.__pending_local_sound_mode = None
        self.__pending_local_sound_mode_count = 0

    def __confirm_local_sound_mode(self, sound_mode: str) -> str:
        """Return a stable local sound mode after confirming changes twice."""
        if self.__last_known_sound_mode is None:
            self.__last_known_sound_mode = sound_mode
            self.__pending_local_sound_mode = None
            self.__pending_local_sound_mode_count = 0
            return sound_mode

        if sound_mode == self.__last_known_sound_mode:
            self.__pending_local_sound_mode = None
            self.__pending_local_sound_mode_count = 0
            return sound_mode

        if sound_mode == self.__pending_local_sound_mode:
            self.__pending_local_sound_mode_count += 1
        else:
            self.__pending_local_sound_mode = sound_mode
            self.__pending_local_sound_mode_count = 1

        if self.__pending_local_sound_mode_count >= _LOCAL_SOUND_MODE_CONFIRMATIONS:
            self.__last_known_sound_mode = sound_mode
            self.__pending_local_sound_mode = None
            self.__pending_local_sound_mode_count = 0
            return sound_mode

        # Q800F intermittently alternates between two values while its actual
        # sound mode has not changed. Keep the confirmed state until readback
        # has repeated the new value.
        return self.__last_known_sound_mode

    @property
    def ocf_woofer_profile(self):
        coordinator = self.coordinator
        if (
            coordinator is None
            or not coordinator.state.use_ocf
            or coordinator.ocf is None
        ):
            return None
        if self.__ocf_woofer_profile is not None:
            return self.__ocf_woofer_profile
        if coordinator.ocf.validated is not True or not (
            self.model == Q800F_WOOFER.model
            or self.local_identifier == Q800F_WOOFER.rpc_identifier
        ):
            return None
        records = coordinator.state.records["ocf"]
        level = records.get("woofer_level")
        connection = records.get("woofer_connection")
        if level is None or connection is None or connection.value not in ("on", "off"):
            return None
        try:
            validate_woofer_level(level.value)
        except ValueError:
            return None
        # Keep the proven route across outages; never replay through another transport.
        self.__ocf_woofer_profile = Q800F_WOOFER
        return self.__ocf_woofer_profile

    @property
    def woofer_control_available(self) -> bool:
        if self.ocf_woofer_profile is not None:
            coordinator = self.coordinator
            power = coordinator.state.resolve("power")
            level = coordinator.state.records["ocf"].get("woofer_level")
            connection = coordinator.state.records["ocf"].get("woofer_connection")
            return bool(
                coordinator.ocf_field_available("woofer_level")
                and coordinator.ocf_field_available("woofer_connection")
                and type(level.value) is int
                and Q800F_WOOFER.minimum <= level.value <= Q800F_WOOFER.maximum
                and connection.value == "on"
                and coordinator.state.snapshot().local_available
                and power is not None
                and power.source == "rpc"
                and power.value == "on"
            )
        return False

    def require_ocf_woofer_available(self):
        if self.ocf_woofer_profile is None or not self.woofer_control_available:
            raise HomeAssistantError(
                "Local Woofer control is unavailable; check power, OCF access "
                "and subwoofer connection"
            )

    def ocf_audio_profile(self, field):
        profile = Q800F_AUDIO_SWITCHES.get(field)
        coordinator = self.coordinator
        if (
            profile is None
            or coordinator is None
            or not coordinator.state.use_ocf
            or coordinator.ocf is None
        ):
            return None
        if field in self.__ocf_audio_profiles:
            return self.__ocf_audio_profiles[field]
        if coordinator.ocf.validated is not True or not (
            self.model == Q800F_WOOFER.model
            or self.local_identifier == Q800F_WOOFER.rpc_identifier
        ):
            return None
        record = coordinator.state.records["ocf"].get(field)
        if (
            record is None
            or type(record.value) is not bool
            or record.observed_at
            < coordinator.state.written_at.get(field, float("-inf"))
        ):
            return None
        self.__ocf_audio_profiles[field] = profile
        return profile

    def ocf_tone_profile(self, field):
        profile = Q800F_TONE.get(field)
        coordinator = self.coordinator
        if (
            profile is None
            or coordinator is None
            or not coordinator.state.use_ocf
            or coordinator.ocf is None
        ):
            return None
        if field in self.__ocf_tone_profiles:
            return self.__ocf_tone_profiles[field]
        record = coordinator.state.records["ocf"].get(field)
        if (
            coordinator.ocf.validated is not True
            or not (
                self.model == Q800F_WOOFER.model
                or self.local_identifier == Q800F_WOOFER.rpc_identifier
            )
            or not coordinator.ocf_field_available(field)
            or record is None
            or type(record.value) is not int
            or not profile.minimum <= record.value <= profile.maximum
            or record.observed_at
            < coordinator.state.written_at.get(field, float("-inf"))
        ):
            return None
        self.__ocf_tone_profiles[field] = profile
        return profile

    def tone_level(self, field):
        profile = self.ocf_tone_profile(field)
        if profile is None:
            return None
        record = self.coordinator.state.records["ocf"].get(field)
        if (
            record is None
            or not record.fresh(self.coordinator.state.clock())
            or type(record.value) is not int
            or not profile.minimum <= record.value <= profile.maximum
        ):
            return None
        return record.value

    def tone_control_available(self, field):
        if self.ocf_tone_profile(field) is None:
            return False
        coordinator = self.coordinator
        power = coordinator.state.resolve("power")
        return bool(
            coordinator.ocf_field_available(field)
            and self.tone_level(field) is not None
            and coordinator.state.snapshot().local_available
            and power is not None
            and power.source == "rpc"
            and power.value == "on"
        )

    def require_tone_available(self, field):
        if not self.tone_control_available(field):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="ocf_tone_unavailable"
            )

    async def set_tone_level(self, field, value):
        try:
            if field not in Q800F_TONE:
                raise ValueError
            value = validate_tone_level(value)
        except ValueError:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="ocf_tone_invalid"
            ) from None
        self.require_tone_available(field)
        profile = self.ocf_tone_profile(field)
        try:
            result = await self.coordinator.async_write_ocf(
                profile.href,
                {profile.property: value},
                tone_profile=profile,
                guard=lambda: self.require_tone_available(field),
                required_fields=(field,),
            )
        except OcfError:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="ocf_tone_unconfirmed"
            ) from None
        if (
            result.readback_matches is not True
            or not self.tone_control_available(field)
            or self.tone_level(field) != value
        ):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="ocf_tone_unconfirmed"
            )

    def audio_switch_available(self, field):
        if self.ocf_audio_profile(field) is not None:
            coordinator = self.coordinator
            power = coordinator.state.resolve("power")
            return bool(
                coordinator.ocf_field_available(field)
                and type(coordinator.state.records["ocf"][field].value) is bool
                and coordinator.state.snapshot().local_available
                and power is not None
                and power.source == "rpc"
                and power.value == "on"
            )
        return False

    def require_ocf_audio_available(self, field):
        if self.ocf_audio_profile(field) is None or not self.audio_switch_available(
            field
        ):
            raise HomeAssistantError(
                "Local audio setting is unavailable; check power and OCF readback"
            )

    async def __set_audio_setting(self, field, enabled):
        if type(enabled) is not bool:
            raise HomeAssistantError("Audio setting must be a boolean")
        profile = self.ocf_audio_profile(field)
        if profile is not None:
            self.require_ocf_audio_available(field)
            try:
                result = await self.coordinator.async_write_ocf(
                    profile.href,
                    {profile.property: int(enabled)},
                    switch_profile=profile,
                    guard=lambda: self.require_ocf_audio_available(field),
                    required_fields=(field,),
                )
            except OcfError as error:
                raise HomeAssistantError(
                    f"Local audio write failed ({error.kind}); no fallback was attempted"
                ) from None
            if (
                result.readback_matches is not True
                or not self.audio_switch_available(field)
                or self.coordinator.state.records["ocf"][field].value is not enabled
            ):
                raise HomeAssistantError(
                    "Local audio write was not confirmed; no retry was attempted"
                )
            return
        raise HomeAssistantError(
            "A verified local audio profile and OCF credential are required"
        )

    def __set_optimistic_sound_mode(self, sound_mode: str) -> None:
        self.__optimistic_sound_mode = sound_mode
        self.__optimistic_sound_mode_updated_at = datetime.datetime.now(datetime.UTC)

    def __sync_optimistic_sound_mode(self) -> None:
        if self.__optimistic_sound_mode is None:
            return

        local_sound_mode = self.__normalize_local_value(
            self.__local_status.get("sound_mode")
        )
        if local_sound_mode == self.__optimistic_sound_mode:
            self.__optimistic_sound_mode = None
            self.__optimistic_sound_mode_updated_at = None
            return

        self.__expire_optimistic_sound_mode()

    def __expire_optimistic_sound_mode(self) -> None:
        if self.__optimistic_sound_mode is None:
            return

        if (
            self.__optimistic_sound_mode_updated_at is not None
            and datetime.datetime.now(datetime.UTC)
            - self.__optimistic_sound_mode_updated_at
            > _OPTIMISTIC_SOUND_MODE_TIMEOUT
        ):
            self.__optimistic_sound_mode = None
            self.__optimistic_sound_mode_updated_at = None

    async def set_woofer(self, level: float):
        try:
            level = validate_woofer_level(level)
        except ValueError:
            raise HomeAssistantError(
                "Woofer level must be an integer between -12 and 6"
            ) from None
        profile = self.ocf_woofer_profile
        if profile is not None:
            self.require_ocf_woofer_available()
            try:
                result = await self.coordinator.async_write_ocf(
                    WOOFER_HREF,
                    {WOOFER_FIELD: level},
                    woofer_profile=profile,
                    guard=self.require_ocf_woofer_available,
                    required_fields=("woofer_level", "woofer_connection"),
                )
            except OcfError as error:
                raise HomeAssistantError(
                    f"Local Woofer operation failed ({error.kind}); "
                    "no fallback was attempted"
                ) from None
            if (
                result.readback_matches is not True
                or not self.woofer_control_available
                or self.woofer_level != level
            ):
                outcome = "ambiguous" if result.ambiguous else "readback mismatch"
                raise HomeAssistantError(
                    f"Local Woofer write was not confirmed ({outcome}); "
                    "no retry was attempted"
                )
            return
        raise HomeAssistantError(
            "A verified OCF credential and Woofer profile are required"
        )

    @property
    def sound_mode(self):
        if self.coordinator is not None:
            return self.__coordinated_value(
                "sound_mode",
                self.__ha_sound_mode_from_local(self.__last_known_sound_mode)
                if self.state == "off" and self.__last_known_sound_mode
                else None,
            )
        local_sound_mode = self.__local_value("sound_mode")
        if local_sound_mode is not None:
            return self.__ha_sound_mode_from_local(local_sound_mode)
        if self.__optimistic_sound_mode is not None:
            return self.__ha_sound_mode_from_local(self.__optimistic_sound_mode)
        if self.__last_known_sound_mode is not None:
            return self.__ha_sound_mode_from_local(self.__last_known_sound_mode)
        return None

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
                "virtual": self.virtual_sound,
                "moderate_bass": self.moderate_bass,
                "spacefit_sound": self.space_fit_sound,
            },
            "equalizer": {
                "active_preset": self.active_equalizer_preset,
                "supported_presets": self.supported_equalizer_presets,
                "action": self.equalizer_action,
                "bands": self.equalizer_bands,
            },
            "tone": {
                "bass_level": self.tone_level("bass_level"),
                "treble_level": self.tone_level("treble_level"),
            },
            "media": {
                "media_title": self.media_title,
                "media_artist": self.media_artist,
                "media_cover_url": self.media_coverart_url,
                "media_duration": self.media_duration,
                "media_position": self.media_position,
            },
        }
