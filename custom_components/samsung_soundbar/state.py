"""Field-level state arbitration shared by all soundbar transports."""

from dataclasses import dataclass
from time import monotonic
from typing import Any

from .ocf_state import OCF_FIELDS
from .ocf_track import MEDIA_TEXT_FIELDS, MEDIA_TEXT_TTL

LOCAL_TTL = 120.0
STREAMING_TTL = 15.0
WRITE_SETTLE_TIME = 5.0
MEDIA_CONTEXT = {
    "power": "on",
    "input_source": "WIFI",
    "sound_from_detail_name": "AirPlay",
}
LOCAL_FIELDS = frozenset(
    {
        "power",
        "volume_level",
        "volume_muted",
        "input_source",
        "sound_mode",
        "sound_from_detail_name",
        "local_codec",
        "local_identifier",
    }
)


@dataclass(frozen=True)
class FieldValue:
    value: Any
    source: str
    observed_at: float
    ttl: float | None

    def fresh(self, now: float) -> bool:
        return self.ttl is None or now - self.observed_at <= self.ttl


@dataclass(frozen=True)
class StateSnapshot:
    values: dict[str, Any]
    available: bool
    local_available: bool
    ocf_available: bool = False
    ocf_unavailable_fields: frozenset[str] = frozenset()
    ocf_fresh_fields: frozenset[str] = frozenset()


class SoundbarState:
    """Merge partial reads without making omitted fields fresh again."""

    def __init__(self, *, local: bool = True, ocf: bool = False, clock=monotonic):
        self.use_local = local
        self.use_ocf = ocf and local
        self.clock = clock
        self.records: dict[str, dict[str, FieldValue]] = {
            "rpc": {},
            "ocf": {},
        }
        self.transport_available = {"rpc": False, "ocf": False}
        self.pending: dict[str, FieldValue] = {}
        self.write_only: dict[str, FieldValue] = {}
        self.written_at: dict[str, float] = {}
        self.write_sources: dict[str, str] = {}
        self.ocf_unavailable_fields: set[str] = set()
        self._media_generation = 0

    def clear_media(self):
        """Revoke every text observation and any in-flight context token."""
        self._media_generation += 1
        for key in MEDIA_TEXT_FIELDS:
            for values in self.records.values():
                values.pop(key, None)
            self.pending.pop(key, None)
            self.write_only.pop(key, None)
            self.written_at.pop(key, None)
            self.write_sources.pop(key, None)

    def media_context(self):
        """A generation token authorizes text, never playback or device writes."""
        if not (
            self.use_local
            and self.use_ocf
            and self.transport_available["rpc"]
            and self.transport_available["ocf"]
        ):
            return None
        now = self.clock()
        for key, expected in MEDIA_CONTEXT.items():
            record = self.records["rpc"].get(key)
            pending = self.pending.get(key)
            if (
                record is None
                or not record.fresh(now)
                or record.value != expected
                or record.observed_at < self.written_at.get(key, float("-inf"))
                or pending is not None
                and pending.fresh(now)
                or key in self.write_only
            ):
                return None
        return self._media_generation

    def _context_observations(self):
        now = self.clock()
        return tuple(
            record.value
            if (record := self.records["rpc"].get(key)) is not None
            and record.fresh(now)
            else None
            for key in MEDIA_CONTEXT
        )

    def merge(
        self, source: str, values: dict[str, Any], *, observed_at: float | None = None
    ):
        source = "rpc" if source == "local" else source
        if source not in self.records:
            raise ValueError("Unknown local transport")
        if (
            source == "rpc"
            and not self.use_local
            or source == "ocf"
            and not self.use_ocf
        ):
            return
        now = self.clock() if observed_at is None else observed_at
        previous_context = self._context_observations() if source == "rpc" else None
        for key, value in values.items():
            if (
                source not in self._sources(key)
                or value is None
                or now < self.written_at.get(key, float("-inf"))
            ):
                continue
            previous = self.records[source].get(key)
            if previous is not None and previous.observed_at > now:
                continue
            ttl = (
                None
                if source == "rpc" and key == "local_identifier"
                else MEDIA_TEXT_TTL
                if key in MEDIA_TEXT_FIELDS
                else STREAMING_TTL
                if key == "sound_from_detail_name"
                else LOCAL_TTL
            )
            self.records[source][key] = FieldValue(value, source, now, ttl)
            # Only the field owner or the write transport may confirm optimism.
            # The settling guard can still mask a contradictory read briefly.
            pending = self.pending.get(key)
            authoritative = source == self._sources(key)[
                0
            ] or source == self.write_sources.get(key)
            if not authoritative:
                continue
            self.write_only.pop(key, None)
            if pending is not None and pending.value == value and authoritative:
                self.pending.pop(key)
                self.write_only.pop(key, None)
            elif pending is None or not pending.fresh(now):
                self.write_only.pop(key, None)
        if source == "rpc" and previous_context != self._context_observations():
            self.clear_media()

    def expect(
        self,
        values: dict[str, Any],
        *,
        write_only: bool = False,
        source: str | None = None,
    ):
        now = self.clock()
        if source in (None, "rpc") and any(
            key in MEDIA_CONTEXT
            and value is not None
            and self._sources(key) == ("rpc",)
            for key, value in values.items()
        ):
            self.clear_media()
        for key, value in values.items():
            if (
                value is None
                or self._sources(key) != ("rpc",)
                or source not in (None, "rpc")
            ):
                continue
            self.written_at[key] = now
            self.write_sources[key] = source or self._sources(key)[0]
            self.pending[key] = FieldValue(value, "optimistic", now, WRITE_SETTLE_TIME)
            self.write_only.pop(key, None)
            if write_only:
                self.write_only[key] = FieldValue(value, "optimistic", now, None)

    def begin_write(self, fields, *, source):
        """Supersede older command guards without inventing a new field value."""
        fields = tuple(fields)
        if source == "rpc" and set(fields) & MEDIA_CONTEXT.keys():
            self.clear_media()
        now = self.clock()
        for key in fields:
            self.written_at[key] = now
            self.write_sources[key] = source
            self.pending.pop(key, None)
            self.write_only.pop(key, None)

    def invalidate(self, source, fields):
        fields = tuple(fields)
        if (source == "rpc" and set(fields) & MEDIA_CONTEXT.keys()) or (
            source == "ocf" and set(fields) & MEDIA_TEXT_FIELDS
        ):
            self.clear_media()
        for key in fields:
            self.records[source].pop(key, None)
            if source in self._sources(key):
                self.pending.pop(key, None)
                self.write_only.pop(key, None)

    def _sources(self, key):
        if key in OCF_FIELDS:
            return ("ocf",) if self.use_ocf else ()
        # Core fields belong to RPC; the three allowlisted text fields are OCF-only.
        return ("rpc",) if self.use_local else ()

    def resolve(self, key: str) -> FieldValue | None:
        if key in MEDIA_TEXT_FIELDS and (
            self.media_context() is None or key in self.ocf_unavailable_fields
        ):
            return None
        now = self.clock()
        pending = self.pending.get(key)
        if pending is not None and pending.fresh(now):
            return pending
        sources = self._sources(key)
        for source in sources:
            record = self.records[source].get(key)
            if (
                key in self.write_only
                and record is not None
                and record.observed_at < self.written_at[key]
            ):
                continue
            if record is not None and record.fresh(now):
                return record
        return self.write_only.get(key)

    def value(self, key: str, default=None):
        record = self.resolve(key)
        return record.value if record is not None else default

    def snapshot(self) -> StateSnapshot:
        keys = self._keys()
        values = {
            key: record.value
            for key in keys
            if (record := self.resolve(key)) is not None
        }
        local = self.use_local and self.transport_available["rpc"]
        ocf = self.use_ocf and self.transport_available["ocf"]
        return StateSnapshot(
            values=values,
            available=bool(local),
            local_available=bool(local),
            ocf_available=bool(ocf),
            ocf_unavailable_fields=frozenset(self.ocf_unavailable_fields),
            ocf_fresh_fields=frozenset(
                key
                for key, record in self.records["ocf"].items()
                if ocf
                and key in OCF_FIELDS
                and key not in self.ocf_unavailable_fields
                and record.fresh(self.clock())
            ),
        )

    def diagnostics(self) -> dict[str, Any]:
        now = self.clock()
        keys = self._keys()
        result = {}
        for key in keys:
            record = self.resolve(key)
            candidates = [
                values[key] for values in self.records.values() if key in values
            ]
            result[key] = {
                "source": record.source if record else None,
                "age_seconds": round(now - record.observed_at, 3) if record else None,
                "ttl_seconds": record.ttl if record else None,
                "pending_write": key in self.pending and self.pending[key].fresh(now),
                "readbacks": {
                    item.source: {
                        "age_seconds": round(now - item.observed_at, 3),
                        "ttl_seconds": item.ttl,
                        "stale": not item.fresh(now),
                    }
                    for item in candidates
                },
            }
        return result

    def _keys(self):
        return set().union(
            *(values.keys() for values in self.records.values()),
            self.pending,
            self.write_only,
        )
