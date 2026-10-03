"""OAuth helpers for Samsung Soundbar."""

from __future__ import annotations

import logging

from aiohttp import ClientError
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    OAuth2TokenRequestError,
    OAuth2TokenRequestReauthError,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.config_entry_oauth2_flow import (
    ImplementationUnavailableError,
    OAuth2Session,
    async_get_config_entry_implementation,
)
from pysmartthings import SmartThings

from .cloud_errors import SmartThingsHttpSession, cloud_error_for_status
from .models import SoundbarConfigEntry

_LOGGER = logging.getLogger(__name__)


class SmartThingsAuthProvider:
    """Keep pysmartthings and manual HTTP requests on a fresh access token."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SoundbarConfigEntry,
        oauth_session: OAuth2Session | None,
        api: SmartThings,
    ) -> None:
        """Initialize the auth provider."""
        self.hass = hass
        self.entry = entry
        self.oauth_session = oauth_session
        self.api = api
        self.api.refresh_token_function = self.async_get_access_token

    @classmethod
    async def async_create(
        cls,
        hass: HomeAssistant,
        entry: SoundbarConfigEntry,
        *,
        defer_auth: bool = False,
    ) -> SmartThingsAuthProvider:
        """Create an auth provider for a config entry."""
        api = SmartThings(session=SmartThingsHttpSession(async_get_clientsession(hass)))
        provider = cls(hass, entry, None, api)
        if not defer_auth:
            await provider.async_get_access_token()
        return provider

    async def async_get_access_token(self, *, force_refresh: bool = False) -> str:
        """Return a valid access token and update the API client."""
        if CONF_TOKEN not in self.entry.data:
            raise ConfigEntryAuthFailed("SmartThings OAuth authorization is required")
        try:
            if self.oauth_session is None:
                try:
                    implementation = await async_get_config_entry_implementation(
                        self.hass, self.entry
                    )
                except ImplementationUnavailableError as err:
                    raise ConfigEntryNotReady(
                        "SmartThings OAuth implementation is temporarily unavailable"
                    ) from err
                except ValueError as err:
                    raise ConfigEntryAuthFailed(
                        "SmartThings OAuth application credentials are unavailable"
                    ) from err
                self.oauth_session = OAuth2Session(
                    self.hass, self.entry, implementation
                )
            if force_refresh:
                await self._async_force_refresh_token()
            else:
                await self.oauth_session.async_ensure_token_valid()
        except OAuth2TokenRequestReauthError as err:
            if mapped := cloud_error_for_status(err.status, err.headers):
                raise mapped from err
            raise ConfigEntryAuthFailed(
                "SmartThings OAuth refresh token is no longer valid"
            ) from err
        except OAuth2TokenRequestError as err:
            if mapped := cloud_error_for_status(err.status, err.headers):
                raise mapped from err
            if err.status == 401:
                raise ConfigEntryAuthFailed(
                    "SmartThings OAuth token refresh failed"
                ) from err
            raise ConfigEntryNotReady(
                "SmartThings OAuth token refresh failed temporarily"
            ) from err
        except ClientError as err:
            raise ConfigEntryNotReady(
                "SmartThings OAuth token refresh failed temporarily"
            ) from err

        access_token = self.entry.data[CONF_TOKEN][CONF_ACCESS_TOKEN]
        self.api.authenticate(access_token)
        return access_token

    async def _async_force_refresh_token(self) -> None:
        """Force refresh the OAuth token."""
        new_token = await self.oauth_session.implementation.async_refresh_token(
            self.oauth_session.token
        )
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={**self.entry.data, CONF_TOKEN: new_token},
        )
