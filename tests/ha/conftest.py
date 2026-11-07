"""Real HA fixtures; replace only local transports and identity discovery."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsung_soundbar.const import (
    CONF_CONTROL_MODE,
    CONF_ENTRY_DEVICE_ID,
    CONF_ENTRY_DEVICE_NAME,
    CONF_LOCAL_HOST,
    CONF_LOCAL_IDENTITY,
    CONF_LOCAL_PORT,
    CONTROL_MODE_LOCAL_ONLY,
    DOMAIN,
)

IDENTITY = {
    "wifi_mac": "94:e6:ba:89:bd:ba",
    "tizen_duid": "uuid:f289154e-3c4a-41d8-ba8f-86850cf9be07",
}
LOCAL_STATUS = {
    "power": "powerOn",
    "volume": 8,
    "mute": False,
    "input_source": "E_ARC",
    "sound_mode": "GAME",
    "codec": "PCM",
    "identifier": "22_AV_HW-Q800F",
}


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    """Use this checkout's real integration, platforms and flows."""


@pytest.fixture
def entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Soundbar Q800F",
        unique_id="existing-cloud-id",
        version=1,
        minor_version=1,
        data={
            CONF_ENTRY_DEVICE_ID: "existing-cloud-id",
            CONF_ENTRY_DEVICE_NAME: "Soundbar Q800F",
        },
        options={
            CONF_CONTROL_MODE: CONTROL_MODE_LOCAL_ONLY,
            CONF_LOCAL_HOST: "192.0.2.26",
            CONF_LOCAL_PORT: 1516,
            CONF_LOCAL_IDENTITY: dict(IDENTITY),
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture(autouse=True)
def transports():
    rpc = MagicMock()
    rpc.status = AsyncMock(return_value=LOCAL_STATUS)
    rpc.power_state = AsyncMock(return_value="powerOn")
    rpc.volume = AsyncMock(return_value=8)
    rpc.is_muted = AsyncMock(return_value=False)
    rpc.input_source = AsyncMock(return_value="E_ARC")
    rpc.sound_mode = AsyncMock(return_value="GAME")
    rpc.set_volume = AsyncMock()
    rpc.set_sound_mode = AsyncMock()
    rpc.select_source = AsyncMock()
    http = MagicMock()
    http.get = AsyncMock(side_effect=AssertionError("Unexpected real Cloud GET"))
    http.post = AsyncMock(side_effect=AssertionError("Unexpected real Cloud POST"))
    http.request = AsyncMock(
        side_effect=AssertionError("Unexpected real Cloud request")
    )
    with (
        patch(
            "custom_components.samsung_soundbar.LocalSoundbarRpcClient",
            return_value=rpc,
        ) as factory,
        patch(
            "custom_components.samsung_soundbar.async_get_clientsession",
            return_value=http,
        ),
        patch(
            "custom_components.samsung_soundbar.config_flow.LocalSoundbarRpcClient",
            return_value=rpc,
        ),
        patch(
            "custom_components.samsung_soundbar.config_flow.async_get_clientsession",
            return_value=http,
        ),
        patch(
            "custom_components.samsung_soundbar.config_flow.async_read_local_identity",
            new_callable=AsyncMock,
            return_value=IDENTITY,
        ),
        patch(
            "custom_components.samsung_soundbar.dhcp_recovery.async_get_clientsession",
            return_value=http,
        ),
        patch(
            "custom_components.samsung_soundbar.dhcp_recovery.async_read_local_identity",
            new_callable=AsyncMock,
            return_value=IDENTITY,
        ),
    ):
        rpc.create_token = AsyncMock()
        rpc.call = AsyncMock(return_value={"identifier": "22_AV_HW-Q800F"})
        yield SimpleNamespace(rpc=rpc, factory=factory, http=http)
