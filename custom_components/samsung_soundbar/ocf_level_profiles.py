"""Canonical Q800F channel-trim and audio-delay grants, never arbitrary writes."""

from dataclasses import dataclass

PREFIX = "x.com.samsung.networkaudio."
CHANNEL_HREF = "/sec/networkaudio/channelVolume"
SYNC_HREF = "/sec/networkaudio/audiosync"


@dataclass(frozen=True)
class LevelProfile:
    field: str
    href: str
    property: str
    minimum: int
    maximum: int
    step: int = 1
    wire_name: str | None = None
    unit: str | None = None


Q800F_CHANNEL_LEVELS = {
    "channel_level_" + field: LevelProfile(
        "channel_level_" + field,
        CHANNEL_HREF,
        PREFIX + "channelVolume",
        -6,
        6,
        wire_name=wire,
    )
    for field, wire in (
        ("center", "Spk_Center"),
        ("side", "Spk_Side"),
        ("frontTop", "Spk_Front_Top"),
        ("rear", "Spk_Rear"),
        ("rearTop", "Spk_Rear_Top"),
    )
}
Q800F_AUDIO_SYNC = LevelProfile(
    "audio_sync", SYNC_HREF, PREFIX + "audiosync", 0, 300, step=25, unit="ms"
)
Q800F_LEVELS = {**Q800F_CHANNEL_LEVELS, "audio_sync": Q800F_AUDIO_SYNC}


def validate_level(profile, value):
    """Accept exact integral values on the model's verified scale."""
    if (
        not any(profile is item for item in Q800F_LEVELS.values())
        or type(value) not in (int, float)
        or not profile.minimum <= value <= profile.maximum
        or int(value) != value
        or (int(value) - profile.minimum) % profile.step
    ):
        raise ValueError("Invalid local audio level")
    return int(value)


def level_payload(profile, value):
    value = validate_level(profile, value)
    if profile.wire_name is not None:
        return {profile.property: [{"name": profile.wire_name, "value": value}]}
    return {profile.property: value}


def valid_level_payload(profile, href, payload):
    if (
        not any(profile is item for item in Q800F_LEVELS.values())
        or href != profile.href
    ):
        return False
    if not isinstance(payload, dict) or set(payload) != {profile.property}:
        return False
    value = payload[profile.property]
    if profile.wire_name is not None:
        if not isinstance(value, list) or len(value) != 1:
            return False
        row = value[0]
        if (
            not isinstance(row, dict)
            or set(row) != {"name", "value"}
            or row["name"] != profile.wire_name
        ):
            return False
        value = row["value"]
    if type(value) is not int:
        return False
    try:
        validate_level(profile, value)
    except ValueError:
        return False
    return True
