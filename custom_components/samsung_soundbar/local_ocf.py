"""Serialized async adapter over smartthings-local's authenticated OCF transport."""

from __future__ import annotations

import asyncio
import base64
import io
import logging
from dataclasses import dataclass
from functools import partial
from time import monotonic
from types import SimpleNamespace
from uuid import UUID

import cbor2

from .ocf_credentials import OcfCredential, async_complete
from .ocf_profiles import Q800F_WOOFER, validate_woofer_level

_BASE = "/sec/networkaudio/"
READ_HREFS = frozenset(
    _BASE + name
    for name in (
        "woofer",
        "advancedaudio",
        "spacefitSound",
        "activeVoiceAmplifier",
        "tone",
        "eq",
        "currentEQMode",
        "channelVolume",
        "surroundspeaker",
        "audiosync",
        "symphony",
        "audioPrompt",
        "av/autoPowerDown",
        "autoUpdate",
        "passThrough",
        "installationType",
        "switch/binary",
        "audio",
        "mode",
        "soundmode",
        "soundFrom",
        "playback",
        "musicinfo",
        "av/audioTrackData",
        "av/mediaPlayback",
        "av/mediaTrackControl",
        "av/mediaPlaybackShuffle",
        "av/mediaPlaybackRepeat",
        "feature",
        "speakerStatus",
        "av/support",
    )
)
_WOOFER = _BASE + "woofer"
_FIELD = "x.com.samsung.networkaudio.woofer"
_LOGGER = logging.getLogger(__name__)


class OcfError(Exception):
    def __init__(self, kind="transport"):
        self.kind = (
            kind
            if kind
            in {
                "transport",
                "auth",
                "identity",
                "permission",
                "unsupported",
                "malformed",
                "backoff",
                "closed",
                "unverified",
            }
            else "transport"
        )
        super().__init__(f"Local OCF operation failed ({self.kind})")


@dataclass(frozen=True)
class OcfWriteResult:
    acknowledged: bool
    readback_matches: bool | None
    ambiguous: bool
    code: int | None


def _load_protocol():
    from smartthings_local.protocol.auth import PskAuth
    from smartthings_local.protocol.dtls_session import (
        ConnectCancellation,
        DtlsCoapSession,
    )
    from smartthings_local.protocol.ocf_discovery import discover_ocf_secure_ports

    return SimpleNamespace(
        discover=discover_ocf_secure_ports,
        auth=PskAuth,
        session=DtlsCoapSession,
        cancellation=ConnectCancellation,
    )


def _decode(body):
    try:
        if not isinstance(body, bytes) or not 0 < len(body) <= 65536:
            raise OcfError("malformed")
        stream = io.BytesIO(body)
        value = cbor2.CBORDecoder(
            stream, max_depth=32, allow_duplicate_keys=False, strict_hashable_types=True
        ).decode()
        if not isinstance(value, dict) or stream.tell() != len(body):
            raise OcfError("malformed")
        return value
    except (cbor2.CBORDecodeError, ValueError, TypeError, RecursionError):
        raise OcfError("malformed") from None


class _PrivateProtocolLogs(logging.Filter):
    def __init__(self, credential):
        super().__init__()
        self._values = (
            credential.psk.hex(),
            base64.b64encode(credential.psk).decode(),
            repr(credential.psk),
            str(credential.client_identity_uuid),
            str(credential.target_device_uuid),
            credential.client_identity_uuid.hex,
            credential.target_device_uuid.hex,
        )
        self._values += tuple(value.upper() for value in self._values)

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - malformed backend logging must not expose raw args
            message = "OCF protocol message [redacted]"
        for value in self._values:
            message = message.replace(value, "[redacted]")
        record.msg, record.args = message, ()
        record.exc_info = record.exc_text = None
        record.stack_info = None
        return True


class LocalSoundbarOcfClient:
    def __init__(
        self,
        hass,
        host: str,
        credential: OcfCredential,
        *,
        clock=monotonic,
        on_error=None,
    ):
        self.hass, self.host = hass, host
        self._credential = credential
        self._clock = clock
        self._lock = asyncio.Lock()
        self._session = self._cancel = None
        self._closed = False
        self._next_retry = 0.0
        self._failures = 0
        self._filter = _PrivateProtocolLogs(credential)
        self._filtered = []
        self.available = False
        self.validated = False
        self.last_error = None
        self.on_error = on_error

    def _filters(self):
        for name in tuple(logging.Logger.manager.loggerDict):
            if name.startswith("smartthings_local"):
                logger = logging.getLogger(name)
                if logger not in self._filtered:
                    logger.addFilter(self._filter)
                    self._filtered.append(logger)

    def _disconnect(self):
        session, self._session = self._session, None
        self.available = False
        try:
            if session is not None:
                try:
                    session.close()
                except Exception:  # noqa: BLE001 - cleanup never exposes backend exceptions
                    _LOGGER.debug("OCF session cleanup failed")
        finally:
            for logger in self._filtered:
                logger.removeFilter(self._filter)
            self._filtered.clear()

    def _connect(self):
        if self._session is not None:
            if not self.available:
                raise OcfError("identity")
            return
        protocol = _load_protocol()
        self._filters()
        ports = protocol.discover(
            self.host, timeout=3, retries=1, order="directory-first"
        ).ports
        if len(ports) != 1 or type(ports[0]) is not int or not 1 <= ports[0] <= 65535:
            raise OcfError("transport")
        self._cancel = protocol.cancellation()
        if getattr(self, "_cancel_requested", False):
            self._cancel.set()
        self._session = protocol.session(
            self.host,
            ports[0],
            auth=protocol.auth(
                identity=self._credential.client_identity_uuid.bytes,
                key=self._credential.psk,
            ),
            write_max_attempts=1,
            rate_limit_rps=2,
        )
        self._session.connect(timeout=12, cancel=self._cancel)
        self._filters()
        self._session.start_reader()
        code, body = self._session.get(["oic", "d"], timeout=6)
        if code in (129, 131):
            raise OcfError("auth")
        if code != 69:
            raise OcfError("transport")
        try:
            matched = (
                UUID(_decode(body).get("di")) == self._credential.target_device_uuid
            )
        except (ValueError, TypeError, AttributeError):
            matched = False
        if not matched:
            raise OcfError("identity")
        self.available = True
        self.validated = True
        self.last_error = None
        self._failures = 0

    def _read(self, href):
        self._connect()
        code, body = self._session.get(href.strip("/").split("/"), timeout=6)
        if code != 69:
            raise OcfError("permission" if code in (129, 131) else "unsupported")
        return _decode(body)

    def _write(self, href, payload):
        self._connect()
        code, ambiguous = None, False
        try:
            code, _ = self._session.post(
                href.strip("/").split("/"), cbor2.dumps(payload), timeout=6
            )
        except Exception:  # noqa: BLE001 - outcome unknown; never resend a write
            ambiguous = True
        acknowledged = type(code) is int and 64 <= code < 96
        match = None
        if acknowledged or ambiguous:
            try:
                readback = self._read(href)
                match = all(
                    type(readback.get(key)) is type(value)
                    and readback.get(key) == value
                    for key, value in payload.items()
                )
            except Exception:  # noqa: BLE001 - failed readback does not authorize a replay
                _LOGGER.debug("OCF write readback unavailable")
                self.last_error = "transport"
                self._disconnect()
        return OcfWriteResult(acknowledged, match, ambiguous, code)

    async def _run(self, action):
        def perform():
            try:
                return True, action()
            except Exception as error:  # noqa: BLE001 - no private exception escapes a cancelled worker
                kind = (
                    error.kind
                    if isinstance(error, OcfError)
                    else "auth"
                    if getattr(error, "code", None) == "authentication"
                    else "transport"
                )
                return False, kind

        self._cancel_requested = False

        def cancel_connect():
            self._cancel_requested = True
            if self._cancel is not None:
                self._cancel.set()

        try:
            (success, result), cancelled = await async_complete(
                self.hass.async_add_executor_job(perform), on_cancel=cancel_connect
            )
            if cancelled:
                await async_complete(self.hass.async_add_executor_job(self._disconnect))
                raise asyncio.CancelledError
            if not success:
                raise OcfError(result)
            if self.available and self.on_error is not None:
                self.on_error(None)
            return result
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - normalize all transport/backend errors
            kind = (
                error.kind
                if isinstance(error, OcfError)
                else "auth"
                if getattr(error, "code", None) == "authentication"
                else "transport"
            )
            self.last_error = kind
            if self.on_error is not None:
                self.on_error(kind)
            if kind not in ("permission", "unsupported"):
                _, cancelled = await async_complete(
                    self.hass.async_add_executor_job(self._disconnect)
                )
                self._failures += 1
                self._next_retry = self._clock() + min(60, 2 ** min(self._failures, 6))
                if cancelled:
                    raise asyncio.CancelledError
            raise OcfError(kind) from None

    def _ready(self):
        if self._closed:
            raise OcfError("closed")
        if self._session is None and self._clock() < self._next_retry:
            raise OcfError("backoff")

    async def async_connect(self):
        async with self._lock:
            self._ready()
            await self._run(self._connect)

    async def async_read(self, href):
        if href not in READ_HREFS:
            raise OcfError("unverified")
        async with self._lock:
            self._ready()
            return await self._run(partial(self._read, href))

    async def async_write(self, href, payload, *, woofer_profile=None):
        # Wider writes need the model-scoped profile granted by the verified route.
        if (
            href != _WOOFER
            or not isinstance(payload, dict)
            or set(payload) != {_FIELD}
            or type(payload[_FIELD]) is not int
            or (woofer_profile is not Q800F_WOOFER and payload[_FIELD] not in (0, 1))
        ):
            raise OcfError("unverified")
        try:
            validate_woofer_level(payload[_FIELD])
        except ValueError:
            raise OcfError("unverified") from None
        payload = dict(payload)
        async with self._lock:
            self._ready()
            return await self._run(partial(self._write, href, payload))

    async def async_validate_candidate(self, credential):
        async with self._lock:
            if self._closed:
                raise OcfError("closed")
            previous, had_session = self._credential, self._session is not None
            previous_handler, self.on_error = self.on_error, None
            try:
                await self._run(self._disconnect)
                self._credential, self._filter = (
                    credential,
                    _PrivateProtocolLogs(credential),
                )
                await self._run(self._connect)
            finally:
                try:
                    await self._run(self._disconnect)
                finally:
                    self._credential, self._filter = (
                        previous,
                        _PrivateProtocolLogs(previous),
                    )
                    self._next_retry = 0
                    try:
                        if had_session and not self._closed:
                            try:
                                await self._run(self._connect)
                            except OcfError:
                                pass
                    finally:
                        self.on_error = previous_handler

    async def async_close(self):
        self._closed = True
        if self._cancel is not None:
            self._cancel.set()
        async with self._lock:
            await self._run(self._disconnect)

    def diagnostics(self):
        return {
            "configured": True,
            "validated": self.validated,
            "available": self.available,
            "last_error": self.last_error,
            "retry_in_seconds": max(0, round(self._next_retry - self._clock())),
        }
