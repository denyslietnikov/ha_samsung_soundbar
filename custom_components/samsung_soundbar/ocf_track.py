"""Pure UI-only track text projection, separate from media diagnostics."""

from unicodedata import category

TRACK_HREF = "/sec/networkaudio/av/audioTrackData"
MEDIA_TEXT_FIELDS = frozenset({"media_title", "media_artist", "media_album_name"})
MEDIA_TEXT_TTL = 15.0
MEDIA_POLL_INTERVAL = 5.0


def _text(value):
    if (
        type(value) is not str
        or len(value) > 1024
        or any(category(character) in {"Cc", "Cs"} for character in value)
    ):
        return None
    return value.strip() or None


def project_track_text(payload) -> dict:
    """Return a full validated snapshot; an invalid title discards all text."""
    if type(payload) is not dict:
        return {}
    title = _text(payload.get("title"))
    if title is None:
        return {}
    values = {"media_title": title}
    for raw, field in (("artist", "media_artist"), ("album", "media_album_name")):
        value = _text(payload.get(raw))
        if value is not None:
            values[field] = value
    return values
