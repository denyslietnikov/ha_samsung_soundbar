"""Config flow for Samsung Soundbar integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
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
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from pysmartthings.exceptions import (
    SmartThingsAuthenticationFailedError,
    SmartThingsConnectionError,
    SmartThingsForbiddenError,
)

from .const import (
    CONF_CLOUD_INTEGRATION,
    CONF_CONTROL_MODE,
    CONF_ENTRY_API_KEY,
    CONF_ENTRY_DEVICE_ID,
    CONF_ENTRY_DEVICE_NAME,
    CONF_ENTRY_MAX_VOLUME,
    CONF_ENTRY_SETTINGS_ADVANCED_AUDIO_SWITCHES,
    CONF_ENTRY_SETTINGS_EQ_SELECTOR,
    CONF_ENTRY_SETTINGS_SOUNDMODE_SELECTOR,
    CONF_ENTRY_SETTINGS_WOOFER_NUMBER,
    CONF_INSTALLED_APP_ID,
    CONF_LOCAL_FALLBACK_TO_CLOUD,
    CONF_LOCAL_HOST,
    CONF_LOCAL_IDENTITY,
    CONF_LOCAL_PORT,
    CONF_LOCAL_TIMEOUT,
    CONF_LOCAL_VERIFY_SSL,
    CONF_LOCATION_ID,
    CONF_SUBSCRIPTION_ID,
    CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
    CONTROL_MODE_LABELS,
    CONTROL_MODE_LOCAL_ONLY,
    CONTROL_MODE_SMARTTHINGS_CLOUD,
    DOMAIN,
    SMARTTHINGS_OAUTH_SCOPES,
    SMARTTHINGS_REQUIRED_SCOPES,
)
from .dhcp_recovery import async_recover_dhcp_host
from .entry_options import DEFAULT_ENTRY_OPTIONS, get_entry_option, get_entry_options
from .local_identity import (
    LocalIdentityError,
    async_read_local_identity,
    identities_match,
)
from .local_rpc import LocalRpcError, LocalSoundbarRpcClient
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
        self._selected_control_mode = CONTROL_MODE_SMARTTHINGS_CLOUD
        self._pending_device_id: str | None = None
        self._pending_device_name: str | None = None

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

    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> ConfigFlowResult:
        """Recover existing local entries; never start OAuth or create an entry."""
        return self.async_abort(
            reason=await async_recover_dhcp_host(self.hass, discovery_info)
        )

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
        if self.source == SOURCE_REAUTH:
            return await super().async_step_user(user_input)
        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {vol.Required(CONF_CONTROL_MODE): vol.In(CONTROL_MODE_LABELS)}
                ),
            )
        mode = user_input.get(CONF_CONTROL_MODE)
        if mode not in CONTROL_MODE_LABELS:
            return self.async_abort(reason="invalid_control_mode")
        self._selected_control_mode = mode
        if mode == CONTROL_MODE_LOCAL_ONLY:
            return await self.async_step_local()
        return await super().async_step_user()

    async def async_step_local(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate a local host and create a local-only or Hybrid entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = str(user_input.get(CONF_LOCAL_HOST, "")).strip()
            if not host:
                errors[CONF_LOCAL_HOST] = "required"
            else:
                local_options = {
                    CONF_LOCAL_HOST: host,
                    CONF_LOCAL_PORT: user_input[CONF_LOCAL_PORT],
                    CONF_LOCAL_VERIFY_SSL: user_input[CONF_LOCAL_VERIFY_SSL],
                    CONF_LOCAL_TIMEOUT: user_input[CONF_LOCAL_TIMEOUT],
                }
                try:
                    identity = await async_read_local_identity(
                        async_get_clientsession(self.hass),
                        host,
                        local_options[CONF_LOCAL_TIMEOUT],
                    )
                except LocalIdentityError:
                    errors["base"] = "identity_unavailable"
                else:
                    rpc = LocalSoundbarRpcClient(
                        host,
                        async_get_clientsession(
                            self.hass,
                            verify_ssl=local_options[CONF_LOCAL_VERIFY_SSL],
                        ),
                        port=local_options[CONF_LOCAL_PORT],
                        verify_ssl=local_options[CONF_LOCAL_VERIFY_SSL],
                        timeout=local_options[CONF_LOCAL_TIMEOUT],
                    )
                    try:
                        await rpc.create_token()
                    except LocalRpcError:
                        errors["base"] = "local_cannot_connect"
                    else:
                        if self._async_local_entry_exists(identity, host):
                            return self.async_abort(reason="already_configured")
                        if self._selected_control_mode == CONTROL_MODE_LOCAL_ONLY:
                            identity_key = next(
                                key
                                for key in ("wifi_mac", "tizen_duid", "upnp_udn")
                                if key in identity
                            )
                            device_id = f"local:{identity_key}:{identity[identity_key]}"
                            await self.async_set_unique_id(device_id)
                            self._abort_if_unique_id_configured()
                            device_name = user_input[CONF_ENTRY_DEVICE_NAME].strip()
                            return self.async_create_entry(
                                title=device_name,
                                data={
                                    CONF_ENTRY_DEVICE_ID: device_id,
                                    CONF_ENTRY_DEVICE_NAME: device_name,
                                    CONF_CONTROL_MODE: CONTROL_MODE_LOCAL_ONLY,
                                    CONF_LOCAL_IDENTITY: identity,
                                    **local_options,
                                },
                            )
                        if self._oauth_data is None or self._pending_device_id is None:
                            return self.async_abort(reason="oauth_error")
                        return self.async_create_entry(
                            title=self._pending_device_name,
                            data={
                                **self._oauth_data,
                                CONF_ENTRY_DEVICE_ID: self._pending_device_id,
                                CONF_ENTRY_DEVICE_NAME: self._pending_device_name,
                                CONF_LOCATION_ID: self._device_locations[
                                    self._pending_device_id
                                ],
                                CONF_CONTROL_MODE: CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
                                CONF_LOCAL_IDENTITY: identity,
                                **local_options,
                            },
                        )

        schema = {
            vol.Required(CONF_LOCAL_HOST): str,
            vol.Required(
                CONF_LOCAL_PORT, default=DEFAULT_ENTRY_OPTIONS[CONF_LOCAL_PORT]
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
            vol.Required(
                CONF_LOCAL_VERIFY_SSL,
                default=DEFAULT_ENTRY_OPTIONS[CONF_LOCAL_VERIFY_SSL],
            ): bool,
            vol.Required(
                CONF_LOCAL_TIMEOUT, default=DEFAULT_ENTRY_OPTIONS[CONF_LOCAL_TIMEOUT]
            ): vol.All(vol.Coerce(float), vol.Range(min=1, max=60)),
        }
        if self._selected_control_mode == CONTROL_MODE_LOCAL_ONLY:
            schema[vol.Required(CONF_ENTRY_DEVICE_NAME, default="Samsung Soundbar")] = (
                str
            )
        return self.async_show_form(
            step_id="local", data_schema=vol.Schema(schema), errors=errors
        )

    def _async_local_entry_exists(self, identity: dict[str, str], host: str) -> bool:
        """Avoid another entry for a known local soundbar."""
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            saved = entry.options.get(CONF_LOCAL_IDENTITY) or entry.data.get(
                CONF_LOCAL_IDENTITY
            )
            if isinstance(saved, dict) and identities_match(saved, identity):
                return True
            configured_host = str(get_entry_option(entry, CONF_LOCAL_HOST)).strip()
            if configured_host and configured_host.casefold() == host.casefold():
                return True
        return False

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

            if self._selected_control_mode == CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS:
                self._pending_device_id = user_input[CONF_ENTRY_DEVICE_ID]
                self._pending_device_name = user_input[CONF_ENTRY_DEVICE_NAME]
                return await self.async_step_local()

            return self.async_create_entry(
                title=user_input[CONF_ENTRY_DEVICE_NAME],
                data={
                    **self._oauth_data,
                    CONF_ENTRY_DEVICE_ID: user_input[CONF_ENTRY_DEVICE_ID],
                    CONF_ENTRY_DEVICE_NAME: user_input[CONF_ENTRY_DEVICE_NAME],
                    CONF_LOCATION_ID: self._device_locations[
                        user_input[CONF_ENTRY_DEVICE_ID]
                    ],
                    CONF_CONTROL_MODE: CONTROL_MODE_SMARTTHINGS_CLOUD,
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

        device_name = entry.data.get(CONF_ENTRY_DEVICE_NAME) or self._devices[device_id]
        preserved_local_data = {
            key: entry.data[key]
            for key in (
                CONF_CONTROL_MODE,
                CONF_LOCAL_HOST,
                CONF_LOCAL_PORT,
                CONF_LOCAL_VERIFY_SSL,
                CONF_LOCAL_TIMEOUT,
                CONF_LOCAL_IDENTITY,
                CONF_LOCAL_FALLBACK_TO_CLOUD,
            )
            if key in entry.data
        }
        new_data = {
            **preserved_local_data,
            **data,
            CONF_ENTRY_DEVICE_ID: device_id,
            CONF_ENTRY_DEVICE_NAME: device_name,
            CONF_LOCATION_ID: self._device_locations[device_id],
        }

        runtime = getattr(entry, "runtime_data", None)
        if runtime is not None:
            runtime.reauth_pending = True
        changed = (
            entry.data != new_data
            or entry.title != device_name
            or entry.unique_id != device_id
        )
        result = self.async_update_and_abort(
            entry,
            data=new_data,
            title=device_name,
            unique_id=device_id,
        )
        # Loaded entries reload through the listener, even for token-only reauth.
        # Failed setup and unchanged credentials have no update to trigger it.
        if not entry.update_listeners or not changed:
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return result


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
            saved = self.config_entry.options.get(
                CONF_LOCAL_IDENTITY
            ) or self.config_entry.data.get(CONF_LOCAL_IDENTITY)
            if isinstance(saved, dict):
                options[CONF_LOCAL_IDENTITY] = dict(saved)
            else:
                saved = None

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

            if not errors and options[CONF_CONTROL_MODE] in (
                CONTROL_MODE_LOCAL_ONLY,
                CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
            ):
                try:
                    identity = await self._async_read_identity(options)
                except LocalIdentityError:
                    host_changed = (
                        options[CONF_LOCAL_HOST]
                        != str(
                            get_entry_option(self.config_entry, CONF_LOCAL_HOST)
                        ).strip()
                    )
                    if options[CONF_CONTROL_MODE] == CONTROL_MODE_LOCAL_ONLY or (
                        saved and host_changed
                    ):
                        errors["base"] = "identity_unavailable"
                    else:
                        _LOGGER.debug(
                            "Hybrid identity unavailable; retaining existing binding"
                        )
                else:
                    if saved and not identities_match(saved, identity):
                        errors["base"] = "identity_mismatch"
                    elif (
                        not saved
                        and options[CONF_CONTROL_MODE] == CONTROL_MODE_LOCAL_ONLY
                    ):
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
                        options[CONF_LOCAL_IDENTITY] = {**(saved or {}), **identity}

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
                    options[CONF_LOCAL_IDENTITY] = {**expected, **observed}
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
            runtime = getattr(self.config_entry, "runtime_data", None)
            subscription = runtime.subscription if runtime is not None else None
            if subscription is not None:
                await async_remove_subscription(self.config_entry, subscription)
            new_data = {
                key: value
                for key, value in self.config_entry.data.items()
                if key not in cloud_keys
            }
            changed = self.hass.config_entries.async_update_entry(
                self.config_entry, data=new_data, options=options
            )
            if changed and not getattr(self.config_entry, "update_listeners", ()):
                self.hass.config_entries.async_schedule_reload(
                    self.config_entry.entry_id
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
