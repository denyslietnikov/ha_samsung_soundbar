"""Config flow for Samsung Soundbar integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import pysmartthings
import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_TOKEN
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.config_entry_oauth2_flow import AbstractOAuth2FlowHandler
from pysmartthings.exceptions import (
    SmartThingsAuthenticationFailedError,
    SmartThingsConnectionError,
    SmartThingsForbiddenError,
)

from .const import (
    CONF_CONTROL_MODE,
    CONF_ENTRY_DEVICE_ID,
    CONF_ENTRY_MAX_VOLUME,
    CONF_ENTRY_DEVICE_NAME,
    CONF_ENTRY_SETTINGS_ADVANCED_AUDIO_SWITCHES,
    CONF_ENTRY_SETTINGS_EQ_SELECTOR,
    CONF_ENTRY_SETTINGS_SOUNDMODE_SELECTOR,
    CONF_ENTRY_SETTINGS_WOOFER_NUMBER,
    CONF_LOCAL_FALLBACK_TO_CLOUD,
    CONF_LOCAL_HOST,
    CONF_LOCAL_PORT,
    CONF_LOCAL_TIMEOUT,
    CONF_LOCAL_VERIFY_SSL,
    CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
    CONTROL_MODES,
    DOMAIN,
    SMARTTHINGS_OAUTH_SCOPES,
)
from .entry_options import get_entry_options
from .local_rpc import LocalRpcError, LocalSoundbarRpcClient

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

    @staticmethod
    @callback
    def async_get_options_flow(_config_entry: ConfigEntry) -> OptionsFlow:
        """Create the options flow."""
        return SamsungSoundbarOptionsFlowHandler()

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
        ) as exc:
            _LOGGER.error("SmartThings OAuth validation failed: %s", exc)
            return self.async_abort(reason="invalid_auth")
        except SmartThingsConnectionError as exc:
            _LOGGER.warning("SmartThings OAuth validation could not connect: %s", exc)
            return self.async_abort(reason="cannot_connect")

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

        if device_id is None:
            return self.async_abort(reason="reauth_device_unavailable")

        if device_id not in self._devices:
            return self.async_abort(reason="reauth_device_unavailable")

        await self.async_set_unique_id(device_id)
        self._abort_if_unique_id_mismatch(reason="reauth_account_mismatch")

        device_name = entry.data.get(CONF_ENTRY_DEVICE_NAME) or self._devices[
            device_id
        ]
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


class SamsungSoundbarOptionsFlowHandler(OptionsFlow):
    """Handle Samsung Soundbar options."""

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Manage Samsung Soundbar feature options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            options = get_entry_options(self.config_entry)
            options.update(user_input)

            if options[CONF_CONTROL_MODE] == CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS:
                local_host = str(options.get(CONF_LOCAL_HOST, "")).strip()
                options[CONF_LOCAL_HOST] = local_host
                if not local_host:
                    errors[CONF_LOCAL_HOST] = "required"
                else:
                    try:
                        await self._async_validate_local_rpc(options)
                    except LocalRpcError as err:
                        _LOGGER.warning(
                            "Cannot connect to local soundbar RPC at %s:%s: %s",
                            local_host,
                            options[CONF_LOCAL_PORT],
                            err,
                        )
                        errors["base"] = "cannot_connect"

            if not errors:
                return self.async_create_entry(title="", data=options)
        else:
            options = get_entry_options(self.config_entry)

        return self.async_show_form(
            step_id="init",
            data_schema=self._options_schema(options),
            errors=errors,
        )

    async def _async_validate_local_rpc(self, options: dict[str, Any]) -> None:
        """Validate local JSON-RPC options."""
        session = async_get_clientsession(
            self.hass,
            verify_ssl=options[CONF_LOCAL_VERIFY_SSL],
        )
        client = LocalSoundbarRpcClient(
            options[CONF_LOCAL_HOST],
            session,
            port=options[CONF_LOCAL_PORT],
            verify_ssl=options[CONF_LOCAL_VERIFY_SSL],
            timeout=options[CONF_LOCAL_TIMEOUT],
        )
        await client.create_token()
        await client.call("getIdentifier")

    @staticmethod
    def _options_schema(options: dict[str, Any]) -> vol.Schema:
        """Return the options schema with current defaults."""
        return vol.Schema(
            {
                vol.Required(
                    CONF_CONTROL_MODE,
                    default=options[CONF_CONTROL_MODE],
                ): vol.In(CONTROL_MODES),
                vol.Optional(
                    CONF_LOCAL_HOST,
                    default=options[CONF_LOCAL_HOST],
                ): str,
                vol.Required(
                    CONF_LOCAL_PORT,
                    default=options[CONF_LOCAL_PORT],
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
                vol.Required(
                    CONF_LOCAL_VERIFY_SSL,
                    default=options[CONF_LOCAL_VERIFY_SSL],
                ): bool,
                vol.Required(
                    CONF_LOCAL_TIMEOUT,
                    default=options[CONF_LOCAL_TIMEOUT],
                ): vol.All(vol.Coerce(float), vol.Range(min=1, max=60)),
                vol.Required(
                    CONF_LOCAL_FALLBACK_TO_CLOUD,
                    default=options[CONF_LOCAL_FALLBACK_TO_CLOUD],
                ): bool,
                vol.Required(
                    CONF_ENTRY_SETTINGS_ADVANCED_AUDIO_SWITCHES,
                    default=options[CONF_ENTRY_SETTINGS_ADVANCED_AUDIO_SWITCHES],
                ): bool,
                vol.Required(
                    CONF_ENTRY_SETTINGS_EQ_SELECTOR,
                    default=options[CONF_ENTRY_SETTINGS_EQ_SELECTOR],
                ): bool,
                vol.Required(
                    CONF_ENTRY_SETTINGS_SOUNDMODE_SELECTOR,
                    default=options[CONF_ENTRY_SETTINGS_SOUNDMODE_SELECTOR],
                ): bool,
                vol.Required(
                    CONF_ENTRY_SETTINGS_WOOFER_NUMBER,
                    default=options[CONF_ENTRY_SETTINGS_WOOFER_NUMBER],
                ): bool,
                vol.Required(
                    CONF_ENTRY_MAX_VOLUME,
                    default=options[CONF_ENTRY_MAX_VOLUME],
                ): vol.All(int, vol.Range(min=1, max=100)),
            }
        )
