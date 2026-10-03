"""Initial Local-only and Hybrid setup without accidental OAuth calls."""

from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_TOKEN
from homeassistant.helpers.config_entry_oauth2_flow import AbstractOAuth2FlowHandler

from custom_components.samsung_soundbar.config_flow import SamsungSoundbarConfigFlow
from custom_components.samsung_soundbar.const import (
    CONF_CONTROL_MODE,
    CONF_ENTRY_DEVICE_ID,
    CONF_ENTRY_DEVICE_NAME,
    CONF_LOCAL_HOST,
    CONF_LOCAL_IDENTITY,
    CONF_LOCAL_PORT,
    CONF_LOCAL_TIMEOUT,
    CONF_LOCAL_VERIFY_SSL,
    CONF_LOCATION_ID,
    CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
    CONTROL_MODE_LOCAL_ONLY,
    CONTROL_MODE_SMARTTHINGS_CLOUD,
)
from custom_components.samsung_soundbar.local_identity import LocalIdentityError
from custom_components.samsung_soundbar.local_rpc import LocalRpcError


IDENTITY = {
    "wifi_mac": "94:e6:ba:89:bd:ba",
    "tizen_duid": "uuid:f289154e-3c4a-41d8-ba8f-86850cf9be07",
}
LOCAL_INPUT = {
    CONF_LOCAL_HOST: "192.0.2.26",
    CONF_LOCAL_PORT: 1516,
    CONF_LOCAL_VERIFY_SSL: False,
    CONF_LOCAL_TIMEOUT: 8,
    CONF_ENTRY_DEVICE_NAME: "Soundbar Q800F",
}


class TestInitialLocalFlow(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.flow = SamsungSoundbarConfigFlow()
        self.flow.context = {"source": "user"}
        self.flow.hass = MagicMock()
        self.flow.hass.config_entries.async_entries.return_value = []
        self.flow.async_show_form = MagicMock(side_effect=lambda **kw: kw)
        self.flow.async_create_entry = MagicMock(side_effect=lambda **kw: kw)
        self.flow.async_abort = MagicMock(side_effect=lambda **kw: kw)
        self.flow.async_set_unique_id = AsyncMock()
        self.flow._abort_if_unique_id_configured = MagicMock()

        self.oauth = patch.object(AbstractOAuth2FlowHandler, "async_step_user", new_callable=AsyncMock)
        self.oauth_step = self.oauth.start()
        self.addCleanup(self.oauth.stop)
        self.oauth_step.return_value = {"step_id": "oauth"}
        self.identity = patch(
            "custom_components.samsung_soundbar.config_flow.async_read_local_identity",
            new_callable=AsyncMock,
        )
        self.read_identity = self.identity.start()
        self.addCleanup(self.identity.stop)
        self.read_identity.return_value = IDENTITY
        self.rpc = MagicMock()
        self.rpc.create_token = AsyncMock(return_value="memory-only-token")
        self.rpc.call = AsyncMock(side_effect=AssertionError("getIdentifier is not required"))
        self.rpc_patch = patch(
            "custom_components.samsung_soundbar.config_flow.LocalSoundbarRpcClient",
            return_value=self.rpc,
        )
        self.rpc_patch.start()
        self.addCleanup(self.rpc_patch.stop)
        self.session_patch = patch(
            "custom_components.samsung_soundbar.config_flow.async_get_clientsession",
            return_value=MagicMock(),
        )
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)

    async def test_local_only_skips_oauth_and_saves_stable_identity(self) -> None:
        start = await self.flow.async_step_user()
        self.assertEqual(start["step_id"], "user")
        selection = await self.flow.async_step_user({CONF_CONTROL_MODE: CONTROL_MODE_LOCAL_ONLY})
        self.assertEqual(selection["step_id"], "local")
        self.oauth_step.assert_not_awaited()

        result = await self.flow.async_step_local(LOCAL_INPUT)
        data = result["data"]
        device_id = "local:wifi_mac:94:e6:ba:89:bd:ba"
        self.assertEqual(data[CONF_ENTRY_DEVICE_ID], device_id)
        self.assertEqual(data[CONF_LOCAL_IDENTITY], IDENTITY)
        self.assertEqual(data[CONF_CONTROL_MODE], CONTROL_MODE_LOCAL_ONLY)
        self.assertNotIn(CONF_TOKEN, data)
        self.assertNotIn("access_token", str(data))
        self.flow.async_set_unique_id.assert_awaited_once_with(device_id)
        self.rpc.create_token.assert_awaited_once()
        self.rpc.call.assert_not_awaited()

    async def test_identity_fallback_and_powered_off_probe(self) -> None:
        await self.flow.async_step_user({CONF_CONTROL_MODE: CONTROL_MODE_LOCAL_ONLY})
        for key, value in (
            ("tizen_duid", "uuid:f289154e-3c4a-41d8-ba8f-86850cf9be07"),
            ("upnp_udn", "uuid:ffa77b59-a763-4caa-ba60-028de1be4bdf"),
        ):
            self.read_identity.return_value = {key: value}
            self.flow.async_set_unique_id.reset_mock()
            result = await self.flow.async_step_local(LOCAL_INPUT)
            self.assertEqual(result["data"][CONF_ENTRY_DEVICE_ID], f"local:{key}:{value}")
            self.flow.async_set_unique_id.assert_awaited_once()
        self.rpc.call.assert_not_awaited()

    async def test_identity_or_rpc_failure_does_not_create_entry(self) -> None:
        await self.flow.async_step_user({CONF_CONTROL_MODE: CONTROL_MODE_LOCAL_ONLY})
        self.read_identity.side_effect = LocalIdentityError("no ID")
        result = await self.flow.async_step_local(LOCAL_INPUT)
        self.assertEqual(result["errors"], {"base": "identity_unavailable"})
        self.rpc.create_token.assert_not_awaited()

        self.read_identity.side_effect = None
        self.rpc.create_token.side_effect = LocalRpcError("offline")
        result = await self.flow.async_step_local(LOCAL_INPUT)
        self.assertEqual(result["errors"], {"base": "local_cannot_connect"})
        self.flow.async_create_entry.assert_not_called()

    async def test_duplicate_identity_or_host_is_rejected(self) -> None:
        await self.flow.async_step_user({CONF_CONTROL_MODE: CONTROL_MODE_LOCAL_ONLY})
        existing = MagicMock()
        existing.options = {CONF_LOCAL_IDENTITY: {"wifi_mac": IDENTITY["wifi_mac"]}}
        existing.data = {}
        self.flow.hass.config_entries.async_entries.return_value = [existing]
        result = await self.flow.async_step_local(LOCAL_INPUT)
        self.assertEqual(result["reason"], "already_configured")

        existing.options = {CONF_LOCAL_HOST: LOCAL_INPUT[CONF_LOCAL_HOST]}
        self.read_identity.return_value = {"upnp_udn": "uuid:ffa77b59-a763-4caa-ba60-028de1be4bdf"}
        result = await self.flow.async_step_local(LOCAL_INPUT)
        self.assertEqual(result["reason"], "already_configured")
        self.flow.async_create_entry.assert_not_called()

    async def test_cloud_and_hybrid_paths_keep_oauth(self) -> None:
        result = await self.flow.async_step_user({CONF_CONTROL_MODE: CONTROL_MODE_SMARTTHINGS_CLOUD})
        self.assertEqual(result["step_id"], "oauth")
        self.flow._oauth_data = {CONF_TOKEN: {"access_token": "test-token"}}
        self.flow._device_locations = {"cloud-id": "location-id"}
        cloud = await self.flow.async_step_device(
            {CONF_ENTRY_DEVICE_ID: "cloud-id", CONF_ENTRY_DEVICE_NAME: "Soundbar"}
        )
        self.assertEqual(cloud["data"][CONF_CONTROL_MODE], CONTROL_MODE_SMARTTHINGS_CLOUD)

        self.flow.async_set_unique_id.reset_mock()
        await self.flow.async_step_user({CONF_CONTROL_MODE: CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS})
        hybrid_form = await self.flow.async_step_device(
            {CONF_ENTRY_DEVICE_ID: "cloud-id", CONF_ENTRY_DEVICE_NAME: "Soundbar"}
        )
        self.assertEqual(hybrid_form["step_id"], "local")
        hybrid = await self.flow.async_step_local(LOCAL_INPUT)
        self.assertEqual(hybrid["data"][CONF_CONTROL_MODE], CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS)
        self.assertEqual(hybrid["data"][CONF_LOCATION_ID], "location-id")
        self.assertEqual(hybrid["data"][CONF_LOCAL_IDENTITY], IDENTITY)

    async def test_reauth_preserves_initial_hybrid_local_settings(self) -> None:
        entry = MagicMock()
        entry.data = {
            CONF_ENTRY_DEVICE_ID: "cloud-id",
            CONF_ENTRY_DEVICE_NAME: "Soundbar",
            CONF_CONTROL_MODE: CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
            CONF_LOCAL_HOST: "192.0.2.26",
            CONF_LOCAL_PORT: 1516,
            CONF_LOCAL_VERIFY_SSL: False,
            CONF_LOCAL_TIMEOUT: 8,
            CONF_LOCAL_IDENTITY: IDENTITY,
        }
        self.flow._devices = {"cloud-id": "Soundbar"}
        self.flow._device_locations = {"cloud-id": "location-id"}
        self.flow._get_reauth_entry = MagicMock(return_value=entry)
        self.flow._abort_if_unique_id_mismatch = MagicMock()
        entry.runtime_data = None
        entry.update_listeners = []
        self.flow.async_update_and_abort = MagicMock(
            side_effect=lambda _entry, **kw: kw
        )
        result = await self.flow._async_finish_reauth({CONF_TOKEN: {"access_token": "new"}})
        self.assertEqual(result["data"][CONF_CONTROL_MODE], CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS)
        self.assertEqual(result["data"][CONF_LOCAL_IDENTITY], IDENTITY)
        self.assertEqual(result["data"][CONF_LOCAL_HOST], "192.0.2.26")
        self.assertEqual(result["data"][CONF_TOKEN]["access_token"], "new")

    async def test_reauth_skips_mode_selection(self) -> None:
        self.flow.context = {"source": "reauth"}
        result = await self.flow.async_step_user()
        self.assertEqual(result["step_id"], "oauth")
        self.oauth_step.assert_awaited_once()
