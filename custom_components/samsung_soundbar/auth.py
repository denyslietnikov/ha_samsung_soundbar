"""OAuth helpers for Samsung Soundbar."""

from __future__ import annotations

import logging

from aiohttp import ClientError
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, OAuth2TokenRequestError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.config_entry_oauth2_flow import (
    ImplementationUnavailableError,
    OAuth2Session,
    async_get_config_entry_implementation,
)
from pysmartthings import SmartThings

_LOGGER = logging.getLogger(__name__)


class SmartThingsAuthProvider:
    """Keep pysmartthings and manual HTTP requests on a fresh access token."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        oauth_session: OAuth2Session,
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
        entry: ConfigEntry,
    ) -> SmartThingsAuthProvider:
        """Create an auth provider for a config entry."""
        try:
            implementation = await async_get_config_entry_implementation(hass, entry)
        except (ImplementationUnavailableError, ValueError) as err:
            raise ConfigEntryAuthFailed(
                "SmartThings OAuth application credentials are unavailable"
            ) from err
        oauth_session = OAuth2Session(hass, entry, implementation)
        api = SmartThings(session=async_get_clientsession(hass))
        provider = cls(hass, entry, oauth_session, api)
        await provider.async_get_access_token()
        return provider

    async def async_get_access_token(self, *, force_refresh: bool = False) -> str:
        """Return a valid access token and update the API client."""
        try:
            if force_refresh:
                await self._async_force_refresh_token()
            else:
                await self.oauth_session.async_ensure_token_valid()
        except (ClientError, OAuth2TokenRequestError) as err:
            raise ConfigEntryAuthFailed(
                "SmartThings OAuth token refresh failed"
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
