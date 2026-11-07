"""Entry-only migration; never contact a service or open private OCF storage."""

from homeassistant.helpers import issue_registry as ir

from .const import (
    CONF_CONTROL_MODE,
    CONF_OCF_CONFIGURED,
    CONTROL_MODE_HYBRID_LOCAL_OCF,
    CONTROL_MODE_LOCAL_ONLY,
    DOMAIN,
)

ENTRY_VERSION = 2
ENTRY_MINOR_VERSION = 0

_OBSOLETE_KEYS = {
    "token",
    "access_token",
    "refresh_token",
    "expires_at",
    "expires_in",
    "token_type",
    "scope",
    "api_key",
    "auth_implementation",
    "auth",
    "oauth",
    "oauth2",
    "client_id",
    "client_secret",
    "cloud_integration",
    "location_id",
    "installed_app_id",
    "subscription_id",
    "subscriptions",
    "local_fallback_to_cloud",
    "settings_eq",
    "settings_advanced_audio",
    "settings_soundmode",
    "settings_woofer",
}


def clear_legacy_issues(hass, entry):
    """Remove only this entry's obsolete local issue records."""
    for kind in ("auth", "access", "payment", "limit", "unavailable"):
        ir.async_delete_issue(hass, DOMAIN, f"cloud_{kind}_{entry.entry_id}")
    hass.data.get(f"{DOMAIN}_cloud_repairs", {}).pop(entry.entry_id, None)


def migrate_entry(hass, entry):
    """Keep the original IDs and OCF marker while retiring entry auth data."""
    if entry.version > ENTRY_VERSION:
        return False
    if entry.version == ENTRY_VERSION:
        return True
    data = {
        key: value for key, value in entry.data.items() if key not in _OBSOLETE_KEYS
    }
    options = {
        key: value for key, value in entry.options.items() if key not in _OBSOLETE_KEYS
    }
    mode = options.get(CONF_CONTROL_MODE, data.get(CONF_CONTROL_MODE))
    configured = (
        data.get(CONF_OCF_CONFIGURED) is True
        or options.get(CONF_OCF_CONFIGURED) is True
    )
    mode = (
        CONTROL_MODE_HYBRID_LOCAL_OCF
        if mode in ("hybrid_local_smartthings", CONTROL_MODE_HYBRID_LOCAL_OCF)
        or configured
        else CONTROL_MODE_LOCAL_ONLY
    )
    data[CONF_CONTROL_MODE] = options[CONF_CONTROL_MODE] = mode
    hass.config_entries.async_update_entry(
        entry,
        data=data,
        options=options,
        version=ENTRY_VERSION,
        minor_version=ENTRY_MINOR_VERSION,
    )
    clear_legacy_issues(hass, entry)
    return True
