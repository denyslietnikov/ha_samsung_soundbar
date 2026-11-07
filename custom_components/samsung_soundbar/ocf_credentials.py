"""Private per-entry OCF credentials; no credentials in config entries or logs."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from homeassistant.helpers.storage import Store

MAX_BUNDLE_BYTES = 65536
_FIELDS = {
    "schema_version",
    "target_device_uuid",
    "client_identity_uuid",
    "psk_encoding",
    "psk",
}
_UNSET = object()


class OcfCredentialError(ValueError):
    def __init__(self):
        super().__init__("OCF credential data or storage could not be validated")


@dataclass(frozen=True, slots=True, repr=False)
class OcfCredential:
    target_device_uuid: UUID
    client_identity_uuid: UUID
    psk: bytes

    def __post_init__(self):
        if (
            not isinstance(self.target_device_uuid, UUID)
            or not self.target_device_uuid.int
            or not isinstance(self.client_identity_uuid, UUID)
            or not self.client_identity_uuid.int
            or not isinstance(self.psk, bytes)
            or len(self.psk) not in (16, 32)
        ):
            raise OcfCredentialError

    def to_storage(self):
        return {
            "schema_version": 1,
            "target_device_uuid": str(self.target_device_uuid),
            "client_identity_uuid": str(self.client_identity_uuid),
            "psk_encoding": "base64",
            "psk": base64.b64encode(self.psk).decode("ascii"),
        }


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise OcfCredentialError
        result[key] = value
    return result


def parse_credential_bundle(content: str | bytes) -> OcfCredential:
    try:
        if (
            not isinstance(content, (str, bytes))
            or not 0 < len(content) <= MAX_BUNDLE_BYTES
        ):
            raise OcfCredentialError
        value = json.loads(content, object_pairs_hook=_unique_fields)
        if not isinstance(value, dict) or set(value) != _FIELDS:
            raise OcfCredentialError
        if (
            type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or value["psk_encoding"] != "base64"
        ):
            raise OcfCredentialError
        if not all(
            isinstance(value[key], str)
            for key in ("target_device_uuid", "client_identity_uuid", "psk")
        ):
            raise OcfCredentialError
        target, client = (
            UUID(value["target_device_uuid"]),
            UUID(value["client_identity_uuid"]),
        )
        key = base64.b64decode(value["psk"], validate=True)
        if not target.int or not client.int or len(key) not in (16, 32):
            raise OcfCredentialError
        return OcfCredential(target, client, key)
    except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError):
        raise OcfCredentialError from None


async def async_complete(awaitable, *, on_cancel=None):
    """Drain an owned operation despite repeated caller cancellation."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
            if on_cancel is not None:
                on_cancel()


class OcfCredentialStore:
    def __init__(self, hass, entry_id: str):
        if not isinstance(entry_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", entry_id
        ):
            raise OcfCredentialError
        self.hass, self.entry_id = hass, entry_id
        key = f"samsung_soundbar.ocf.{entry_id}"
        self.store = Store(hass, 1, key, private=True, atomic_writes=True)
        self.rollback_store = Store(
            hass, 1, key + ".rollback", private=True, atomic_writes=True
        )
        locks = hass.data.setdefault("samsung_soundbar_ocf_locks", {})
        self._lock, self.configuration_lock = locks.setdefault(
            entry_id, (asyncio.Lock(), asyncio.Lock())
        )

    async def async_load(self):
        async with self._lock:
            return await self._load()

    async def _load(self):
        try:
            pending = await self.rollback_store.async_load()
            if pending is not None:
                if not isinstance(pending, dict) or set(pending) != {"previous"}:
                    raise OcfCredentialError
                previous = (
                    None
                    if pending["previous"] is None
                    else parse_credential_bundle(json.dumps(pending["previous"]))
                )
                restored, cancelled = await async_complete(self._restore(previous))
                if cancelled:
                    raise asyncio.CancelledError
                if not restored:
                    raise OcfCredentialError
            value = await self.store.async_load()
            return None if value is None else parse_credential_bundle(json.dumps(value))
        except Exception:  # noqa: BLE001 - never expose secret-bearing storage errors
            raise OcfCredentialError from None

    def _current(self, guard):
        return (
            not self.hass.is_stopping
            and self.hass.config_entries.async_get_entry(self.entry_id) is not None
            and (guard is None or guard())
        )

    async def async_save(self, credential, *, expected=_UNSET, guard=None):
        credential = parse_credential_bundle(json.dumps(credential.to_storage()))
        async with self._lock:
            previous = await self._load()
            if expected is not _UNSET and previous != expected:
                raise OcfCredentialError
            if not self._current(guard):
                raise OcfCredentialError
            rollback = {"previous": None if previous is None else previous.to_storage()}
            prepared, cancelled = await async_complete(
                self._write_verified(self.rollback_store, rollback)
            )
            if not prepared or cancelled or not self._current(guard):
                await async_complete(self._remove_verified(self.rollback_store))
                if cancelled:
                    raise asyncio.CancelledError
                raise OcfCredentialError
            written, cancelled = await async_complete(
                self._write_verified(self.store, credential.to_storage())
            )
            if not written or cancelled or not self._current(guard):
                _, rollback_cancelled = await async_complete(self._restore(previous))
                if cancelled or rollback_cancelled:
                    raise asyncio.CancelledError
                raise OcfCredentialError
            committed, cancelled = await async_complete(
                self._remove_verified(self.rollback_store)
            )
            if not committed or cancelled or not self._current(guard):
                # Preserve a recoverable old key even if rollback itself fails.
                await async_complete(
                    self._write_verified(self.rollback_store, rollback)
                )
                _, rollback_cancelled = await async_complete(self._restore(previous))
                if cancelled or rollback_cancelled:
                    raise asyncio.CancelledError
                raise OcfCredentialError

    async def _write_verified(self, store, data):
        try:
            await store.async_save(data)
            if store is self.store:
                observed = await self.hass.async_add_executor_job(self._read_persisted)
                return observed == parse_credential_bundle(json.dumps(data))
            observed = await self.hass.async_add_executor_job(
                lambda: self._read_store(store)
            )
            return observed == data
        except Exception:  # noqa: BLE001 - HA Store may log rather than raise write failures
            return False

    def _read_store(self, store):
        with Path(store.path).open("rb") as stream:
            raw = stream.read(MAX_BUNDLE_BYTES + 1)
        if len(raw) > MAX_BUNDLE_BYTES:
            raise OcfCredentialError
        value = json.loads(raw, object_pairs_hook=_unique_fields)
        if value.get("version") != 1 or value.get("key") != store.key:
            raise OcfCredentialError
        return value["data"]

    def _read_persisted(self):
        return parse_credential_bundle(json.dumps(self._read_store(self.store)))

    async def _remove_verified(self, store):
        try:
            await store.async_remove()
            return await self.hass.async_add_executor_job(
                lambda: not Path(store.path).exists()
            )
        except Exception:  # noqa: BLE001 - preserve the rollback record on failed cleanup
            return False

    async def _restore(self, previous):
        restored = (
            await self._remove_verified(self.store)
            if previous is None
            else await self._write_verified(self.store, previous.to_storage())
        )
        return restored and await self._remove_verified(self.rollback_store)

    async def async_remove(self, *, locked=False):
        if not locked:
            async with self.configuration_lock:
                return await self.async_remove(locked=True)
        async with self._lock:
            # Remove recovery state before the active record, so an acknowledged
            # Forget cannot resurrect a previous key during a future load.
            for store in (self.rollback_store, self.store):
                removed, cancelled = await async_complete(self._remove_verified(store))
                if not removed:
                    raise OcfCredentialError
                if cancelled:
                    # Finish both deletions before propagating cancellation.
                    for remaining in (self.rollback_store, self.store):
                        await async_complete(self._remove_verified(remaining))
                    raise asyncio.CancelledError
