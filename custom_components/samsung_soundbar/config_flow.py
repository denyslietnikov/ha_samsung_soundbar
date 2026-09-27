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
    CONF_ENTRY_API_KEY,
    CONF_CLOUD_INTEGRATION,
    CONF_ENTRY_MAX_VOLUME,
    CONF_ENTRY_DEVICE_NAME,
    CONF_ENTRY_SETTINGS_ADVANCED_AUDIO_SWITCHES,
    CONF_ENTRY_SETTINGS_EQ_SELECTOR,
    CONF_ENTRY_SETTINGS_SOUNDMODE_SELECTOR,
    CONF_ENTRY_SETTINGS_WOOFER_NUMBER,
    CONF_LOCATION_ID,
    CONF_INSTALLED_APP_ID,
    CONF_SUBSCRIPTION_ID,
    CONF_LOCAL_FALLBACK_TO_CLOUD,
    CONF_LOCAL_HOST,
    CONF_LOCAL_IDENTITY,
    CONF_LOCAL_PORT,
    CONF_LOCAL_TIMEOUT,
    CONF_LOCAL_VERIFY_SSL,
    CONTROL_MODE_LABELS,
    CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
    CONTROL_MODE_LOCAL_ONLY,
    DOMAIN,
    SMARTTHINGS_OAUTH_SCOPES,
    SMARTTHINGS_REQUIRED_SCOPES,
)
from .entry_options import get_entry_options
from .local_rpc import LocalRpcError, LocalSoundbarRpcClient
from .local_identity import LocalIdentityError, async_read_local_identity, identities_match
from .subscription import async_remove_subscription

_LOGGER = logging.getLogger(__name__)


class SamsungSoundbarConfigFlow(AbstractOAuth2FlowHandler, domain=DOMAIN):
    """Handle Samsung Soundbar config flow."""

    VERSION = 1
    MINOR_VERSION = 1
    DOMAIN = DOMAIN

    def __init__(self) -> None:
        super().__init__()
        self._devices: dict[str, str] = {}
        self._device_locations: dict[str, str] = {}
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
        if granted_scope and not set(SMARTTHINGS_REQUIRED_SCOPES) <= set(
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
        self._device_locations = {
            device.device_id: device.location_id for device in devices
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
                    CONF_LOCATION_ID: self._device_locations[
                        user_input[CONF_ENTRY_DEVICE_ID]
                    ],
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
            CONF_LOCATION_ID: self._device_locations[device_id],
        }

        return self.async_update_reload_and_abort(
            entry,
            data=new_data,
            title=device_name,
            unique_id=device_id,
        )


class SamsungSoundbarOptionsFlowHandler(OptionsFlow):
    """Handle Samsung Soundbar options."""

    _pending_options: dict[str, Any] | None = None
    _pending_identity: dict[str, str] | None = None

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Manage Samsung Soundbar feature options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            options = get_entry_options(self.config_entry)
            options.update(user_input)

            if options[CONF_CONTROL_MODE] in (
                CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
                CONTROL_MODE_LOCAL_ONLY,
            ):
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

            if not errors and options[CONF_CONTROL_MODE] == CONTROL_MODE_LOCAL_ONLY:
                try:
                    identity = await self._async_read_identity(options)
                except LocalIdentityError:
                    errors["base"] = "identity_unavailable"
                else:
                    saved = self.config_entry.options.get(CONF_LOCAL_IDENTITY)
                    if saved and not identities_match(saved, identity):
                        errors["base"] = "identity_mismatch"
                    elif not saved:
                        self._pending_options = options
                        self._pending_identity = identity
                        return self.async_show_form(
                            step_id="confirm_local_identity",
                            description_placeholders={
                                "identity": next(iter(identity.values())),
                                "host": options[CONF_LOCAL_HOST],
                            },
                        )
                    else:
                        options[CONF_LOCAL_IDENTITY] = identity

            if not errors:
                return await self._async_save_options(options)
        else:
            options = get_entry_options(self.config_entry)

        return self.async_show_form(
            step_id="init",
            data_schema=self._options_schema(options),
            errors=errors,
        )

    async def async_step_confirm_local_identity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Explicitly bind a legacy cloud entry to a local physical device."""
        options = self._pending_options
        expected = self._pending_identity
        if options is None or expected is None:
            return await self.async_step_init()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await self._async_validate_local_rpc(options)
                observed = await self._async_read_identity(options)
            except (LocalRpcError, LocalIdentityError):
                errors["base"] = "identity_unavailable"
            else:
                if not identities_match(expected, observed):
                    errors["base"] = "identity_mismatch"
                else:
                    options[CONF_LOCAL_IDENTITY] = observed
                    return await self._async_save_options(options)
        return self.async_show_form(
            step_id="confirm_local_identity",
            errors=errors,
            description_placeholders={
                "identity": next(iter(expected.values())),
                "host": options[CONF_LOCAL_HOST],
            },
        )

    async def _async_read_identity(self, options: dict[str, Any]) -> dict[str, str]:
        session = async_get_clientsession(self.hass)
        return await async_read_local_identity(
            session, options[CONF_LOCAL_HOST], options[CONF_LOCAL_TIMEOUT]
        )

    async def _async_save_options(self, options: dict[str, Any]) -> ConfigFlowResult:
        cloud_keys = {
            CONF_TOKEN,
            CONF_ENTRY_API_KEY,
            CONF_CLOUD_INTEGRATION,
            CONF_LOCATION_ID,
            CONF_INSTALLED_APP_ID,
            CONF_SUBSCRIPTION_ID,
            "auth_implementation",
        }
        if (
            options[CONF_CONTROL_MODE] == CONTROL_MODE_LOCAL_ONLY
            and cloud_keys & self.config_entry.data.keys()
        ):
            runtime = self.hass.data.get(DOMAIN)
            subscription = (
                runtime.subscriptions.get(self.config_entry.entry_id)
                if runtime is not None
                else None
            )
            if subscription is not None:
                await async_remove_subscription(self.config_entry, subscription)
            new_data = {
                key: value
                for key, value in self.config_entry.data.items()
                if key not in cloud_keys
            }
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=new_data, options=options
            )
        return self.async_create_entry(title="", data=options)

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
                ): vol.In(CONTROL_MODE_LABELS),
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
