from __future__ import annotations

import asyncio
import json
from typing import Any

from aiohttp import ClientError, ClientResponseError, ClientSession


DEFAULT_LOCAL_RPC_PORT = 1516
DEFAULT_LOCAL_RPC_TIMEOUT = 8
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


class LocalRpcAuthError(LocalRpcError):
    """Raised when the local soundbar rejects the access token."""


class LocalSoundbarRpcClient:
    """Small diagnostic client for Samsung soundbar local JSON-RPC."""

    def __init__(
        self,
        host: str,
        session: ClientSession,
        *,
        port: int = DEFAULT_LOCAL_RPC_PORT,
        verify_ssl: bool = False,
        timeout: int | float = DEFAULT_LOCAL_RPC_TIMEOUT,
    ) -> None:
        self._url = f"https://{host}:{port}/"
        self._session = session
        self._verify_ssl = verify_ssl
        self._timeout = timeout
        self._token: str | None = None
        self._token_lock = asyncio.Lock()
        self._request_id = 0

    @property
    def token_length(self) -> int | None:
        return len(self._token) if self._token is not None else None

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_payload = json.dumps(payload, separators=(",", ":"))
        try:
            async with asyncio.timeout(self._timeout):
                response = await self._session.post(
                    self._url,
                    data=raw_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    ssl=self._verify_ssl,
                )
                response.raise_for_status()
                data = await response.json(content_type=None)
        except ClientResponseError as err:
            raise LocalRpcError(
                f"HTTP {err.status}: {err.message or 'request failed'}"
            ) from err
        except (ClientError, asyncio.TimeoutError) as err:
            raise LocalRpcError(str(err)) from err
        except json.JSONDecodeError as err:
            raise LocalRpcError("response is not valid JSON") from err

        if not isinstance(data, dict):
            raise LocalRpcError(f"unexpected response type: {type(data).__name__}")

        if "error" in data:
            error = data["error"]
            message = self._format_error(error)
            if "token" in message.lower() or "auth" in message.lower():
                raise LocalRpcAuthError(message)
            raise LocalRpcError(message)

        result = data.get("result")
        if isinstance(result, dict):
            return result
        return {"value": result}

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
            await self.create_token()
            request_params["AccessToken"] = self._token
            return await self._post(self._payload(method, request_params))

    async def power_on(self) -> None:
        await self.call("powerControl", {"power": "powerOn"})

    async def power_off(self) -> None:
        await self.call("powerControl", {"power": "powerOff"})

    async def remote_key(self, remote_key: str) -> None:
        await self.call("remoteKeyControl", {"remoteKey": remote_key})

    async def volume_up(self) -> None:
        await self.remote_key("VOL_UP")

    async def volume_down(self) -> None:
        await self.remote_key("VOL_DOWN")

    async def mute_toggle(self) -> None:
        await self.remote_key("MUTE")

    async def set_volume(self, level: int) -> None:
        if not 0 <= level <= 100:
            raise ValueError("Volume has to be in range 0-100")

        try:
            await self.call("setVolume", {"volume": level})
            return
        except LocalRpcError:
            pass

        current = await self.volume()
        while current != level:
            if current < level:
                await self.volume_up()
                current += 1
            else:
                await self.volume_down()
                current -= 1

    async def select_input(self, source: str) -> None:
        await self.call("inputSelectControl", {"inputSource": source})

    async def set_sound_mode(self, sound_mode: str) -> None:
        await self.call("soundModeControl", {"soundMode": sound_mode})

    async def power_state(self) -> str | None:
        value = (await self.call("powerControl")).get("power")
        return str(value) if value is not None else None

    async def volume(self) -> int:
        value = (await self.call("getVolume")).get("volume")
        return int(value)

    async def is_muted(self) -> bool:
        return bool((await self.call("getMute")).get("mute"))

    async def input_source(self) -> str | None:
        value = (await self.call("inputSelectControl")).get("inputSource")
        return str(value) if value is not None else None

    async def sound_mode(self) -> str | None:
        value = (await self.call("soundModeControl")).get("soundMode")
        return str(value) if value is not None else None

    async def codec(self) -> str | None:
        value = (await self.call("getCodec")).get("codec")
        return str(value) if value is not None else None

    async def identifier(self) -> str | None:
        value = (await self.call("getIdentifier")).get("identifier")
        return str(value) if value is not None else None

    async def status(self) -> dict[str, Any]:
        power, volume, mute, source, sound_mode, codec, identifier = (
            await asyncio.gather(
                self.power_state(),
                self.volume(),
                self.is_muted(),
                self.input_source(),
                self.sound_mode(),
                self.codec(),
                self.identifier(),
            )
        )
        return {
            "power": power,
            "volume": volume,
            "mute": mute,
            "input_source": source,
            "sound_mode": sound_mode,
            "codec": codec,
            "identifier": identifier,
        }

    @staticmethod
    def _redact_token(token: str) -> str:
        if len(token) <= 8:
            return "***"
        return f"{token[:4]}...{token[-4:]}"
