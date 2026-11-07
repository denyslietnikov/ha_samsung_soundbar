"""Explicit, non-sensitive OCF field projection. Unknown device data stays private."""

from dataclasses import dataclass

from .ocf_channels import CHANNEL_NAMES, parse_channel_volume
from .ocf_profiles import parse_eq_metadata

BASE = "/sec/networkaudio/"
PREFIX = "x.com.samsung.networkaudio."
RESOURCE_FIELDS = {
    BASE + "woofer": {"woofer_level", "woofer_connection"},
    BASE + "advancedaudio": {
        "night_mode",
        "bass_mode",
        "voice_amplifier",
        "virtual_sound",
        "moderate_bass",
    },
    BASE + "spacefitSound": {"spacefit_sound"},
    BASE + "tone": {"bass_level", "treble_level"},
    BASE + "eq": {
        "active_equalizer_preset",
        "equalizer_band_levels",
        "supported_equalizer_presets",
        "equalizer_action",
        "equalizer_schema_valid",
    },
    BASE + "currentEQMode": {"current_equalizer_mode"},
    BASE + "channelVolume": {
        "channel_schema_valid",
        *("channel_level_" + name for name in CHANNEL_NAMES),
        *("channel_status_" + name for name in CHANNEL_NAMES),
    },
    BASE + "audiosync": {"audio_sync", "audio_sync_supported"},
}
OCF_FIELDS = frozenset().union(*RESOURCE_FIELDS.values())
POLL_HREFS = tuple(RESOURCE_FIELDS)


@dataclass(frozen=True)
class OcfProjection:
    values: dict
    supported: bool | None
    invalidated: tuple[str, ...] = ()


def _resource_support(payload):
    supported = payload.get(PREFIX + "isSupported", payload.get("isSupported"))
    return bool(supported) if type(supported) is int and supported in (0, 1) else None


def project_audio_sync(payload):
    """Project a dedicated AudioSync GET, never retaining missing observations."""
    supported = _resource_support(payload)
    values = {}
    if supported is not None:
        values["audio_sync_supported"] = supported
    value = payload.get(PREFIX + "audiosync")
    if supported is True and type(value) is int and -1000 <= value <= 1000:
        values["audio_sync"] = value
    invalidated = tuple(
        field for field in ("audio_sync", "audio_sync_supported") if field not in values
    )
    return OcfProjection(values, supported, invalidated)


def project_ocf(href, payload):
    """Only known names, enums and numeric settings enter shared state or dumps."""
    if href == BASE + "audiosync":
        return project_audio_sync(payload)
    supported = _resource_support(payload)
    values = {}
    invalidated = []
    if href == BASE + "channelVolume":
        channels = parse_channel_volume(payload)
        return OcfProjection(channels.values, supported, channels.invalidated)
    if supported is False:
        return OcfProjection(values, supported)
    names = {
        "woofer": {"woofer": "woofer_level"},
        "tone": {"bass": "bass_level", "treble": "treble_level"},
    }.get(href.removeprefix(BASE), {})
    for raw, field in names.items():
        value = payload.get(PREFIX + raw)
        if type(value) is int and -1000 <= value <= 1000:
            values[field] = value
    flags = {
        "advancedaudio": {
            "nightmode": "night_mode",
            "bassboost": "bass_mode",
            "voiceamplifier": "voice_amplifier",
            "virtual": "virtual_sound",
            "moderatebass": "moderate_bass",
        },
        "spacefitSound": {"spacefitSound": "spacefit_sound"},
    }.get(href.removeprefix(BASE), {})
    for raw, field in flags.items():
        value = payload.get(PREFIX + raw)
        if type(value) is int and value in (0, 1):
            values[field] = bool(value)
    if href == BASE + "woofer":
        connection = payload.get(PREFIX + "connection")
        if connection in ("on", "off"):
            values["woofer_connection"] = connection
    if href == BASE + "eq":
        values.update(parse_eq_metadata(payload))
        preset = payload.get(PREFIX + "EQname")
        # Do not export arbitrary strings from a device as diagnostic metadata.
        if isinstance(preset, str) and preset.lower() in {
            "standard",
            "custom",
            "pop",
            "jazz",
            "classic",
            "rock",
        }:
            values["active_equalizer_preset"] = preset
        bands = payload.get(PREFIX + "EQband")
        if (
            isinstance(bands, list)
            and 0 < len(bands) <= 32
            and all(type(value) is int and -100 <= value <= 100 for value in bands)
        ):
            values["equalizer_band_levels"] = list(bands)
    if href == BASE + "currentEQMode":
        mode = payload.get(PREFIX + "currentEQMode")
        if mode in ("tone", "usereq"):
            values["current_equalizer_mode"] = mode
    return OcfProjection(values, supported, tuple(invalidated))
