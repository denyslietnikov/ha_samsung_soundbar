"""Register media-player actions independently of platform setup."""

import voluptuous as vol
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.service import async_register_platform_entity_service

from .const import DOMAIN

ENTITY_SERVICES = (
    ("select_soundmode", {vol.Required("sound_mode"): str}, "async_select_sound_mode"),
    (
        "set_woofer_level",
        {vol.Required("level"): vol.All(int, vol.Range(min=-12, max=6))},
        "async_set_woofer_level",
    ),
    ("set_night_mode", {vol.Required("enabled"): bool}, "async_set_night_mode"),
    ("set_bass_enhancer", {vol.Required("enabled"): bool}, "async_set_bass_mode"),
    ("set_voice_enhancer", {vol.Required("enabled"): bool}, "async_set_voice_mode"),
    (
        "set_space_fit_sound",
        {vol.Required("enabled"): bool},
        "async_set_space_fit_sound",
    ),
)


@callback
def async_register_entity_services(hass: HomeAssistant) -> None:
    """Keep the existing action names, schemas and entity targets."""
    for name, schema, method in ENTITY_SERVICES:
        async_register_platform_entity_service(
            hass,
            DOMAIN,
            name,
            entity_domain="media_player",
            schema=schema,
            func=method,
        )
