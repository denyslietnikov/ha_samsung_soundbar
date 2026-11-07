"""Set up the soundbar using only local RPC and optional local OCF."""

import logging
from functools import partial

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_DEVICE_ID as CONF_HA_DEVICE_ID
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api_extension.SoundbarDevice import SoundbarDevice
from .const import (
    CONF_CONTROL_MODE,
    CONF_ENTRY_DEVICE_ID,
    CONF_ENTRY_DEVICE_NAME,
    CONF_ENTRY_MAX_VOLUME,
    CONF_INCLUDE_NULL,
    CONF_LOCAL_HOST,
    CONF_LOCAL_IDENTITY,
    CONF_LOCAL_PORT,
    CONF_LOCAL_RPC_HOST,
    CONF_LOCAL_RPC_METHODS,
    CONF_LOCAL_RPC_PORT,
    CONF_LOCAL_RPC_TIMEOUT,
    CONF_LOCAL_RPC_VERIFY_SSL,
    CONF_LOCAL_RPC_WRITE_METHOD,
    CONF_LOCAL_RPC_WRITE_PARAMS,
    CONF_LOCAL_TIMEOUT,
    CONF_LOCAL_VERIFY_SSL,
    CONF_OCF_CONFIGURED,
    CONTROL_MODE_HYBRID_LOCAL_OCF,
    CONTROL_MODES,
    DOMAIN,
    SERVICE_DUMP_DISCOVERY_SNAPSHOT,
    SERVICE_DUMP_LOCAL_RPC,
    SERVICE_DUMP_OCF_STATUS,
)
from .coordinator import SoundbarCoordinator
from .entity_migrations import async_remove_woofer_buttons
from .entry_migration import clear_legacy_issues, migrate_entry
from .entry_options import get_entry_option
from .local_device import LocalDevice
from .local_identity import has_stable_identity
from .local_ocf import LocalSoundbarOcfClient, OcfError
from .local_rpc import (
    DEFAULT_LOCAL_RPC_METHODS,
    DEFAULT_LOCAL_RPC_PORT,
    DEFAULT_LOCAL_RPC_TIMEOUT,
    LocalRpcError,
    LocalSoundbarRpcClient,
)
from .models import SoundbarConfigEntry, SoundbarRuntimeData
from .ocf_credentials import OcfCredentialError, OcfCredentialStore
from .ocf_media import MEDIA_HREFS
from .ocf_repairs import update_local_configuration_issue, update_ocf_issue
from .ocf_state import DIAGNOSTIC_HREFS, POLL_HREFS
from .services import async_register_entity_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["media_player", "switch", "number", "select", "sensor"]

DUMP_OCF_STATUS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_HA_DEVICE_ID): cv.string,
        vol.Optional("preset", default="audio"): vol.In(("audio", "media")),
        vol.Optional("hrefs"): vol.All(
            cv.ensure_list,
            vol.Length(max=len(DIAGNOSTIC_HREFS)),
            [vol.In(DIAGNOSTIC_HREFS)],
        ),
    }
)

DUMP_DISCOVERY_SNAPSHOT_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_HA_DEVICE_ID): cv.string,
        vol.Optional(CONF_INCLUDE_NULL, default=True): cv.boolean,
    }
)

DUMP_LOCAL_RPC_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_LOCAL_RPC_HOST): cv.string,
        vol.Optional(
            CONF_LOCAL_RPC_PORT,
            default=DEFAULT_LOCAL_RPC_PORT,
        ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
        vol.Optional(
            CONF_LOCAL_RPC_VERIFY_SSL,
            default=False,
        ): cv.boolean,
        vol.Optional(
            CONF_LOCAL_RPC_TIMEOUT,
            default=DEFAULT_LOCAL_RPC_TIMEOUT,
        ): vol.All(vol.Coerce(float), vol.Range(min=1, max=60)),
        vol.Optional(CONF_LOCAL_RPC_METHODS): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(CONF_LOCAL_RPC_WRITE_METHOD): cv.string,
        vol.Optional(CONF_LOCAL_RPC_WRITE_PARAMS): dict,
    }
)


CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register actions independently of config-entry and platform setup."""
    _async_register_services(hass)
    async_register_entity_services(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: SoundbarConfigEntry) -> bool:
    """Migrate the existing entry in place without opening any transport."""
    return migrate_entry(hass, entry)


async def async_setup_entry(hass: HomeAssistant, entry: SoundbarConfigEntry) -> bool:
    """Set up core RPC first; optional OCF must never block entry startup."""
    clear_legacy_issues(hass, entry)
    device_id = entry.data.get(CONF_ENTRY_DEVICE_ID)
    local_host = str(get_entry_option(entry, CONF_LOCAL_HOST) or "").strip()
    control_mode = get_entry_option(entry, CONF_CONTROL_MODE)
    identity = entry.options.get(CONF_LOCAL_IDENTITY) or entry.data.get(
        CONF_LOCAL_IDENTITY
    )
    if (
        not device_id
        or not local_host
        or control_mode not in CONTROL_MODES
        or not has_stable_identity(identity)
    ):
        update_local_configuration_issue(hass, entry, True)
        raise ConfigEntryNotReady(
            "Reconfigure this soundbar entry with its local host and confirmed identity; "
            "the original device ID will be retained"
        )
    update_local_configuration_issue(hass, entry, False)
    _async_remove_legacy_artwork_entity(hass, entry)
    async_remove_woofer_buttons(hass, entry)
    verify_ssl = get_entry_option(entry, CONF_LOCAL_VERIFY_SSL)
    local_rpc = LocalSoundbarRpcClient(
        local_host,
        async_get_clientsession(hass, verify_ssl=verify_ssl),
        port=get_entry_option(entry, CONF_LOCAL_PORT),
        verify_ssl=verify_ssl,
        timeout=get_entry_option(entry, CONF_LOCAL_TIMEOUT),
    )
    soundbar_device = SoundbarDevice(
        device=LocalDevice(device_id),
        session=async_get_clientsession(hass),
        max_volume=get_entry_option(entry, CONF_ENTRY_MAX_VOLUME),
        device_name=entry.data.get(CONF_ENTRY_DEVICE_NAME) or local_host,
        control_mode=control_mode,
        local_rpc=local_rpc,
    )
    coordinator = SoundbarCoordinator(hass, entry, soundbar_device)
    entry.runtime_data = SoundbarRuntimeData(
        device=soundbar_device,
        coordinator=coordinator,
        config=dict(entry.data),
        options=dict(entry.options),
    )
    try:
        await coordinator.async_refresh()
        await _async_setup_ocf(hass, entry)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        try:
            await coordinator.async_shutdown()
        except Exception:  # noqa: BLE001 - preserve the setup failure, without secret-bearing traceback
            _LOGGER.error(
                "Could not finish local transport cleanup for entry %s", entry.entry_id
            )
        finally:
            del entry.runtime_data
        raise
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_remove_entry(hass: HomeAssistant, entry: SoundbarConfigEntry) -> None:
    """An explicit entry deletion removes only that entry's private credential."""
    clear_legacy_issues(hass, entry)
    update_ocf_issue(hass, entry, None)
    update_local_configuration_issue(hass, entry, False)
    await OcfCredentialStore(hass, entry.entry_id).async_remove()


def _async_remove_legacy_artwork_entity(
    hass: HomeAssistant, entry: SoundbarConfigEntry
) -> None:
    """Remove the old image entity without touching media player artwork."""
    device_id = entry.data.get(CONF_ENTRY_DEVICE_ID)
    if not device_id:
        return
    registry = er.async_get(hass)
    unique_id = f"{device_id}_sw_Image URL"
    if entity_id := registry.async_get_entity_id("image", DOMAIN, unique_id):
        entity = registry.async_get(entity_id)
        if entity is not None and entity.config_entry_id == entry.entry_id:
            registry.async_remove(entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: SoundbarConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        try:
            await entry.runtime_data.coordinator.async_shutdown()
        finally:
            del entry.runtime_data
    return unload_ok


async def _async_setup_ocf(hass, entry):
    """RPC-only never opens the Store, even when a saved credential is retained."""
    if get_entry_option(entry, CONF_CONTROL_MODE) != CONTROL_MODE_HYBRID_LOCAL_OCF:
        update_ocf_issue(hass, entry, None)
        return
    if (
        entry.data.get(CONF_OCF_CONFIGURED) is not True
        and entry.options.get(CONF_OCF_CONFIGURED) is not True
    ):
        update_ocf_issue(hass, entry, "missing_credential")
        return
    try:
        credential = await OcfCredentialStore(hass, entry.entry_id).async_load()
        if credential is None:
            update_ocf_issue(hass, entry, "missing_credential")
            return
    except OcfCredentialError:
        update_ocf_issue(hass, entry, "credential")
        return
    update_ocf_issue(hass, entry, None)
    entry.runtime_data.ocf = LocalSoundbarOcfClient(
        hass,
        str(get_entry_option(entry, CONF_LOCAL_HOST)).strip(),
        credential,
        on_error=partial(update_ocf_issue, hass, entry),
    )
    entry.runtime_data.coordinator.configure_ocf(entry.runtime_data.ocf)


async def async_reload_entry(hass: HomeAssistant, entry: SoundbarConfigEntry) -> None:
    runtime = getattr(entry, "runtime_data", None)
    if (
        runtime is not None
        and runtime.config == entry.data
        and runtime.options == entry.options
    ):
        return
    await hass.config_entries.async_reload(entry.entry_id)


def _async_register_services(hass: HomeAssistant) -> None:
    """Register only local diagnostic actions."""
    for name, handler, schema in (
        (
            SERVICE_DUMP_OCF_STATUS,
            _async_create_dump_ocf_status_service,
            DUMP_OCF_STATUS_SCHEMA,
        ),
        (
            SERVICE_DUMP_DISCOVERY_SNAPSHOT,
            _async_create_dump_discovery_snapshot_service,
            DUMP_DISCOVERY_SNAPSHOT_SCHEMA,
        ),
        (
            SERVICE_DUMP_LOCAL_RPC,
            _async_create_dump_local_rpc_service,
            DUMP_LOCAL_RPC_SCHEMA,
        ),
    ):
        if not hass.services.has_service(DOMAIN, name):
            hass.services.async_register(
                DOMAIN,
                name,
                handler(hass),
                schema=schema,
                supports_response=SupportsResponse.OPTIONAL,
            )


def _async_create_dump_discovery_snapshot_service(hass: HomeAssistant):
    async def async_dump_discovery_snapshot(call: ServiceCall) -> ServiceResponse:
        device = _async_resolve_service_device(hass, call.data.get(CONF_HA_DEVICE_ID))
        return await device.async_dump_discovery_snapshot(
            include_null=call.data[CONF_INCLUDE_NULL],
        )

    return async_dump_discovery_snapshot


def _async_create_dump_ocf_status_service(hass: HomeAssistant):
    async def async_dump_ocf_status(call: ServiceCall) -> ServiceResponse:
        device = _async_resolve_service_device(hass, call.data.get(CONF_HA_DEVICE_ID))
        try:
            return await device.coordinator.async_ocf_snapshot(
                call.data.get(
                    "hrefs",
                    MEDIA_HREFS if call.data["preset"] == "media" else POLL_HREFS,
                )
            )
        except OcfError:
            raise HomeAssistantError(
                "Local OCF diagnostic request could not be completed"
            ) from None

    return async_dump_ocf_status


def _async_create_dump_local_rpc_service(hass: HomeAssistant):
    """Create the local JSON-RPC dump service handler."""

    async def async_dump_local_rpc(call: ServiceCall) -> ServiceResponse:
        host = call.data[CONF_LOCAL_RPC_HOST]
        port = call.data[CONF_LOCAL_RPC_PORT]
        verify_ssl = call.data[CONF_LOCAL_RPC_VERIFY_SSL]
        timeout = call.data[CONF_LOCAL_RPC_TIMEOUT]
        methods = tuple(
            call.data.get(CONF_LOCAL_RPC_METHODS) or DEFAULT_LOCAL_RPC_METHODS
        )
        write_method = call.data.get(CONF_LOCAL_RPC_WRITE_METHOD)
        write_params = call.data.get(CONF_LOCAL_RPC_WRITE_PARAMS) or {}

        if write_method is not None and not isinstance(write_params, dict):
            raise HomeAssistantError("write_params must be an object")

        verification = None
        if write_method == "volumeControl":
            value = write_params.get("volume")
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= 100
            ):
                raise HomeAssistantError(
                    "volumeControl requires an integer volume in range 0-100"
                )
            verification = ("getVolume", "volume", value)
        elif write_method == "muteControl":
            value = write_params.get("mute")
            if not isinstance(value, bool):
                raise HomeAssistantError("muteControl requires a boolean mute value")
            verification = ("getMute", "mute", value)
        if verification is not None:
            methods = tuple(dict.fromkeys((*methods, verification[0])))

        session = async_get_clientsession(hass, verify_ssl=verify_ssl)
        client = LocalSoundbarRpcClient(
            host,
            session,
            port=port,
            verify_ssl=verify_ssl,
            timeout=timeout,
        )
        results: dict[str, object] = {}
        errors: dict[str, str] = {}
        post_write_results: dict[str, object] = {}
        post_write_errors: dict[str, str] = {}
        result: dict[str, object] = {
            "host": host,
            "port": port,
            "verify_ssl": verify_ssl,
            "timeout": timeout,
            "methods": list(methods),
            "token_created": False,
            "token_length": None,
            "results": results,
            "errors": errors,
            "error_codes": {},
            "write_probe": None,
            "write_result": None,
            "write_error": None,
            "write_error_code": None,
            "write_verification": None,
            "post_write_results": post_write_results,
            "post_write_errors": post_write_errors,
        }

        try:
            await client.create_token()
        except LocalRpcError as err:
            errors["createAccessToken"] = str(err)
            return result

        result["token_created"] = True
        result["token_length"] = client.token_length

        for method in methods:
            try:
                results[method] = await client.call(method)
            except LocalRpcError as err:
                errors[method] = str(err)
                result["error_codes"][method] = err.code

        if write_method is not None:
            result["write_probe"] = {
                "method": write_method,
                "params": _redact_rpc_params(write_params),
            }
            try:
                result["write_result"] = await client.call(write_method, write_params)
            except LocalRpcError as err:
                result["write_error"] = str(err)
                result["write_error_code"] = err.code

            for method in methods:
                try:
                    post_write_results[method] = await client.call(method)
                except LocalRpcError as err:
                    post_write_errors[method] = str(err)

            if verification is not None:
                method, field, requested = verification
                parse = client.parse_volume if field == "volume" else client.parse_mute
                readbacks = []
                for values in (results, post_write_results):
                    try:
                        readbacks.append(parse(values.get(method, {}).get(field)))
                    except LocalRpcError:
                        readbacks.append(None)
                before, after = readbacks
                result["write_verification"] = {
                    "field": field,
                    "requested": requested,
                    "before": before,
                    "after": after,
                    "readback_matches": result["write_error"] is None
                    and after is not None
                    and after == requested,
                    "state_change_observed": before is not None
                    and after is not None
                    and before != after,
                    "runtime_configuration_changed": False,
                }

        return result

    return async_dump_local_rpc


def _redact_rpc_params(value):
    """Redact token-like values from local RPC diagnostics."""
    if isinstance(value, dict):
        return {
            key: "***" if "token" in str(key).lower() else _redact_rpc_params(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_rpc_params(item) for item in value]
    return value


def _async_resolve_service_device(
    hass: HomeAssistant,
    ha_device_id: str | None,
) -> SoundbarDevice:
    """Resolve a registry device to its owning, loaded config entry."""
    entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED and hasattr(entry, "runtime_data")
    ]
    if not entries:
        raise HomeAssistantError("No Samsung Soundbar devices are loaded")
    if ha_device_id is None:
        if len(entries) == 1:
            return entries[0].runtime_data.device
        raise HomeAssistantError(
            "device_id is required when multiple soundbars are loaded"
        )

    device_entry = dr.async_get(hass).async_get(ha_device_id)
    if device_entry is None:
        raise HomeAssistantError(f"Unknown Home Assistant device_id: {ha_device_id}")
    for entry in entries:
        if entry.entry_id == device_entry.config_entry_id:
            return entry.runtime_data.device
    raise HomeAssistantError(
        "Selected device is not a loaded Samsung Soundbar integration device"
    )
