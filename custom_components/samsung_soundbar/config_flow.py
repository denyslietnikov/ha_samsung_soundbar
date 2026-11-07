"""Native local RPC and optional OCF setup; credentials live only in private Store."""

from __future__ import annotations

from copy import deepcopy

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from .const import (
    CONF_CONTROL_MODE,
    CONF_ENTRY_DEVICE_ID,
    CONF_ENTRY_DEVICE_NAME,
    CONF_ENTRY_MAX_VOLUME,
    CONF_LOCAL_HOST,
    CONF_LOCAL_IDENTITY,
    CONF_LOCAL_PORT,
    CONF_LOCAL_TIMEOUT,
    CONF_LOCAL_VERIFY_SSL,
    CONF_OCF_ACTION,
    CONF_OCF_CONFIGURED,
    CONTROL_MODE_HYBRID_LOCAL_OCF,
    CONTROL_MODE_LABELS,
    CONTROL_MODE_LOCAL_ONLY,
    DOMAIN,
)
from .dhcp_recovery import async_recover_dhcp_host
from .entry_migration import ENTRY_MINOR_VERSION, ENTRY_VERSION
from .entry_options import DEFAULT_ENTRY_OPTIONS, get_entry_option, get_entry_options
from .local_identity import (
    LocalIdentityError,
    async_read_local_identity,
    has_stable_identity,
    identities_match,
)
from .local_ocf import LocalSoundbarOcfClient, OcfError
from .local_rpc import LocalRpcError, LocalSoundbarRpcClient
from .ocf_credentials import (
    OcfCredentialError,
    OcfCredentialStore,
    parse_credential_bundle,
)
from .ocf_repairs import update_local_configuration_issue, update_ocf_issue


def _saved_identity(entry):
    identity = entry.options.get(CONF_LOCAL_IDENTITY) or entry.data.get(
        CONF_LOCAL_IDENTITY
    )
    return identity if has_stable_identity(identity) else None


def _binding_snapshot(entry):
    """Detach the effective settings and identity from mutable entry data."""
    return deepcopy(
        {
            **get_entry_options(entry),
            CONF_LOCAL_IDENTITY: entry.options.get(CONF_LOCAL_IDENTITY)
            or entry.data.get(CONF_LOCAL_IDENTITY),
        }
    )


def _binding_is_current(hass, entry, snapshot):
    return (
        hass.config_entries.async_get_entry(entry.entry_id) is entry
        and _binding_snapshot(entry) == snapshot
    )


def _identity_in_use(hass, identity, host, exclude=None):
    """A locator or known physical identity must not bind two different entries."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry is exclude:
            continue
        saved = _saved_identity(entry)
        if isinstance(saved, dict) and identities_match(saved, identity):
            return True
        configured_host = str(get_entry_option(entry, CONF_LOCAL_HOST) or "").strip()
        if configured_host and configured_host.casefold() == host.casefold():
            return True
    return False


def _local_schema(options, *, mode=False, name=None):
    fields = {}
    if mode:
        fields[vol.Required(CONF_CONTROL_MODE, default=options[CONF_CONTROL_MODE])] = (
            vol.In(CONTROL_MODE_LABELS)
        )
    fields.update(
        {
            vol.Required(CONF_LOCAL_HOST, default=options[CONF_LOCAL_HOST]): str,
            vol.Required(CONF_LOCAL_PORT, default=options[CONF_LOCAL_PORT]): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=65535)
            ),
            vol.Required(
                CONF_LOCAL_VERIFY_SSL, default=options[CONF_LOCAL_VERIFY_SSL]
            ): bool,
            vol.Required(
                CONF_LOCAL_TIMEOUT, default=options[CONF_LOCAL_TIMEOUT]
            ): vol.All(vol.Coerce(float), vol.Range(min=1, max=60)),
        }
    )
    if name is not None:
        fields[vol.Required(CONF_ENTRY_DEVICE_NAME, default=name)] = str
    return fields


async def _validate_local(hass, options):
    """Read identity and validate RPC authentication without a hardware write."""
    host = options[CONF_LOCAL_HOST]
    identity = await async_read_local_identity(
        async_get_clientsession(hass), host, options[CONF_LOCAL_TIMEOUT]
    )
    rpc = LocalSoundbarRpcClient(
        host,
        async_get_clientsession(hass, verify_ssl=options[CONF_LOCAL_VERIFY_SSL]),
        port=options[CONF_LOCAL_PORT],
        verify_ssl=options[CONF_LOCAL_VERIFY_SSL],
        timeout=options[CONF_LOCAL_TIMEOUT],
    )
    await rpc.create_token()
    await rpc.call("getIdentifier")
    return identity


class SamsungSoundbarConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create or reconfigure the existing soundbar using local transports."""

    VERSION = ENTRY_VERSION
    MINOR_VERSION = ENTRY_MINOR_VERSION

    def __init__(self):
        self._selected_control_mode = CONTROL_MODE_LOCAL_ONLY
        self._pending_options = None
        self._pending_identity = None
        self._pending_binding = None

    @staticmethod
    @callback
    def async_get_options_flow(_config_entry: ConfigEntry) -> OptionsFlow:
        return SamsungSoundbarOptionsFlowHandler()

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo):
        return self.async_abort(
            reason=await async_recover_dhcp_host(self.hass, discovery_info)
        )

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            mode = user_input.get(CONF_CONTROL_MODE)
            if mode not in CONTROL_MODE_LABELS:
                return self.async_abort(reason="invalid_control_mode")
            self._selected_control_mode = mode
            return await self.async_step_local()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CONTROL_MODE, default=CONTROL_MODE_LOCAL_ONLY
                    ): vol.In(CONTROL_MODE_LABELS),
                }
            ),
        )

    async def async_step_local(self, user_input=None):
        errors = {}
        options = dict(DEFAULT_ENTRY_OPTIONS)
        if user_input is not None:
            options.update(
                {key: value for key, value in user_input.items() if key in options}
            )
            options[CONF_LOCAL_HOST] = str(options[CONF_LOCAL_HOST]).strip()
            name = str(user_input.get(CONF_ENTRY_DEVICE_NAME, "")).strip()
            if not options[CONF_LOCAL_HOST]:
                errors[CONF_LOCAL_HOST] = "required"
            if not name:
                errors[CONF_ENTRY_DEVICE_NAME] = "required"
            if not errors:
                try:
                    identity = await _validate_local(self.hass, options)
                except LocalIdentityError:
                    errors["base"] = "identity_unavailable"
                except LocalRpcError:
                    errors["base"] = "local_cannot_connect"
                else:
                    if _identity_in_use(self.hass, identity, options[CONF_LOCAL_HOST]):
                        return self.async_abort(reason="already_configured")
                    key = next(
                        key
                        for key in ("wifi_mac", "tizen_duid", "upnp_udn")
                        if key in identity
                    )
                    device_id = f"local:{key}:{identity[key]}"
                    await self.async_set_unique_id(device_id)
                    self._abort_if_unique_id_configured()
                    options[CONF_CONTROL_MODE] = self._selected_control_mode
                    return self.async_create_entry(
                        title=name,
                        data={
                            **options,
                            CONF_ENTRY_DEVICE_ID: device_id,
                            CONF_ENTRY_DEVICE_NAME: name,
                            CONF_LOCAL_IDENTITY: identity,
                        },
                    )
        return self.async_show_form(
            step_id="local",
            data_schema=vol.Schema(_local_schema(options, name="Samsung Soundbar")),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input=None):
        entry = self._get_reconfigure_entry()
        binding = _binding_snapshot(entry)
        errors = {}
        options = get_entry_options(entry)
        if user_input is not None:
            options.update(user_input)
            options[CONF_LOCAL_HOST] = str(options[CONF_LOCAL_HOST]).strip()
            if not options[CONF_LOCAL_HOST]:
                errors[CONF_LOCAL_HOST] = "required"
            if not errors:
                try:
                    observed = await _validate_local(self.hass, options)
                except LocalIdentityError:
                    errors["base"] = "identity_unavailable"
                except LocalRpcError:
                    errors["base"] = "local_cannot_connect"
                else:
                    saved = _saved_identity(entry)
                    if (
                        not _binding_is_current(self.hass, entry, binding)
                        or _identity_in_use(
                            self.hass, observed, options[CONF_LOCAL_HOST], entry
                        )
                        or saved
                        and not identities_match(saved, observed)
                    ):
                        errors["base"] = "identity_mismatch"
                    elif not saved:
                        self._pending_options, self._pending_identity = (
                            options,
                            observed,
                        )
                        self._pending_binding = binding
                        return await self.async_step_confirm_local_identity()
                    else:
                        options[CONF_LOCAL_IDENTITY] = {**saved, **observed}
                        return self._finish_reconfigure(entry, options)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(_local_schema(options, mode=True)),
            errors=errors,
        )

    async def async_step_confirm_local_identity(self, user_input=None):
        if self._pending_options is None or self._pending_identity is None:
            return await self.async_step_reconfigure()
        options, expected = self._pending_options, self._pending_identity
        errors = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None and not _binding_is_current(
            self.hass, entry, self._pending_binding
        ):
            errors["base"] = "identity_mismatch"
        elif user_input is not None:
            try:
                observed = await _validate_local(self.hass, options)
            except (LocalIdentityError, LocalRpcError):
                errors["base"] = "identity_unavailable"
            else:
                if (
                    not _binding_is_current(self.hass, entry, self._pending_binding)
                    or not identities_match(expected, observed)
                    or _identity_in_use(
                        self.hass, observed, options[CONF_LOCAL_HOST], entry
                    )
                ):
                    errors["base"] = "identity_mismatch"
                else:
                    options[CONF_LOCAL_IDENTITY] = {**expected, **observed}
                    return self._finish_reconfigure(entry, options)
        return self.async_show_form(
            step_id="confirm_local_identity",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                "identity": next(iter(expected.values())),
                "host": options[CONF_LOCAL_HOST],
            },
        )

    def _finish_reconfigure(self, entry, options):
        update_local_configuration_issue(self.hass, entry, False)
        result = self.async_update_and_abort(
            entry, options={**entry.options, **options}
        )
        if not entry.update_listeners:
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return result


class SamsungSoundbarOptionsFlowHandler(OptionsFlow):
    """Manage local connection settings and explicitly import or forget OCF."""

    def __init__(self):
        self._pending_options = None
        self._pending_identity = None
        self._pending_binding = None

    async def async_step_init(self, user_input=None):
        errors = {}
        binding = _binding_snapshot(self.config_entry)
        options = get_entry_options(self.config_entry)
        if user_input is not None:
            action = user_input.get(CONF_OCF_ACTION, "keep")
            if action == "import":
                return await self.async_step_ocf_import()
            if action == "forget":
                return await self.async_step_ocf_forget()
            options.update(
                {key: value for key, value in user_input.items() if key in options}
            )
            options[CONF_LOCAL_HOST] = str(options[CONF_LOCAL_HOST]).strip()
            if not options[CONF_LOCAL_HOST]:
                errors[CONF_LOCAL_HOST] = "required"
            else:
                try:
                    identity = await _validate_local(self.hass, options)
                except LocalIdentityError:
                    errors["base"] = "identity_unavailable"
                except LocalRpcError:
                    errors["base"] = "local_cannot_connect"
                else:
                    saved = _saved_identity(self.config_entry)
                    if (
                        not _binding_is_current(self.hass, self.config_entry, binding)
                        or _identity_in_use(
                            self.hass,
                            identity,
                            options[CONF_LOCAL_HOST],
                            self.config_entry,
                        )
                        or saved
                        and not identities_match(saved, identity)
                    ):
                        errors["base"] = "identity_mismatch"
                    elif not saved:
                        self._pending_options, self._pending_identity = (
                            options,
                            identity,
                        )
                        self._pending_binding = binding
                        return await self.async_step_confirm_local_identity()
                    else:
                        options[CONF_LOCAL_IDENTITY] = {**saved, **identity}
                        return await self._async_save_options(options)
        return self.async_show_form(
            step_id="init", data_schema=self._options_schema(options), errors=errors
        )

    async def async_step_confirm_local_identity(self, user_input=None):
        options, expected = self._pending_options, self._pending_identity
        if options is None or expected is None:
            return await self.async_step_init()
        errors = {}
        if user_input is not None and not _binding_is_current(
            self.hass, self.config_entry, self._pending_binding
        ):
            errors["base"] = "identity_mismatch"
        elif user_input is not None:
            try:
                observed = await _validate_local(self.hass, options)
            except (LocalRpcError, LocalIdentityError):
                errors["base"] = "identity_unavailable"
            else:
                if (
                    not _binding_is_current(
                        self.hass, self.config_entry, self._pending_binding
                    )
                    or not identities_match(expected, observed)
                    or _identity_in_use(
                        self.hass, observed, options[CONF_LOCAL_HOST], self.config_entry
                    )
                ):
                    errors["base"] = "identity_mismatch"
                else:
                    options[CONF_LOCAL_IDENTITY] = {**expected, **observed}
                    return await self._async_save_options(options)
        return self.async_show_form(
            step_id="confirm_local_identity",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                "identity": next(iter(expected.values())),
                "host": options[CONF_LOCAL_HOST],
            },
        )

    async def _async_read_identity(self, options):
        return await async_read_local_identity(
            async_get_clientsession(self.hass),
            options[CONF_LOCAL_HOST],
            options[CONF_LOCAL_TIMEOUT],
        )

    async def async_step_ocf_import(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                credential = parse_credential_bundle(
                    user_input.get("credential_bundle", "")
                )
            except OcfCredentialError:
                errors["base"] = "ocf_invalid"
            else:
                result, error = await self._async_import_ocf(credential)
                if result is not None:
                    return result
                errors["base"] = error
        return self.async_show_form(
            step_id="ocf_import",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required("credential_bundle"): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    )
                }
            ),
        )

    async def _async_import_ocf(self, credential):
        store = OcfCredentialStore(self.hass, self.config_entry.entry_id)
        async with store.configuration_lock:
            options = get_entry_options(self.config_entry)
            host = str(options[CONF_LOCAL_HOST]).strip()
            saved = self.config_entry.options.get(
                CONF_LOCAL_IDENTITY
            ) or self.config_entry.data.get(CONF_LOCAL_IDENTITY)
            if (
                options[CONF_CONTROL_MODE]
                not in (CONTROL_MODE_LOCAL_ONLY, CONTROL_MODE_HYBRID_LOCAL_OCF)
                or not host
            ):
                return None, "ocf_local_required"
            if saved is not None and not isinstance(saved, dict):
                return None, "identity_unavailable"
            saved = dict(saved) if saved else None
            observed = None

            def active():
                current_identity = self.config_entry.options.get(
                    CONF_LOCAL_IDENTITY
                ) or self.config_entry.data.get(CONF_LOCAL_IDENTITY)
                return (
                    self.hass.config_entries.async_get_entry(self.config_entry.entry_id)
                    is self.config_entry
                    and any(
                        item["flow_id"] == self.flow_id
                        for item in self.hass.config_entries.options.async_progress()
                    )
                    and str(
                        get_entry_option(self.config_entry, CONF_LOCAL_HOST)
                    ).strip()
                    == host
                    and get_entry_option(self.config_entry, CONF_CONTROL_MODE)
                    == options[CONF_CONTROL_MODE]
                    and (
                        current_identity == saved
                        or (
                            isinstance(current_identity, dict)
                            and not current_identity
                            and saved is None
                        )
                    )
                    and (
                        observed is None
                        or not _identity_in_use(
                            self.hass, observed, host, self.config_entry
                        )
                    )
                )

            client, owned = None, False
            try:
                if not active():
                    raise OcfCredentialError
                observed = await self._async_read_identity(options)
                if not observed:
                    return None, "identity_unavailable"
                if saved is not None and not identities_match(saved, observed):
                    return None, "identity_mismatch"
                if _identity_in_use(self.hass, observed, host, self.config_entry):
                    return None, "identity_mismatch"
                previous = await store.async_load()
                if (
                    previous is not None
                    and previous.target_device_uuid != credential.target_device_uuid
                ):
                    raise OcfCredentialError
                runtime = getattr(self.config_entry, "runtime_data", None)
                client = runtime.ocf if runtime is not None else None
                if client is None:
                    client = LocalSoundbarOcfClient(self.hass, host, credential)
                    owned = True
                try:
                    await client.async_validate_candidate(credential)
                finally:
                    if owned:
                        await client.async_close()
                await store.async_save(credential, expected=previous, guard=active)
                return self._async_finish_ocf(
                    True, identity=observed if saved is None else None
                ), None
            except LocalIdentityError:
                return None, "identity_unavailable"
            except OcfCredentialError:
                return None, "ocf_storage"
            except OcfError as error:
                return None, "ocf_auth" if error.kind in (
                    "auth",
                    "identity",
                ) else "ocf_connect"

    async def async_step_ocf_forget(self, user_input=None):
        errors = {}
        if user_input is not None and user_input.get("confirm") is True:
            store = OcfCredentialStore(self.hass, self.config_entry.entry_id)
            async with store.configuration_lock:
                try:
                    await store.async_remove(locked=True)
                    runtime = getattr(self.config_entry, "runtime_data", None)
                    if runtime is not None and runtime.ocf is not None:
                        try:
                            await runtime.coordinator.async_detach_ocf()
                        finally:
                            runtime.ocf = runtime.coordinator.ocf
                except (OcfCredentialError, OcfError):
                    errors["base"] = "ocf_storage"
                else:
                    return self._async_finish_ocf(False)
        return self.async_show_form(
            step_id="ocf_forget",
            errors=errors,
            data_schema=vol.Schema({vol.Required("confirm", default=False): bool}),
        )

    def _async_finish_ocf(self, configured, *, identity=None):
        data = dict(self.config_entry.data)
        options = dict(self.config_entry.options)
        if identity is not None:
            options[CONF_LOCAL_IDENTITY] = dict(identity)
        if configured:
            data[CONF_OCF_CONFIGURED] = True
            data[CONF_CONTROL_MODE] = options[CONF_CONTROL_MODE] = (
                CONTROL_MODE_HYBRID_LOCAL_OCF
            )
        else:
            data.pop(CONF_OCF_CONFIGURED, None)
            options.pop(CONF_OCF_CONFIGURED, None)
        changed = self.hass.config_entries.async_update_entry(
            self.config_entry, data=data, options=options
        )
        hybrid = (
            get_entry_option(self.config_entry, CONF_CONTROL_MODE)
            == CONTROL_MODE_HYBRID_LOCAL_OCF
        )
        update_ocf_issue(
            self.hass,
            self.config_entry,
            "missing_credential" if hybrid and not configured else None,
        )
        runtime = getattr(self.config_entry, "runtime_data", None)
        if (
            runtime is not None
            and (not self.config_entry.update_listeners or not changed)
        ) or (
            not self.config_entry.update_listeners
            and self.config_entry.state
            in (ConfigEntryState.SETUP_ERROR, ConfigEntryState.SETUP_RETRY)
        ):
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
        return self.async_create_entry(title="", data=options)

    async def _async_save_options(self, options):
        update_local_configuration_issue(self.hass, self.config_entry, False)
        options = {**self.config_entry.options, **options}
        changed = self.hass.config_entries.async_update_entry(
            self.config_entry, options=options
        )
        if (
            changed
            and not self.config_entry.update_listeners
            and self.config_entry.state
            in (ConfigEntryState.SETUP_ERROR, ConfigEntryState.SETUP_RETRY)
        ):
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
        return self.async_create_entry(title="", data=options)

    @staticmethod
    def _options_schema(options):
        fields = _local_schema(options, mode=True)
        fields[vol.Optional(CONF_OCF_ACTION, default="keep")] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=["keep", "import", "forget"], translation_key="ocf_action"
            )
        )
        fields[
            vol.Required(CONF_ENTRY_MAX_VOLUME, default=options[CONF_ENTRY_MAX_VOLUME])
        ] = vol.All(int, vol.Range(min=1, max=100))
        return vol.Schema(fields)
