"""Read stable, non-secret identity from the soundbar's local endpoints."""

from __future__ import annotations

import asyncio
import re
from xml.etree import ElementTree

import aiohttp


class LocalIdentityError(Exception):
    """No usable local identity could be read."""


def _normalize_mac(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    compact = re.sub(r"[:-]", "", value).lower()
    if not re.fullmatch(r"[0-9a-f]{12}", compact) or compact == "0" * 12:
        return None
    return ":".join(compact[index : index + 2] for index in range(0, 12, 2))


def _normalize_uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if not re.fullmatch(r"uuid:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", normalized):
        return None
    return normalized


async def async_read_local_identity(
    session: aiohttp.ClientSession, host: str, timeout: float
) -> dict[str, str]:
    """Return all available stable identifiers, preferring Tizen metadata."""
    identity: dict[str, str] = {}
    request_timeout = aiohttp.ClientTimeout(total=timeout)

    try:
        async with session.get(
            f"http://{host}:8001/api/v2/", timeout=request_timeout
        ) as response:
            response.raise_for_status()
            payload = await response.json()
        device = payload.get("device") if isinstance(payload, dict) else None
        if isinstance(device, dict):
            if mac := _normalize_mac(device.get("wifiMac")):
                identity["wifi_mac"] = mac
            if duid := _normalize_uuid(device.get("duid")):
                identity["tizen_duid"] = duid
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError):
        pass

    try:
        async with session.get(
            f"http://{host}:9110/ip_control", timeout=request_timeout
        ) as response:
            response.raise_for_status()
            body = await response.content.read(65537)
        if len(body) <= 65536:
            root = ElementTree.fromstring(body)
            udn = root.findtext(".//{*}device/{*}UDN")
            if normalized := _normalize_uuid(udn):
                identity["upnp_udn"] = normalized
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, ElementTree.ParseError):
        pass

    if not identity:
        raise LocalIdentityError("No stable local MAC or UUID was available")
    return identity


def identities_match(saved: dict[str, str], observed: dict[str, str]) -> bool:
    """Reject contradictions and require a shared identifier."""
    common = saved.keys() & observed.keys()
    return bool(common) and all(saved[key] == observed[key] for key in common)
