from __future__ import annotations

import asyncio
import json
from typing import Any

from aiohttp import ClientError, ClientResponseError, ClientSession

DEFAULT_LOCAL_RPC_PORT = 1516
DEFAULT_LOCAL_RPC_TIMEOUT = 8
MAX_VOLUME_COMMANDS = 200
MAX_VOLUME_UNCHANGED_READBACKS = 3
VOLUME_ADJUSTMENT_TIMEOUT = 60
DEFAULT_LOCAL_RPC_METHODS = (
    "powerControl",
    "getVolume",
    "getMute",
    "inputSelectControl",
    "soundModeControl",
    "getCodec",
    "getIdentifier",
)
LOCAL_SOUND_MODE_VALUES = (
    "STANDARD",
    "SURROUND",
    "GAME",
    "ADAPTIVE",
)


class LocalRpcError(Exception):
    """Raised when the local soundbar JSON-RPC API fails."""

    def __init__(self, message: str, *, code: int | None = None):
        super().__init__(message)
        self.code = code


class LocalRpcAuthError(LocalRpcError):
    """Raised when the local soundbar rejects the access token."""


class LocalRpcCommandError(LocalRpcError):
    """Raised when a reachable soundbar rejects or cannot complete a command."""


class LocalRpcMethodNotFoundError(LocalRpcCommandError):
    """Raised for the JSON-RPC method-not-found response (-32601)."""


class LocalRpcParseError(LocalRpcCommandError):
    """Parse error is not proof of rejected authentication on every model."""


class LocalSoundbarRpcClient:
    """Samsung soundbar local JSON-RPC with serialized requests and readback."""

    def __init__(
        self,
        host: str,
        session: ClientSession,
        *,
        port: int = DEFAULT_LOCAL_RPC_PORT,
        verify_ssl: bool = False,
        timeout: float = DEFAULT_LOCAL_RPC_TIMEOUT,
    ) -> None:
        self._url = f"https://{host}:{port}/"
        self._session = session
        self._verify_ssl = verify_ssl
        self._timeout = timeout
        self._token: str | None = None
        self._token_lock = asyncio.Lock()
        self._call_lock = asyncio.Lock()
        self._volume_lock = asyncio.Lock()
        self._request_id = 0
        self._identifier: str | None = None
        self._identifier_confirmations = 0

    @property
    def token_length(self) -> int | None:
        return len(self._token) if self._token is not None else None

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_payload = json.dumps(payload, separators=(",", ":"))
        try:
            async with asyncio.timeout(self._timeout):
                async with self._session.post(
                    self._url,
                    data=raw_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    ssl=self._verify_ssl,
                ) as response:
                    response.raise_for_status()
                    data = await response.json(content_type=None)
        except ClientResponseError as err:
            error_type = (
                LocalRpcAuthError if err.status in (401, 403) else LocalRpcError
            )
            raise error_type(
                self._sanitize_error(
                    f"HTTP {err.status}: {err.message or 'request failed'}", payload
                )
            ) from err
        except (ClientError, asyncio.TimeoutError) as err:
            raise LocalRpcError(
                self._sanitize_error(str(err) or "request timed out", payload)
            ) from err
        except json.JSONDecodeError as err:
            raise LocalRpcError("response is not valid JSON") from err

        if not isinstance(data, dict):
            raise LocalRpcError(f"unexpected response type: {type(data).__name__}")

        if "error" in data or ("result" not in data and "code" in data):
            error = data.get("error", data)
            message = self._sanitize_error(self._format_error(error), payload)
            code = error.get("code") if isinstance(error, dict) else None
            try:
                code = int(code) if code is not None else None
            except (ValueError, TypeError):
                code = None
            if code == -32601:
                raise LocalRpcMethodNotFoundError(message, code=code)
            if code == -32700:
                raise LocalRpcParseError(message, code=code)
            if "token" in message.lower() or "auth" in message.lower():
                raise LocalRpcAuthError(message, code=code)
            raise LocalRpcCommandError(message, code=code)

        if "result" not in data:
            raise LocalRpcError("response has neither result nor a JSON-RPC error")

        result = data.get("result")
        if isinstance(result, dict):
            if result.get("success") is False:
                raise LocalRpcCommandError("command returned success: false")
            return result
        return {"value": result}

    def _sanitize_error(self, message: str, payload: dict[str, Any]) -> str:
        for token in (self._token, payload.get("params", {}).get("AccessToken")):
            if isinstance(token, str) and token:
                message = message.replace(token, "***")
        return message

    @staticmethod
    def _format_error(error: Any) -> str:
        if isinstance(error, dict):
            message = error.get("message") or error.get("error") or str(error)
            code = error.get("code")
            return f"{code}: {message}" if code is not None else str(message)
        return str(error)

    def _payload(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._request_id += 1
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "id": self._request_id,
        }
        if params:
            payload["params"] = params
        return payload

    async def create_token(self) -> str:
        async with self._token_lock:
            result = await self._post(self._payload("createAccessToken"))
            token = result.get("AccessToken")
            if not isinstance(token, str) or not token:
                raise LocalRpcError("createAccessToken did not return AccessToken")
            self._token = token
            return token

    async def _ensure_token(self) -> None:
        if self._token is not None:
            return
        await self.create_token()

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        """Call one RPC method at a time.

        Samsung soundbars can refuse connections when several JSON-RPC calls
        are issued concurrently. Status collection uses asyncio.gather(), so
        serialization belongs here rather than at each caller.
        """
        async with self._call_lock:
            return await self._call(method, params, authenticated=authenticated)

    async def _call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        if method == "createAccessToken":
            token = await self.create_token()
            return {"AccessToken": self._redact_token(token)}

        request_params = dict(params or {})
        if authenticated:
            await self._ensure_token()
            request_params.setdefault("AccessToken", self._token)

        try:
            return await self._post(self._payload(method, request_params))
        except LocalRpcAuthError:
            if not authenticated:
                raise
            self._token = None
            await self.create_token()
            request_params["AccessToken"] = self._token
            try:
                return await self._post(self._payload(method, request_params))
            except LocalRpcAuthError:
                self._token = None
                raise

    async def power_on(self) -> None:
        await self.call("powerControl", {"power": "powerOn"})

    async def power_off(self) -> None:
        await self.call("powerControl", {"power": "powerOff"})

    async def remote_key(self, remote_key: str) -> None:
        await self.call("remoteKeyControl", {"remoteKey": remote_key})

    async def volume_up(self) -> None:
        async with self._volume_lock:
            await self.remote_key("VOL_UP")

    async def volume_down(self) -> None:
        async with self._volume_lock:
            await self.remote_key("VOL_DOWN")

    async def mute_toggle(self) -> None:
        await self.remote_key("MUTE")

    async def set_volume(self, level: int) -> None:
        if (
            isinstance(level, bool)
            or not isinstance(level, int)
            or not 0 <= level <= 100
        ):
            raise ValueError("Volume has to be in range 0-100")

        async with self._volume_lock:
            try:
                await self.call("setVolume", {"volume": level})
                return
            except LocalRpcMethodNotFoundError:
                pass

            try:
                async with asyncio.timeout(VOLUME_ADJUSTMENT_TIMEOUT):
                    await self._adjust_volume(level)
            except TimeoutError as err:
                raise LocalRpcCommandError("volume adjustment timed out") from err

    async def _adjust_volume(self, level: int) -> None:
        # Hold the volume transaction lock, but acquire the RPC lock per request
        # so other state reads can continue without observing an intermediate volume.
        current = await self._read_volume()
        unchanged = 0
        for _ in range(MAX_VOLUME_COMMANDS):
            if current == level:
                return
            await self.remote_key("VOL_UP" if current < level else "VOL_DOWN")
            readback = await self._read_volume()
            unchanged = unchanged + 1 if readback == current else 0
            current = readback
            if unchanged >= MAX_VOLUME_UNCHANGED_READBACKS:
                raise LocalRpcCommandError(
                    "volume did not change after repeated commands"
                )
        if current != level:
            raise LocalRpcCommandError("volume adjustment exceeded the command limit")

    async def select_input(self, source: str) -> None:
        await self.call("inputSelectControl", {"inputSource": source})

    async def set_sound_mode(self, sound_mode: str) -> None:
        await self.call("soundModeControl", {"soundMode": sound_mode})

    async def power_state(self) -> str | None:
        value = (await self.call("powerControl")).get("power")
        return str(value) if value is not None else None

    async def volume(self) -> int:
        async with self._volume_lock:
            return await self._read_volume()

    async def _read_volume(self) -> int:
        value = (await self.call("getVolume")).get("volume")
        return self.parse_volume(value)

    @staticmethod
    def parse_volume(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise LocalRpcError("getVolume did not return a valid volume")
        try:
            volume = int(value)
        except (TypeError, ValueError) as err:
            raise LocalRpcError("getVolume did not return a valid volume") from err
        if not 0 <= volume <= 100:
            raise LocalRpcError("getVolume returned a volume outside 0-100")
        return volume

    async def is_muted(self) -> bool:
        value = (await self.call("getMute")).get("mute")
        return self.parse_mute(value)

    @staticmethod
    def parse_mute(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.lower() in ("true", "false", "0", "1"):
            return value.lower() in ("true", "1")
        raise LocalRpcError("getMute did not return a valid mute state")

    async def input_source(self) -> str | None:
        value = (await self.call("inputSelectControl")).get("inputSource")
        return str(value) if value is not None else None

    async def sound_mode(self) -> str | None:
        value = (await self.call("soundModeControl")).get("soundMode")
        return str(value) if value is not None else None

    async def codec(self) -> str | None:
        value = (await self.call("getCodec")).get("codec")
        return str(value) if value is not None else None

    async def identifier(self, *, use_cache: bool = False) -> str | None:
        if use_cache and self._identifier_confirmations >= 2:
            return self._identifier
        value = (await self.call("getIdentifier")).get("identifier")
        if not isinstance(value, str) or not value.strip():
            self._identifier_confirmations = 0
            return None
        self._identifier_confirmations = (
            self._identifier_confirmations + 1 if value == self._identifier else 1
        )
        self._identifier = value
        return value

    async def status(self) -> dict[str, Any]:
        values = await asyncio.gather(
            self.power_state(),
            self.volume(),
            self.is_muted(),
            self.input_source(),
            self.sound_mode(),
            self.codec(),
            self.identifier(use_cache=True),
            return_exceptions=True,
        )
        keys = (
            "power",
            "volume",
            "mute",
            "input_source",
            "sound_mode",
            "codec",
            "identifier",
        )
        status = {}
        for key, value in zip(keys, values, strict=True):
            if key in ("codec", "identifier") and isinstance(
                value, LocalRpcMethodNotFoundError
            ):
                value = None
            if isinstance(value, BaseException):
                raise value
            status[key] = value
        return status

    @staticmethod
    def _redact_token(token: str) -> str:
        if len(token) <= 8:
            return "***"
        return f"{token[:4]}...{token[-4:]}"
