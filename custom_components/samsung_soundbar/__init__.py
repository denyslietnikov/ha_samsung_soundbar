import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_DEVICE_ID as CONF_HA_DEVICE_ID, CONF_TOKEN
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
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pysmartthings.exceptions import (
    SmartThingsAuthenticationFailedError,
    SmartThingsConnectionError,
    SmartThingsForbiddenError,
)
import voluptuous as vol

from .api_extension.SoundbarDevice import SoundbarDevice
from .api_extension.smartthings_compat import ensure_device_entity
from .auth import SmartThingsAuthProvider
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
    CONF_HREF,
    CONF_INCLUDE_NULL,
    CONF_LOCAL_TIMEOUT,
    CONF_LOCAL_VERIFY_SSL,
    CONF_LOCAL_RPC_HOST,
    CONF_LOCAL_RPC_METHODS,
    CONF_LOCAL_RPC_PORT,
    CONF_LOCAL_RPC_TIMEOUT,
    CONF_LOCAL_RPC_VERIFY_SSL,
    CONF_LOCAL_RPC_WRITE_METHOD,
    CONF_LOCAL_RPC_WRITE_PARAMS,
    CONF_PRESET,
    CONF_WRITE_PROPERTY,
    CONF_WRITE_VALUE,
    CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
    DOMAIN,
    EXECUTE_PAYLOAD_PRESETS,
    SERVICE_DUMP_EXECUTE_PAYLOAD,
    SERVICE_DUMP_LOCAL_RPC,
    SERVICE_DUMP_STATUS_SUMMARY,
)
from .entry_options import get_entry_option
from .local_rpc import (
    DEFAULT_LOCAL_RPC_METHODS,
    DEFAULT_LOCAL_RPC_PORT,
    DEFAULT_LOCAL_RPC_TIMEOUT,
    LocalRpcError,
    LocalSoundbarRpcClient,
)
from .models import DeviceConfig, SoundbarConfig

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["media_player", "switch", "image", "number", "select", "sensor"]

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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Samsung Soundbar from config entry."""

    _LOGGER.info("[%s] Setting up entry", DOMAIN)

    if CONF_TOKEN not in entry.data:
        raise ConfigEntryAuthFailed(
            "Legacy SmartThings PAT entries must be reauthenticated with OAuth"
        )

    auth_provider = await SmartThingsAuthProvider.async_create(hass, entry)
    api = auth_provider.api

    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = SoundbarConfig(api, {}, auth_provider)

    domain_config: SoundbarConfig = hass.data[DOMAIN]
    domain_config.api = api
    domain_config.auth_provider = auth_provider

    _async_register_services(hass)

    device_id = entry.data.get(CONF_ENTRY_DEVICE_ID)

    if device_id not in domain_config.devices:

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

        except (
            SmartThingsAuthenticationFailedError,
            SmartThingsForbiddenError,
        ) as err:
            _LOGGER.error(
                "[%s] SmartThings authentication failed. "
                "The token may have expired, been revoked, or lost access to "
                "the configured soundbar.",
                DOMAIN,
            )

            raise ConfigEntryAuthFailed(
                "SmartThings authorization is no longer valid"
            ) from err

        except SmartThingsConnectionError as err:
            _LOGGER.warning(
                "[%s] SmartThings service is temporarily unavailable while "
                "loading device %s: %s",
                DOMAIN,
                device_id,
                err,
            )
            raise ConfigEntryNotReady(
                "SmartThings service is temporarily unavailable"
            ) from err

        except Exception as err:
            _LOGGER.exception(
                "[%s] Unexpected error while loading device %s",
                DOMAIN,
                device_id,
            )
            raise

        session = async_get_clientsession(hass)
        control_mode = get_entry_option(entry, CONF_CONTROL_MODE)
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
        )

        await soundbar_device.update()

        domain_config.devices[device_id] = DeviceConfig(
            entry.data,
            soundbar_device,
        )

        _LOGGER.info("[%s] Device initialized successfully", DOMAIN)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        domain_data = hass.data.get(DOMAIN)
        if domain_data:
            domain_data.devices.pop(entry.data.get(CONF_ENTRY_DEVICE_ID), None)
            if not domain_data.devices:
                hass.services.async_remove(DOMAIN, SERVICE_DUMP_EXECUTE_PAYLOAD)
                hass.services.async_remove(DOMAIN, SERVICE_DUMP_LOCAL_RPC)
                hass.services.async_remove(DOMAIN, SERVICE_DUMP_STATUS_SUMMARY)
                hass.data.pop(DOMAIN, None)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
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
        domain_config: SoundbarConfig | None = hass.data.get(DOMAIN)
        if domain_config is None or not domain_config.devices:
            raise HomeAssistantError("No Samsung Soundbar devices are loaded")

        soundbar_device = _async_resolve_service_device(
            hass,
            domain_config,
            call.data.get(CONF_HA_DEVICE_ID),
        )

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
        domain_config: SoundbarConfig | None = hass.data.get(DOMAIN)
        if domain_config is None or not domain_config.devices:
            raise HomeAssistantError("No Samsung Soundbar devices are loaded")

        soundbar_device = _async_resolve_service_device(
            hass,
            domain_config,
            call.data.get(CONF_HA_DEVICE_ID),
        )
        return await soundbar_device.async_dump_status_summary(
            include_null=call.data[CONF_INCLUDE_NULL],
        )

    return async_dump_status_summary


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
            "write_probe": None,
            "write_result": None,
            "write_error": None,
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

        if write_method is not None:
            result["write_probe"] = {
                "method": write_method,
                "params": _redact_rpc_params(write_params),
            }
            try:
                result["write_result"] = await client.call(write_method, write_params)
            except LocalRpcError as err:
                result["write_error"] = str(err)

            for method in methods:
                try:
                    post_write_results[method] = await client.call(method)
                except LocalRpcError as err:
                    post_write_errors[method] = str(err)

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
    domain_config: SoundbarConfig,
    ha_device_id: str | None,
) -> SoundbarDevice:
    """Resolve a Home Assistant device id to a loaded SoundbarDevice."""
    if ha_device_id is None:
        if len(domain_config.devices) == 1:
            return next(iter(domain_config.devices.values())).device
        raise HomeAssistantError(
            "device_id is required when multiple soundbars are loaded"
        )

    device_registry = dr.async_get(hass)
    device_entry = device_registry.async_get(ha_device_id)
    if device_entry is None:
        raise HomeAssistantError(f"Unknown Home Assistant device_id: {ha_device_id}")

    for identifier_domain, identifier in device_entry.identifiers:
        if identifier_domain != DOMAIN:
            continue
        device_config = domain_config.devices.get(identifier)
        if device_config is not None:
            return device_config.device

    raise HomeAssistantError(
        "Selected device is not a loaded Samsung Soundbar integration device"
    )
