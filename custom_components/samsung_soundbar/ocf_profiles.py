"""Model-scoped writes backed by authenticated Q800F read/write/readback probes."""

from dataclasses import dataclass

WOOFER_HREF = "/sec/networkaudio/woofer"
WOOFER_FIELD = "x.com.samsung.networkaudio.woofer"


def validate_woofer_level(value):
    """Keep the actual dB scale; never truncate fractions or use a slider index."""
    if type(value) not in (int, float) or not -12 <= value <= 6 or int(value) != value:
        raise ValueError("Woofer level must be an integer between -12 and 6")
    return int(value)


@dataclass(frozen=True)
class WooferProfile:
    model: str
    rpc_identifier: str
    minimum: int = -12
    maximum: int = 6
    step: int = 1


# 2026-11-07, HW-Q800F / 1103.1: -12, -7, -6 and +6 each read back;
# the earlier 0/+1 probe and the -7 probe establish a continuous integer scale.
# This is not an exhaustive step scan or a claim about other soundbar models.
Q800F_WOOFER = WooferProfile("HW-Q800F", "22_AV_HW-Q800F")


@dataclass(frozen=True)
class ToneProfile:
    field: str
    property: str
    minimum: int = -6
    maximum: int = 6
    step: int = 1
    href: str = "/sec/networkaudio/tone"


def validate_tone_level(value):
    """Accept exact integral levels only; no truncation or clamping."""
    if type(value) not in (int, float) or not -6 <= value <= 6 or int(value) != value:
        raise ValueError("Tone level must be an integer between -6 and 6")
    return int(value)


# 2026-11-07, Q800F/1103.1: each field -6/-1/+1/+6 read back in Surround;
# the other tone field, advanced audio and EQ were unchanged. Not other models/modes.
Q800F_TONE = {
    "bass_level": ToneProfile("bass_level", "x.com.samsung.networkaudio.bass"),
    "treble_level": ToneProfile("treble_level", "x.com.samsung.networkaudio.treble"),
}


@dataclass(frozen=True)
class EqualizerProfile:
    href: str = "/sec/networkaudio/eq"
    presets: tuple[str, ...] = ("NONE", "POP", "JAZZ", "CLASSIC", "CUSTOM")
    band_count: int = 7
    minimum: int = -6
    maximum: int = 6


Q800F_EQ = EqualizerProfile()
EQ_PREFIX = "x.com.samsung.networkaudio."


def parse_eq_metadata(payload):
    """Project only the observed Q800F schema; do not publish arbitrary enums."""
    preset = payload.get(EQ_PREFIX + "EQname")
    options = payload.get(EQ_PREFIX + "supportedList")
    action = payload.get(EQ_PREFIX + "action")
    bands = payload.get(EQ_PREFIX + "EQband")
    options_valid = (
        isinstance(options, list)
        and 0 < len(options) <= 32
        and all(isinstance(item, str) for item in options)
        and len(set(options)) == len(options)
    )
    known = (
        [item for item in options if item in Q800F_EQ.presets] if options_valid else []
    )
    bands_valid = (
        isinstance(bands, list)
        and len(bands) == Q800F_EQ.band_count
        and all(
            type(item) is str and item in {str(value) for value in range(-6, 7)}
            for item in bands
        )
    )
    valid = preset in known and action == "setEQmode" and bands_valid
    values = {"equalizer_schema_valid": valid}
    if isinstance(preset, str) and preset in Q800F_EQ.presets:
        values["active_equalizer_preset"] = preset
    if known:
        values["supported_equalizer_presets"] = known
    if action == "setEQmode":
        values["equalizer_action"] = action
    if bands_valid:
        values["equalizer_band_levels"] = [int(item) for item in bands]
    return values


def valid_eq_payload(payload):
    return (
        isinstance(payload, dict)
        and set(payload) == {EQ_PREFIX + "EQname", EQ_PREFIX + "action"}
        and type(payload[EQ_PREFIX + "EQname"]) is str
        and payload[EQ_PREFIX + "EQname"] in Q800F_EQ.presets
        and payload[EQ_PREFIX + "action"] == "setEQmode"
    )


@dataclass(frozen=True)
class AudioSwitchProfile:
    field: str
    href: str
    property: str
    suffix: str
    getter: str
    setter: str
    name: str
    icon: str


_BASE = "/sec/networkaudio/"
_PREFIX = "x.com.samsung.networkaudio."
# Exact integer 0/1 single-property writes, probed on Q800F/1103.1.
Q800F_AUDIO_SWITCHES = {
    item.field: item
    for item in (
        AudioSwitchProfile(
            "night_mode",
            _BASE + "advancedaudio",
            _PREFIX + "nightmode",
            "nightmode",
            "night_mode",
            "set_night_mode",
            "Nightmode",
            "mdi:weather-night",
        ),
        AudioSwitchProfile(
            "bass_mode",
            _BASE + "advancedaudio",
            _PREFIX + "bassboost",
            "bassmode",
            "bass_mode",
            "set_bass_mode",
            "Bassmode",
            "mdi:speaker-wireless",
        ),
        AudioSwitchProfile(
            "voice_amplifier",
            _BASE + "advancedaudio",
            _PREFIX + "voiceamplifier",
            "voice_amplifier",
            "voice_amplifier",
            "set_voice_amplifier",
            "Voice Amplifier",
            "mdi:account-voice",
        ),
        AudioSwitchProfile(
            "virtual_sound",
            _BASE + "advancedaudio",
            _PREFIX + "virtual",
            "virtual",
            "virtual_sound",
            "set_virtual_sound",
            "Virtual",
            "mdi:surround-sound",
        ),
        AudioSwitchProfile(
            "moderate_bass",
            _BASE + "advancedaudio",
            _PREFIX + "moderatebass",
            "moderate_bass",
            "moderate_bass",
            "set_moderate_bass",
            "Moderate Bass",
            "mdi:volume-low",
        ),
        AudioSwitchProfile(
            "spacefit_sound",
            _BASE + "spacefitSound",
            _PREFIX + "spacefitSound",
            "space_fit_sound",
            "space_fit_sound",
            "set_space_fit_sound",
            "SpaceFit Sound",
            "mdi:surround-sound",
        ),
    )
}
LEGACY_AUDIO_SWITCH_FIELDS = frozenset(
    {"night_mode", "bass_mode", "voice_amplifier", "virtual_sound"}
)
