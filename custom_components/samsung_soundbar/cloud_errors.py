"""Classify Cloud failures without guessing entitlement from a generic 403."""

from __future__ import annotations

from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from pysmartthings.exceptions import (
    SmartThingsForbiddenError,
    SmartThingsRateLimitError,
)


class CloudAccessError(HomeAssistantError):
    """Access or billing refusal which OAuth refresh cannot repair."""

    def __init__(self, status: int = 403) -> None:
        self.status = status
        self.retry_after = 300
        super().__init__(
            "SmartThings Cloud requires payment (HTTP 402)"
            if status == 402
            else "SmartThings Cloud denied access (HTTP 403); check app permissions or use Local only"
        )


class CloudRateLimitError(ConfigEntryNotReady):
    """Request rate/quota exhaustion is not an expired OAuth token."""

    def __init__(self, retry_after: float = 60) -> None:
        self.status = 429
        self.retry_after = max(15, min(retry_after, 3600))
        super().__init__("SmartThings Cloud request limit reached (HTTP 429)")


def cloud_error_for_status(status: int, headers=None) -> Exception | None:
    """Interpret only documented HTTP semantics, not untrusted response text."""
    if status in (402, 403):
        return CloudAccessError(status)
    if status == 429:
        try:
            delay = float((headers or {}).get("Retry-After", 60))
        except (TypeError, ValueError):
            delay = 60
        return CloudRateLimitError(delay)
    if status in (408, 500, 502, 503, 504, 520, 524):
        return ConfigEntryNotReady(
            f"SmartThings Cloud temporarily unavailable (HTTP {status})"
        )
    return None


def normalize_cloud_error(error: Exception) -> Exception:
    """Handle upstream exceptions even if a caller supplies an unwrapped client."""
    if isinstance(error, SmartThingsForbiddenError):
        return CloudAccessError()
    if isinstance(error, SmartThingsRateLimitError):
        return CloudRateLimitError()
    return error


class SmartThingsHttpSession:
    """Preserve status codes that pysmartthings 4.0.3 otherwise discards."""

    def __init__(self, session) -> None:
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    async def request(self, *args, **kwargs):
        response = await self._session.request(*args, **kwargs)
        if error := cloud_error_for_status(response.status, response.headers):
            response.release()
            raise error
        return response
