"""Recover configured local hosts without creating or merging devices."""

from __future__ import annotations

import asyncio
import logging
from ipaddress import IPv4Address

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from .const import (
    CONF_CONTROL_MODE,
    CONF_LOCAL_HOST,
    CONF_LOCAL_IDENTITY,
    CONF_LOCAL_TIMEOUT,
    CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
    CONTROL_MODE_LOCAL_ONLY,
    DOMAIN,
)
from .entry_options import get_entry_option
from .local_identity import (
    LocalIdentityError,
    async_read_local_identity,
    identities_match,
    normalize_mac,
)

_LOGGER = logging.getLogger(__name__)
_LOCK_KEY = f"{DOMAIN}_dhcp_recovery_lock"


def _local_config(entry: ConfigEntry) -> tuple[object, str, dict[str, str]]:
    """Snapshot the identity and effective settings before an awaited probe."""
    identity = entry.options.get(CONF_LOCAL_IDENTITY) or entry.data.get(
        CONF_LOCAL_IDENTITY
    )
    return (
        get_entry_option(entry, CONF_CONTROL_MODE),
        str(get_entry_option(entry, CONF_LOCAL_HOST)).strip(),
        dict(identity) if isinstance(identity, dict) else {},
    )


def _matching_entries(hass: HomeAssistant, mac: str) -> list[ConfigEntry]:
    entries = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        mode, host, identity = _local_config(entry)
        if (
            entry.disabled_by is None
            and mode in (CONTROL_MODE_LOCAL_ONLY, CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS)
            and host
            and normalize_mac(identity.get("wifi_mac")) == mac
        ):
            entries.append(entry)
    return entries


async def async_recover_dhcp_host(
    hass: HomeAssistant, discovery_info: DhcpServiceInfo
) -> str:
    """Accept a DHCP locator only after read-only endpoint identity verification."""
    mac = normalize_mac(discovery_info.macaddress)
    try:
        address = IPv4Address(discovery_info.ip)
    except ValueError:
        return "dhcp_not_configured"
    if not mac or address.is_unspecified or address.is_multicast or address.is_loopback:
        return "dhcp_not_configured"
    host = str(address)

    # Manufacturer/hostname discovery must never probe unrelated LAN devices.
    if not _matching_entries(hass, mac):
        return "dhcp_not_configured"

    lock = hass.data.setdefault(_LOCK_KEY, asyncio.Lock())
    async with lock:
        reason = "already_configured"
        for entry in _matching_entries(hass, mac):
            original = _local_config(entry)
            _, old_host, saved = original
            if old_host == host:
                continue
            try:
                observed = await async_read_local_identity(
                    async_get_clientsession(hass),
                    host,
                    get_entry_option(entry, CONF_LOCAL_TIMEOUT),
                )
            except LocalIdentityError:
                _LOGGER.debug("DHCP identity unavailable for entry %s", entry.entry_id)
                reason = "dhcp_identity_unavailable"
                continue

            if (
                hass.config_entries.async_get_entry(entry.entry_id) is not entry
                or entry.disabled_by is not None
                or _local_config(entry) != original
            ):
                reason = "dhcp_configuration_changed"
                continue
            if not identities_match(saved, observed):
                _LOGGER.warning(
                    "Ignoring DHCP host %s: local identity mismatch for entry %s",
                    host,
                    entry.entry_id,
                )
                reason = "dhcp_identity_mismatch"
                continue

            data = {**entry.data, CONF_LOCAL_HOST: host}
            options = {**entry.options, CONF_LOCAL_HOST: host}
            # Keep identifiers from an unavailable endpoint for the next recovery.
            identity_storage = options if CONF_LOCAL_IDENTITY in entry.options else data
            identity_storage[CONF_LOCAL_IDENTITY] = {**saved, **observed}
            changed = hass.config_entries.async_update_entry(
                entry, data=data, options=options
            )
            if changed and not entry.update_listeners:
                # Loaded entries reload through their existing update listener.
                # Failed setup entries have no listener and need a fresh attempt.
                hass.config_entries.async_schedule_reload(entry.entry_id)
            if changed:
                _LOGGER.info(
                    "Recovered local host for entry %s: %s -> %s",
                    entry.entry_id,
                    old_host,
                    host,
                )
                reason = "dhcp_host_updated"
        return reason
