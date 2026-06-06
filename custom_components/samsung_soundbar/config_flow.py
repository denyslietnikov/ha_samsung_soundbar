"""Config flow for Samsung Soundbar integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import pysmartthings
import voluptuous as vol
from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlowResult
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_TOKEN
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.config_entry_oauth2_flow import AbstractOAuth2FlowHandler
from pysmartthings.exceptions import (
    SmartThingsAuthenticationFailedError,
    SmartThingsConnectionError,
    SmartThingsForbiddenError,
)

from .const import (
    CONF_ENTRY_DEVICE_ID,
    CONF_ENTRY_DEVICE_NAME,
    DOMAIN,
    SMARTTHINGS_OAUTH_SCOPES,
)

_LOGGER = logging.getLogger(__name__)


class SamsungSoundbarConfigFlow(AbstractOAuth2FlowHandler, domain=DOMAIN):
    """Handle Samsung Soundbar config flow."""

    VERSION = 1
    MINOR_VERSION = 1
    DOMAIN = DOMAIN

    def __init__(self) -> None:
        super().__init__()
        self._devices: dict[str, str] = {}
        self._oauth_data: dict[str, Any] | None = None

    @property
    def logger(self) -> logging.Logger:
        """Return logger."""
        return _LOGGER

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        """Extra authorization data for SmartThings."""
        return {"scope": " ".join(SMARTTHINGS_OAUTH_SCOPES)}

    async def async_oauth_create_entry(
        self,
        data: dict[str, Any],
    ) -> ConfigFlowResult:
        """Create an entry from OAuth data."""
        token = data[CONF_TOKEN]
        granted_scope = token.get("scope")
        if granted_scope and not set(SMARTTHINGS_OAUTH_SCOPES) <= set(
            granted_scope.split()
        ):
            return self.async_abort(reason="missing_scopes")

        api = pysmartthings.SmartThings(session=async_get_clientsession(self.hass))
        api.authenticate(token[CONF_ACCESS_TOKEN])

        try:
            devices = await api.get_devices()
        except (
            SmartThingsAuthenticationFailedError,
            SmartThingsForbiddenError,
            SmartThingsConnectionError,
        ) as exc:
            _LOGGER.error("SmartThings OAuth validation failed: %s", exc)
            return self.async_abort(reason="invalid_auth")

        self._devices = {
            device.device_id: getattr(device, "label", None) or device.device_id
            for device in devices
        }

        if not self._devices:
            return self.async_abort(reason="no_devices")

        if self.source == SOURCE_REAUTH:
            return await self._async_finish_reauth(data)

        self._oauth_data = data
        return await self.async_step_device()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle user flow start."""
        return await super().async_step_user(user_input)

    async def async_step_reauth(
        self,
        entry_data: Mapping[str, Any],
    ) -> ConfigFlowResult:
        """Handle reauthentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Confirm OAuth reauthentication."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_user()

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle soundbar device selection."""

        if user_input is not None:
            if self._oauth_data is None:
                return self.async_abort(reason="oauth_error")

            await self.async_set_unique_id(user_input[CONF_ENTRY_DEVICE_ID])
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=user_input[CONF_ENTRY_DEVICE_NAME],
                data={
                    **self._oauth_data,
                    CONF_ENTRY_DEVICE_ID: user_input[CONF_ENTRY_DEVICE_ID],
                    CONF_ENTRY_DEVICE_NAME: user_input[CONF_ENTRY_DEVICE_NAME],
                },
            )

        default_device_id = next(iter(self._devices), None)
        default_name = self._devices.get(default_device_id, DOMAIN)

        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ENTRY_DEVICE_ID,
                        default=default_device_id,
                    ): vol.In(self._devices),
                    vol.Required(
                        CONF_ENTRY_DEVICE_NAME,
                        default=default_name,
                    ): str,
                }
            ),
        )

    async def _async_finish_reauth(
        self,
        data: dict[str, Any],
    ) -> ConfigFlowResult:
        """Finish reauth by replacing legacy auth data with OAuth data."""
        entry = self._get_reauth_entry()
        device_id = entry.data.get(CONF_ENTRY_DEVICE_ID)

        if device_id not in self._devices:
            return self.async_abort(reason="reauth_device_unavailable")

        await self.async_set_unique_id(device_id)
        self._abort_if_unique_id_mismatch(reason="reauth_account_mismatch")

        device_name = entry.data.get(CONF_ENTRY_DEVICE_NAME) or self._devices[device_id]
        new_data = {
            **data,
            CONF_ENTRY_DEVICE_ID: device_id,
            CONF_ENTRY_DEVICE_NAME: device_name,
        }

        return self.async_update_reload_and_abort(
            entry,
            data=new_data,
            title=device_name,
            unique_id=device_id,
        )
