import logging
from types import SimpleNamespace

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_DEVICE_ID as CONF_HA_DEVICE_ID
from homeassistant.const import CONF_TOKEN
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import (
    config_validation as cv,
)
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers import (
    entity_registry as er,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pysmartthings.exceptions import (
    SmartThingsAuthenticationFailedError,
    SmartThingsConnectionError,
    SmartThingsError,
    SmartThingsForbiddenError,
    SmartThingsRateLimitError,
)

from .api_extension.smartthings_compat import (
    SmartThingsDeviceCompat,
    ensure_device_entity,
)
from .api_extension.SoundbarDevice import SoundbarDevice
from .auth import SmartThingsAuthProvider
from .cloud_errors import CloudAccessError, normalize_cloud_error
from .cloud_repairs import clear_cloud_issues, get_cloud_repairs
from .const import (
    CONF_CONTROL_MODE,
    CONF_ENTRY_DEVICE_ID,
    CONF_ENTRY_DEVICE_NAME,
    CONF_ENTRY_MAX_VOLUME,
    CONF_ENTRY_SETTINGS_ADVANCED_AUDIO_SWITCHES,
    CONF_ENTRY_SETTINGS_EQ_SELECTOR,
    CONF_ENTRY_SETTINGS_SOUNDMODE_SELECTOR,
    CONF_ENTRY_SETTINGS_WOOFER_NUMBER,
    CONF_EXECUTE_HREFS,
    CONF_HREF,
    CONF_INCLUDE_EXECUTE_STATUS,
    CONF_INCLUDE_FLATTENED_STATUS,
    CONF_INCLUDE_NULL,
    CONF_INCLUDE_RAW_STATUS,
    CONF_LOCAL_FALLBACK_TO_CLOUD,
    CONF_LOCAL_HOST,
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
    CONF_LOCATION_ID,
    CONF_PRESET,
    CONF_SUBSCRIPTION_ID,
    CONF_WRITE_PROPERTY,
    CONF_WRITE_VALUE,
    CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
    CONTROL_MODE_LOCAL_ONLY,
    DOMAIN,
    EXECUTE_PAYLOAD_PRESETS,
    SERVICE_DUMP_DISCOVERY_SNAPSHOT,
    SERVICE_DUMP_EXECUTE_PAYLOAD,
    SERVICE_DUMP_LOCAL_RPC,
    SERVICE_DUMP_STATUS_SUMMARY,
)
from .coordinator import SoundbarCoordinator
from .entry_options import get_entry_option
from .local_device import LocalDevice
from .local_rpc import (
    DEFAULT_LOCAL_RPC_METHODS,
    DEFAULT_LOCAL_RPC_PORT,
    DEFAULT_LOCAL_RPC_TIMEOUT,
    LocalRpcError,
    LocalSoundbarRpcClient,
)
from .models import SoundbarConfigEntry, SoundbarRuntimeData
from .services import async_register_entity_services
from .subscription import async_remove_subscription, async_setup_subscription

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["media_player", "switch", "number", "select", "sensor"]

DUMP_EXECUTE_PAYLOAD_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_HA_DEVICE_ID): cv.string,
        vol.Optional(CONF_HREF): cv.string,
        vol.Optional(CONF_PRESET, default="all"): vol.In(EXECUTE_PAYLOAD_PRESETS),
        vol.Optional(CONF_WRITE_PROPERTY): cv.string,
        vol.Optional(CONF_WRITE_VALUE): vol.Any(str, int, float, bool, dict, list),
    }
)

DUMP_STATUS_SUMMARY_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_HA_DEVICE_ID): cv.string,
        vol.Optional(CONF_INCLUDE_NULL, default=False): cv.boolean,
    }
)

DUMP_DISCOVERY_SNAPSHOT_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_HA_DEVICE_ID): cv.string,
        vol.Optional(CONF_INCLUDE_NULL, default=True): cv.boolean,
        vol.Optional(CONF_INCLUDE_RAW_STATUS, default=False): cv.boolean,
        vol.Optional(CONF_INCLUDE_FLATTENED_STATUS, default=True): cv.boolean,
        vol.Optional(CONF_INCLUDE_EXECUTE_STATUS, default=True): cv.boolean,
        vol.Optional(CONF_EXECUTE_HREFS): vol.All(cv.ensure_list, [cv.string]),
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


async def async_setup_entry(hass: HomeAssistant, entry: SoundbarConfigEntry) -> bool:
    """Report actionable Cloud failures without routing access refusals to reauth."""
    try:
        return await _async_setup_entry(hass, entry)
    except (
        ConfigEntryAuthFailed,
        ConfigEntryNotReady,
        CloudAccessError,
        SmartThingsForbiddenError,
        SmartThingsConnectionError,
        SmartThingsRateLimitError,
    ) as err:
        error = normalize_cloud_error(err)
        if get_entry_option(entry, CONF_CONTROL_MODE) != CONTROL_MODE_LOCAL_ONLY:
            get_cloud_repairs(hass, entry).failed(error)
        if isinstance(error, CloudAccessError):
            raise ConfigEntryNotReady(str(error)) from err
        if error is not err:
            raise error from err
        raise


async def async_remove_entry(hass: HomeAssistant, entry: SoundbarConfigEntry) -> None:
    """Remove Repairs belonging to an entry which the user deleted."""
    clear_cloud_issues(hass, entry)


async def _async_setup_entry(hass: HomeAssistant, entry: SoundbarConfigEntry) -> bool:
    """Set up Samsung Soundbar from config entry."""

    _LOGGER.info("[%s] Setting up entry", DOMAIN)
    _async_remove_legacy_artwork_entity(hass, entry)

    if get_entry_option(entry, CONF_CONTROL_MODE) == CONTROL_MODE_LOCAL_ONLY:
        clear_cloud_issues(hass, entry)
        return await _async_setup_local_only_entry(hass, entry)

    control_mode = get_entry_option(entry, CONF_CONTROL_MODE)
    hybrid = control_mode == CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS and bool(
        get_entry_option(entry, CONF_LOCAL_HOST)
    )
    if CONF_TOKEN not in entry.data and not hybrid:
        raise ConfigEntryAuthFailed(
            "Legacy SmartThings PAT entries must be reauthenticated with OAuth"
        )

    auth_provider = await SmartThingsAuthProvider.async_create(
        hass, entry, defer_auth=hybrid
    )
    api = auth_provider.api

    device_id = entry.data[CONF_ENTRY_DEVICE_ID]
    cloud_setup_error = None
    suggested_area = None
    try:
        _LOGGER.debug(
            "[%s] Validating SmartThings authentication for device %s",
            DOMAIN,
            device_id,
        )

        smart_things_device = ensure_device_entity(
            api,
            await api.get_device(device_id),
        )
        location_id = (
            entry.data.get(CONF_LOCATION_ID) or smart_things_device.location_id
        )
        if location_id != entry.data.get(CONF_LOCATION_ID):
            hass.config_entries.async_update_entry(
                entry,
                data={**entry.data, CONF_LOCATION_ID: location_id},
            )
        if smart_things_device.room_id:
            try:
                room = await api.get_room(
                    location_id,
                    smart_things_device.room_id,
                )
            except (SmartThingsError, HomeAssistantError) as err:
                _LOGGER.debug(
                    "[%s] Could not load SmartThings room metadata for %s: %s",
                    DOMAIN,
                    device_id,
                    err,
                )
            else:
                suggested_area = room.name

    except (
        SmartThingsAuthenticationFailedError,
        SmartThingsForbiddenError,
        SmartThingsRateLimitError,
        ConfigEntryAuthFailed,
        ConfigEntryNotReady,
        CloudAccessError,
        SmartThingsConnectionError,
    ) as err:
        cloud_setup_error = normalize_cloud_error(err)
        if isinstance(err, SmartThingsAuthenticationFailedError):
            cloud_setup_error = ConfigEntryAuthFailed(
                "SmartThings authorization is no longer valid"
            )
        if isinstance(err, SmartThingsConnectionError):
            cloud_setup_error = ConfigEntryNotReady(
                "SmartThings Cloud temporarily unavailable"
            )
        if not hybrid:
            raise cloud_setup_error from err
        # The existing entry ID is authoritative; no new physical identity is guessed.
        smart_things_device = SmartThingsDeviceCompat(
            api,
            SimpleNamespace(
                device_id=device_id,
                location_id=entry.data.get(CONF_LOCATION_ID),
                room_id=None,
            ),
        )

    except Exception:
        _LOGGER.exception(
            "[%s] Unexpected error while loading device %s",
            DOMAIN,
            device_id,
        )
        raise

    session = async_get_clientsession(hass)
    local_rpc_client = None
    if control_mode == CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS:
        local_host = str(get_entry_option(entry, CONF_LOCAL_HOST)).strip()
        if local_host:
            local_verify_ssl = get_entry_option(entry, CONF_LOCAL_VERIFY_SSL)
            local_rpc_client = LocalSoundbarRpcClient(
                local_host,
                async_get_clientsession(hass, verify_ssl=local_verify_ssl),
                port=get_entry_option(entry, CONF_LOCAL_PORT),
                verify_ssl=local_verify_ssl,
                timeout=get_entry_option(entry, CONF_LOCAL_TIMEOUT),
            )

    soundbar_device = SoundbarDevice(
        device=smart_things_device,
        session=session,
        auth_provider=auth_provider,
        max_volume=get_entry_option(entry, CONF_ENTRY_MAX_VOLUME),
        device_name=entry.data.get(CONF_ENTRY_DEVICE_NAME),
        enable_eq=get_entry_option(entry, CONF_ENTRY_SETTINGS_EQ_SELECTOR),
        enable_advanced_audio=get_entry_option(
            entry,
            CONF_ENTRY_SETTINGS_ADVANCED_AUDIO_SWITCHES,
        ),
        enable_soundmode=get_entry_option(
            entry,
            CONF_ENTRY_SETTINGS_SOUNDMODE_SELECTOR,
        ),
        enable_woofer=get_entry_option(entry, CONF_ENTRY_SETTINGS_WOOFER_NUMBER),
        control_mode=control_mode,
        local_rpc=local_rpc_client,
        local_fallback_to_cloud=get_entry_option(
            entry,
            CONF_LOCAL_FALLBACK_TO_CLOUD,
        ),
        suggested_area=suggested_area,
    )

    coordinator = SoundbarCoordinator(hass, entry, soundbar_device)
    if cloud_setup_error is not None:
        soundbar_device.cloud_setup_pending = True
        coordinator.cloud_failed(cloud_setup_error)
    await coordinator.async_refresh()

    entry.runtime_data = SoundbarRuntimeData(
        device=soundbar_device,
        coordinator=coordinator,
        config=dict(entry.data),
        options=dict(entry.options),
        api=api,
        auth_provider=auth_provider,
    )

    _LOGGER.info("[%s] Device initialized successfully", DOMAIN)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if entry.runtime_data.device.cloud_available:
        entry.runtime_data.subscription = await async_setup_subscription(
            hass,
            entry,
            api,
            entry.runtime_data.device,
        )

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


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


async def _async_setup_local_only_entry(
    hass: HomeAssistant, entry: SoundbarConfigEntry
) -> bool:
    """Load an existing entry without creating an OAuth or SmartThings client."""
    device_id = entry.data.get(CONF_ENTRY_DEVICE_ID)
    local_host = str(get_entry_option(entry, CONF_LOCAL_HOST)).strip()
    if not device_id or not local_host:
        raise ConfigEntryNotReady("Local soundbar device ID and host are required")

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
        auth_provider=None,
        max_volume=get_entry_option(entry, CONF_ENTRY_MAX_VOLUME),
        device_name=entry.data.get(CONF_ENTRY_DEVICE_NAME) or local_host,
        enable_eq=False,
        enable_advanced_audio=False,
        enable_soundmode=False,
        enable_woofer=False,
        control_mode=CONTROL_MODE_LOCAL_ONLY,
        local_rpc=local_rpc,
        local_fallback_to_cloud=False,
    )
    coordinator = SoundbarCoordinator(hass, entry, soundbar_device)
    await coordinator.async_refresh()
    entry.runtime_data = SoundbarRuntimeData(
        device=soundbar_device,
        coordinator=coordinator,
        config=dict(entry.data),
        options=dict(entry.options),
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SoundbarConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        runtime = entry.runtime_data
        await runtime.coordinator.async_shutdown()
        if runtime.subscription is not None:
            await async_remove_subscription(entry, runtime.subscription)
        del entry.runtime_data
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: SoundbarConfigEntry) -> None:
    runtime = getattr(entry, "runtime_data", None)
    if runtime is not None and not runtime.reauth_pending:
        ignored = {CONF_TOKEN, CONF_SUBSCRIPTION_ID}
        before = {
            key: value for key, value in runtime.config.items() if key not in ignored
        }
        after = {key: value for key, value in entry.data.items() if key not in ignored}
        if before == after and runtime.options == entry.options:
            return
    await hass.config_entries.async_reload(entry.entry_id)


def _async_register_services(hass: HomeAssistant) -> None:
    """Register integration-level services once."""
    if not hass.services.has_service(DOMAIN, SERVICE_DUMP_EXECUTE_PAYLOAD):
        hass.services.async_register(
            DOMAIN,
            SERVICE_DUMP_EXECUTE_PAYLOAD,
            _async_create_dump_execute_payload_service(hass),
            schema=DUMP_EXECUTE_PAYLOAD_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_DUMP_STATUS_SUMMARY):
        hass.services.async_register(
            DOMAIN,
            SERVICE_DUMP_STATUS_SUMMARY,
            _async_create_dump_status_summary_service(hass),
            schema=DUMP_STATUS_SUMMARY_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_DUMP_DISCOVERY_SNAPSHOT):
        hass.services.async_register(
            DOMAIN,
            SERVICE_DUMP_DISCOVERY_SNAPSHOT,
            _async_create_dump_discovery_snapshot_service(hass),
            schema=DUMP_DISCOVERY_SNAPSHOT_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_DUMP_LOCAL_RPC):
        hass.services.async_register(
            DOMAIN,
            SERVICE_DUMP_LOCAL_RPC,
            _async_create_dump_local_rpc_service(hass),
            schema=DUMP_LOCAL_RPC_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )


def _async_create_dump_execute_payload_service(hass: HomeAssistant):
    """Create the execute payload dump service handler."""

    async def async_dump_execute_payload(call: ServiceCall) -> ServiceResponse:
        soundbar_device = _async_resolve_service_device(
            hass,
            call.data.get(CONF_HA_DEVICE_ID),
        )
        _require_cloud_device(soundbar_device)

        href = call.data.get(CONF_HREF)
        preset = call.data.get(CONF_PRESET, "all")
        hrefs = (href,) if href else EXECUTE_PAYLOAD_PRESETS[preset]
        write_property = call.data.get(CONF_WRITE_PROPERTY)
        write_probe = None
        if write_property is not None:
            if href is None:
                raise HomeAssistantError("write_property requires href")
            if CONF_WRITE_VALUE not in call.data:
                raise HomeAssistantError("write_property requires write_value")
            write_probe = (write_property, call.data[CONF_WRITE_VALUE])

        result = await soundbar_device.async_dump_execute_payload(
            hrefs,
            write_probe=write_probe,
        )
        return {
            "preset": None if href else preset,
            "hrefs": list(hrefs),
            **result,
        }

    return async_dump_execute_payload


def _async_create_dump_status_summary_service(hass: HomeAssistant):
    """Create the status summary dump service handler."""

    async def async_dump_status_summary(call: ServiceCall) -> ServiceResponse:
        soundbar_device = _async_resolve_service_device(
            hass,
            call.data.get(CONF_HA_DEVICE_ID),
        )
        _require_cloud_device(soundbar_device)
        return await soundbar_device.async_dump_status_summary(
            include_null=call.data[CONF_INCLUDE_NULL],
        )

    return async_dump_status_summary


def _async_create_dump_discovery_snapshot_service(hass: HomeAssistant):
    """Create the raw discovery snapshot service handler."""

    async def async_dump_discovery_snapshot(call: ServiceCall) -> ServiceResponse:
        soundbar_device = _async_resolve_service_device(
            hass,
            call.data.get(CONF_HA_DEVICE_ID),
        )
        return await soundbar_device.async_dump_discovery_snapshot(
            include_null=call.data[CONF_INCLUDE_NULL],
            include_raw_status=call.data[CONF_INCLUDE_RAW_STATUS],
            include_flattened_status=call.data[CONF_INCLUDE_FLATTENED_STATUS],
            include_execute_status=call.data[CONF_INCLUDE_EXECUTE_STATUS],
            execute_hrefs=call.data.get(CONF_EXECUTE_HREFS) or (),
        )

    return async_dump_discovery_snapshot


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
                    "runtime_enabled": False,
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


def _require_cloud_device(device: SoundbarDevice) -> None:
    if device.local_only:
        raise HomeAssistantError(
            "This diagnostic action requires SmartThings Cloud; use dump_local_rpc"
        )


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
