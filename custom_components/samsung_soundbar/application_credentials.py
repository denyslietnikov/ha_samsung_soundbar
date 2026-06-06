"""Application credentials for Samsung Soundbar."""

from __future__ import annotations

from http import HTTPStatus
from json import JSONDecodeError
import logging
from typing import cast

from aiohttp import BasicAuth, ClientError, ClientResponseError
from homeassistant.components.application_credentials import (
    AuthImplementation,
    AuthorizationServer,
    ClientCredential,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    OAuth2TokenRequestError,
    OAuth2TokenRequestReauthError,
    OAuth2TokenRequestTransientError,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.config_entry_oauth2_flow import AbstractOAuth2Implementation

from .const import DOMAIN, SMARTTHINGS_AUTHORIZE_URL, SMARTTHINGS_TOKEN_URL

_LOGGER = logging.getLogger(__name__)

SMARTTHINGS_CLI_DOCS_URL = "https://developer.smartthings.com/docs/sdks/cli"
SMARTTHINGS_OAUTH_REDIRECT_URI = "https://my.home-assistant.io/redirect/oauth"


async def async_get_description_placeholders(
    hass: HomeAssistant,
) -> dict[str, str]:
    """Return placeholders for the application credentials dialog."""
    return {
        "console_url": SMARTTHINGS_CLI_DOCS_URL,
        "redirect_uri": SMARTTHINGS_OAUTH_REDIRECT_URI,
    }


async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    """Return the SmartThings OAuth2 authorization server."""
    return AuthorizationServer(
        authorize_url=SMARTTHINGS_AUTHORIZE_URL,
        token_url=SMARTTHINGS_TOKEN_URL,
    )


async def async_get_auth_implementation(
    hass: HomeAssistant,
    auth_domain: str,
    credential: ClientCredential,
) -> AbstractOAuth2Implementation:
    """Return the SmartThings OAuth implementation."""
    return SmartThingsOAuth2Implementation(
        hass,
        auth_domain,
        credential,
        authorization_server=await async_get_authorization_server(hass),
    )


class SmartThingsOAuth2Implementation(AuthImplementation):
    """SmartThings OAuth implementation using HTTP Basic token auth."""

    async def _token_request(self, data: dict) -> dict:
        """Make a token request."""
        session = async_get_clientsession(self.hass)

        _LOGGER.debug("Sending SmartThings token request to %s", self.token_url)
        try:
            resp = await session.post(
                self.token_url,
                data=data,
                auth=BasicAuth(self.client_id, self.client_secret),
            )
            if resp.status >= 400:
                try:
                    error_response = await resp.json()
                except (ClientError, JSONDecodeError):
                    error_response = {}

                error_code = error_response.get("error", "unknown")
                error_description = error_response.get(
                    "error_description", "unknown error"
                )
                _LOGGER.debug(
                    "SmartThings token request failed (%s): %s",
                    error_code,
                    error_description,
                )
                resp.raise_for_status()
        except ClientResponseError as err:
            if err.status == HTTPStatus.TOO_MANY_REQUESTS or 500 <= err.status <= 599:
                raise OAuth2TokenRequestTransientError(
                    request_info=err.request_info,
                    history=err.history,
                    status=err.status,
                    message=err.message,
                    headers=err.headers,
                    domain=DOMAIN,
                ) from err
            if 400 <= err.status <= 499:
                raise OAuth2TokenRequestReauthError(
                    request_info=err.request_info,
                    history=err.history,
                    status=err.status,
                    message=err.message,
                    headers=err.headers,
                    domain=DOMAIN,
                ) from err
            raise OAuth2TokenRequestError(
                request_info=err.request_info,
                history=err.history,
                status=err.status,
                message=err.message,
                headers=err.headers,
                domain=DOMAIN,
            ) from err

        return cast(dict, await resp.json())
