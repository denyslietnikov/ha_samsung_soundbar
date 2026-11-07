"""Explicit, non-sensitive OCF field projection. Unknown device data stays private."""

from dataclasses import dataclass

from .ocf_profiles import parse_eq_metadata

BASE = "/sec/networkaudio/"
PREFIX = "x.com.samsung.networkaudio."
CHANNEL_NAMES = frozenset(
    {
        "center",
        "side",
        "wide",
        "front",
        "rear",
        "rearSide",
        "rearTop",
        "frontTop",
        "top",
    }
)
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
    BASE + "channelVolume": {"channel_level_" + name for name in CHANNEL_NAMES},
    BASE + "audiosync": {"audio_sync"},
}
OCF_FIELDS = frozenset().union(*RESOURCE_FIELDS.values())
POLL_HREFS = tuple(RESOURCE_FIELDS)


@dataclass(frozen=True)
class OcfProjection:
    values: dict
    supported: bool | None
    invalidated: tuple[str, ...] = ()


def project_ocf(href, payload):
    """Only known names, enums and numeric settings enter shared state or dumps."""
    supported = payload.get(PREFIX + "isSupported", payload.get("isSupported"))
    supported = (
        bool(supported) if type(supported) is int and supported in (0, 1) else None
    )
    values = {}
    invalidated = []
    if supported is False:
        return OcfProjection(values, supported)
    names = {
        "woofer": {"woofer": "woofer_level"},
        "tone": {"bass": "bass_level", "treble": "treble_level"},
        "audiosync": {"audiosync": "audio_sync"},
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
    if href == BASE + "channelVolume":
        channels = payload.get(PREFIX + "channelVolume")
        if isinstance(channels, list) and len(channels) <= 32:
            for item in channels:
                if not isinstance(item, dict):
                    continue
                name, value = item.get("name"), item.get("value")
                if (
                    isinstance(name, str)
                    and name in CHANNEL_NAMES
                    and item.get("status") in (0, "off")
                ):
                    invalidated.append("channel_level_" + name)
                    continue
                if (
                    isinstance(name, str)
                    and name in CHANNEL_NAMES
                    and type(value) is int
                    and -100 <= value <= 100
                    and item.get("status") in (1, "on")
                ):
                    values["channel_level_" + name] = value
    return OcfProjection(values, supported, tuple(invalidated))
