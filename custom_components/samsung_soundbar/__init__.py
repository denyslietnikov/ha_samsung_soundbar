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
    CONF_ENTRY_DEVICE_ID,
    CONF_ENTRY_DEVICE_NAME,
    CONF_ENTRY_MAX_VOLUME,
    CONF_ENTRY_SETTINGS_ADVANCED_AUDIO_SWITCHES,
    CONF_ENTRY_SETTINGS_EQ_SELECTOR,
    CONF_ENTRY_SETTINGS_SOUNDMODE_SELECTOR,
    CONF_ENTRY_SETTINGS_WOOFER_NUMBER,
    CONF_HREF,
    CONF_PRESET,
    DOMAIN,
    EXECUTE_PAYLOAD_PRESETS,
    SERVICE_DUMP_EXECUTE_PAYLOAD,
)
from .models import DeviceConfig, SoundbarConfig

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["media_player", "switch", "image", "number", "select", "sensor"]

DUMP_EXECUTE_PAYLOAD_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_HA_DEVICE_ID): cv.string,
        vol.Optional(CONF_HREF): cv.string,
        vol.Optional(CONF_PRESET, default="all"): vol.In(EXECUTE_PAYLOAD_PRESETS),
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

        soundbar_device = SoundbarDevice(
            device=smart_things_device,
            session=session,
            auth_provider=auth_provider,
            max_volume=entry.options.get(CONF_ENTRY_MAX_VOLUME, 100),
            device_name=entry.data.get(CONF_ENTRY_DEVICE_NAME),
            enable_eq=entry.options.get(CONF_ENTRY_SETTINGS_EQ_SELECTOR, False),
            enable_advanced_audio=entry.options.get(
                CONF_ENTRY_SETTINGS_ADVANCED_AUDIO_SWITCHES, False
            ),
            enable_soundmode=entry.options.get(
                CONF_ENTRY_SETTINGS_SOUNDMODE_SELECTOR, False
            ),
            enable_woofer=entry.options.get(
                CONF_ENTRY_SETTINGS_WOOFER_NUMBER, False
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
                hass.data.pop(DOMAIN, None)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _async_register_services(hass: HomeAssistant) -> None:
    """Register integration-level services once."""
    if hass.services.has_service(DOMAIN, SERVICE_DUMP_EXECUTE_PAYLOAD):
        return

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

        result = await soundbar_device.async_dump_execute_payload(hrefs)
        return {
            "preset": None if href else preset,
            "hrefs": list(hrefs),
            **result,
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_DUMP_EXECUTE_PAYLOAD,
        async_dump_execute_payload,
        schema=DUMP_EXECUTE_PAYLOAD_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


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
