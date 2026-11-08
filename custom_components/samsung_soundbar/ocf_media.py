"""Pure, bounded, allowlisted media diagnostics; no state or transport access."""

BASE = "/sec/networkaudio/"
PREFIX = "x.com.samsung.networkaudio."
MEDIA_HREFS = tuple(
    BASE + suffix
    for suffix in (
        "soundFrom",
        "playback",
        "musicinfo",
        "av/audioTrackData",
        "av/mediaPlayback",
        "av/mediaTrackControl",
        "av/mediaPlaybackShuffle",
        "av/mediaPlaybackRepeat",
    )
)

# Q800F reports STOPPED on AirPlay pause, but PAUSED on native Roon pause.
_STATUS = frozenset(
    {
        "play",
        "pause",
        "stop",
        "playing",
        "paused",
        "stopped",
        "idle",
        "PLAYING",
        "STOPPED",
        "PAUSED",
    }
)
_PLAYBACK_MODES = frozenset({"play", "pause", "stop", "fastforward", "rewind"})
_SHUFFLE = frozenset({"enabled", "disabled"})
_REPEAT = frozenset(
    {"none", "all", "one", "disabled", "enabled", "repeatall", "repeatone"}
)
_MISSING = object()
_INVALID = object()
_CONTROL_NAMES = (
    "play",
    "next",
    "prev",
    "skip_forward",
    "skip_backward",
    "shuffle",
    "repeat",
    "loop",
    "thumbs_up",
    "thumbs_down",
)

# Output names, exact protocol paths, and validators are all fixed, not device supplied.
_SCHEMAS = {
    "soundFrom": {
        "sbMode": ((PREFIX + "sbMode",), "integer", ()),
        "name": ((PREFIX + "name",), "presence", ()),
        "soundFrom.status": ((PREFIX + "soundFrom", "status"), "integer", ()),
        "soundFrom.deviceType": ((PREFIX + "soundFrom", "deviceType"), "integer", ()),
        "soundFrom.name": ((PREFIX + "soundFrom", "name"), "presence", ()),
        "soundFrom.connectionType": (
            (PREFIX + "soundFrom", "connectionType"),
            "presence",
            (),
        ),
    },
    "playback": {"status": ((PREFIX + "status",), "enum", _STATUS)},
    "musicinfo": {
        **{
            "control." + name: (("control", name), "presence", ())
            for name in _CONTROL_NAMES
        },
        "contentProvider.name": (("contentProvider", "name"), "presence", ()),
    },
    "av/audioTrackData": {
        **{
            name: ((name,), "presence", ())
            for name in (
                "title",
                "artist",
                "album",
                "thumbnail",
                "source",
                "trackUri",
            )
        },
        "duration": (("duration",), "integer", ()),
        "trackNumber": (("trackNumber",), "integer", ()),
    },
    "av/mediaPlayback": {
        "modes": (("modes",), "enum_list", _PLAYBACK_MODES),
        "supportedModes": (("supportedModes",), "enum_list", _PLAYBACK_MODES),
        "position": (("position",), "integer", ()),
        "index": (("index",), "integer", ()),
    },
    "av/mediaTrackControl": {},
    "av/mediaPlaybackShuffle": {"mode": (("mode",), "enum", _SHUFFLE)},
    "av/mediaPlaybackRepeat": {"mode": (("mode",), "enum", _REPEAT)},
}


def _lookup(payload, path):
    value = payload
    for key in path:
        if type(value) is not dict:
            return _INVALID
        if key not in value:
            return _MISSING
        value = value[key]
    return value


def _summarize(value, kind, allowed):
    if value is _MISSING:
        return {"state": "missing"}
    if value is _INVALID:
        return {"state": "invalid"}
    if kind == "integer":
        if type(value) is int and 0 <= value <= 2**31 - 1:
            return {"state": "value", "value": value}
        return {"state": "invalid"}
    if kind == "enum_list":
        if (
            type(value) is not list
            or len(value) > 8
            or any(type(item) is not str or len(item) > 4096 for item in value)
            or len(set(value)) != len(value)
        ):
            return {"state": "invalid"}
        if not value:
            return {"state": "empty"}
        if any(item not in allowed for item in value):
            return {"state": "unrecognized"}
        return {"state": "value", "value": list(value)}
    if type(value) is not str or len(value) > 4096:
        return {"state": "invalid"}
    if not value or (kind == "presence" and not value.strip()):
        return {"state": "empty"}
    if kind == "presence":
        return {"state": "present"}
    if value not in allowed:
        return {"state": "unrecognized"}
    return {"state": "value", "value": value}


def project_media_diagnostic(href: str, payload: dict) -> dict:
    """Return fixed-field summaries without private device strings or raw data."""
    if type(href) is not str or href not in MEDIA_HREFS:
        raise ValueError("Unknown media resource")
    if type(payload) is not dict:
        raise ValueError("Invalid media payload")
    resource = href.removeprefix(BASE)
    fields = {}
    for name, (path, kind, allowed) in _SCHEMAS[resource].items():
        value = _lookup(payload, path)
        fields[name] = _summarize(value, kind, allowed)
    return {"fields": fields}
