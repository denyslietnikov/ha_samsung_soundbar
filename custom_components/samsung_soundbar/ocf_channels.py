"""Pure read-only channel parsing; no writable profile, range, or unit claims."""

from dataclasses import dataclass
from types import MappingProxyType

PREFIX = "x.com.samsung.networkaudio."
CHANNEL_RAW_NAMES = MappingProxyType(
    {
        "center": "Spk_Center",
        "side": "Spk_Side",
        "wide": "Spk_Wide",
        "frontTop": "Spk_Front_Top",
        "rear": "Spk_Rear",
        "rearTop": "Spk_Rear_Top",
        "rearSide": "Spk_Rear_Side",
    }
)
RAW_CHANNEL_NAMES = MappingProxyType(
    {raw: key for key, raw in CHANNEL_RAW_NAMES.items()}
)
CHANNEL_NAMES = frozenset(CHANNEL_RAW_NAMES) | {"front", "top"}


@dataclass(frozen=True)
class ChannelProjection:
    """Only allowlisted shared-state fields, never raw device rows."""

    values: dict[str, int | bool]
    invalidated: tuple[str, ...] = ()


def parse_channel_volume(payload: dict) -> ChannelProjection:
    """Project an authoritative GET, invalidating unobserved known fields.

    Legacy names are read-compatible only in homogeneous legacy lists and never
    grant schema validity. Unknown typed unique rows are allowed, not exported;
    callers needing raw preflight data must retain the original payload.
    """
    values = {"channel_schema_valid": False}
    observation_fields = tuple(
        sorted(
            prefix + name
            for name in CHANNEL_NAMES
            for prefix in ("channel_level_", "channel_status_")
        )
    )
    support = payload.get(PREFIX + "isSupported", payload.get("isSupported"))
    if type(support) is not int or support != 1:
        return ChannelProjection(values, observation_fields)
    rows = payload.get(PREFIX + "channelVolume")
    if not isinstance(rows, list) or not 0 < len(rows) <= 32:
        return ChannelProjection(values, observation_fields)

    has_actual_names = any(
        isinstance(row, dict)
        and isinstance(row.get("name"), str)
        and row["name"] in RAW_CHANNEL_NAMES
        for row in rows
    )
    valid = True
    seen_names = set()
    seen_channels = set()
    invalid_channels = set()
    observations = {}
    for row in rows:
        if not isinstance(row, dict):
            valid = False
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name:
            valid = False
            continue
        legacy = name in CHANNEL_NAMES
        canonical = name if legacy else RAW_CHANNEL_NAMES.get(name)
        if name in seen_names:
            valid = False
        seen_names.add(name)
        if canonical is not None:
            if canonical in seen_channels:
                valid = False
                invalid_channels.add(canonical)
            seen_channels.add(canonical)
        if legacy:
            valid = False
            if has_actual_names:
                invalid_channels.add(canonical)
        value, status = row.get("value"), row.get("status")
        if (
            type(value) is not int
            or type(status) is not int
            or status not in (-1, 0, 1)
        ):
            valid = False
            if canonical is not None:
                invalid_channels.add(canonical)
            continue
        if canonical is not None:
            observations[canonical] = (value, status)

    for canonical, (value, status) in observations.items():
        if canonical in invalid_channels:
            continue
        values["channel_status_" + canonical] = status
        if status == 1:
            values["channel_level_" + canonical] = value
    values["channel_schema_valid"] = valid
    return ChannelProjection(
        values, tuple(field for field in observation_fields if field not in values)
    )
