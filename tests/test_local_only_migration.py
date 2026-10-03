"""Tests for binding a legacy entry to local-only control."""

from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_TOKEN

from custom_components.samsung_soundbar.config_flow import (
    SamsungSoundbarOptionsFlowHandler,
)
from custom_components.samsung_soundbar.const import (
    CONF_CONTROL_MODE,
    CONF_ENTRY_DEVICE_ID,
    CONF_ENTRY_DEVICE_NAME,
    CONF_LOCAL_HOST,
    CONF_LOCAL_IDENTITY,
    CONF_LOCATION_ID,
    CONF_SUBSCRIPTION_ID,
    CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
    CONTROL_MODE_LOCAL_ONLY,
    CONTROL_MODE_SMARTTHINGS_CLOUD,
)
from custom_components.samsung_soundbar.local_identity import LocalIdentityError

IDENTITY = {"wifi_mac": "94:e6:ba:89:bd:ba", "tizen_duid": "uuid:f289154e-3c4a-41d8-ba8f-86850cf9be07"}


class TestLocalOnlyMigration(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.entry = MagicMock()
        self.entry.entry_id = "entry-1"
        self.entry.data = {
            CONF_ENTRY_DEVICE_ID: "smartthings-device-id",
            CONF_ENTRY_DEVICE_NAME: "Soundbar Q800F",
            CONF_TOKEN: {"access_token": "secret"},
            "auth_implementation": "old-oauth",
            CONF_LOCATION_ID: "location",
            CONF_SUBSCRIPTION_ID: "subscription",
        }
        self.entry.options = {
            CONF_CONTROL_MODE: CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
            CONF_LOCAL_HOST: "192.0.2.26",
        }
        self.hass = MagicMock()
        self.hass.data = {}
        self.hass.config_entries.async_get_known_entry.return_value = self.entry
        self.flow = SamsungSoundbarOptionsFlowHandler()
        self.flow.hass = self.hass
        self.flow.handler = self.entry.entry_id
        self.flow.async_show_form = MagicMock(side_effect=lambda **kw: kw)
        self.flow.async_create_entry = MagicMock(side_effect=lambda **kw: kw)
        self.flow._async_validate_local_rpc = AsyncMock()
        self.identity_probe = patch.object(
            self.flow, "_async_read_identity", new_callable=AsyncMock
        )
        self.read_identity = self.identity_probe.start()
        self.addCleanup(self.identity_probe.stop)
        self.read_identity.return_value = IDENTITY

    async def test_hybrid_to_local_requires_confirmation_and_preserves_device_id(self) -> None:
        result = await self.flow.async_step_init({CONF_CONTROL_MODE: CONTROL_MODE_LOCAL_ONLY})
        self.assertEqual(result["step_id"], "confirm_local_identity")
        self.hass.config_entries.async_update_entry.assert_not_called()

        result = await self.flow.async_step_confirm_local_identity({})
        self.assertEqual(result["data"][CONF_LOCAL_IDENTITY], IDENTITY)
        self.assertEqual(self.read_identity.await_count, 2)
        new_data = self.hass.config_entries.async_update_entry.call_args.kwargs["data"]
        self.assertEqual(
            self.hass.config_entries.async_update_entry.call_args.kwargs["options"],
            result["data"],
        )
        self.assertEqual(new_data[CONF_ENTRY_DEVICE_ID], "smartthings-device-id")
        self.assertEqual(new_data[CONF_ENTRY_DEVICE_NAME], "Soundbar Q800F")
        for key in (CONF_TOKEN, "auth_implementation", CONF_LOCATION_ID, CONF_SUBSCRIPTION_ID):
            self.assertNotIn(key, new_data)

    async def test_identity_change_during_confirmation_is_rejected(self) -> None:
        self.read_identity.side_effect = [IDENTITY, {"wifi_mac": "94:e6:ba:89:bd:bb"}]
        await self.flow.async_step_init({CONF_CONTROL_MODE: CONTROL_MODE_LOCAL_ONLY})
        result = await self.flow.async_step_confirm_local_identity({})
        self.assertEqual(result["errors"], {"base": "identity_mismatch"})
        self.hass.config_entries.async_update_entry.assert_not_called()

    async def test_initial_local_entry_identity_protects_host_change(self) -> None:
        self.entry.data = {
            CONF_ENTRY_DEVICE_ID: "local:wifi_mac:94:e6:ba:89:bd:ba",
            CONF_ENTRY_DEVICE_NAME: "Soundbar Q800F",
            CONF_LOCAL_IDENTITY: IDENTITY,
            CONF_LOCAL_HOST: "192.0.2.26",
            CONF_CONTROL_MODE: CONTROL_MODE_LOCAL_ONLY,
        }
        self.entry.options = {}
        self.read_identity.return_value = {"wifi_mac": "94:e6:ba:89:bd:bb"}
        result = await self.flow.async_step_init({CONF_LOCAL_HOST: "192.0.2.27"})
        self.assertEqual(result["errors"], {"base": "identity_mismatch"})

        self.read_identity.return_value = {"wifi_mac": IDENTITY["wifi_mac"]}
        result = await self.flow.async_step_init({CONF_LOCAL_HOST: "192.0.2.27"})
        self.assertEqual(result["data"][CONF_LOCAL_IDENTITY]["wifi_mac"], IDENTITY["wifi_mac"])

    async def test_ip_change_requires_matching_saved_identity(self) -> None:
        self.entry.options = {
            CONF_CONTROL_MODE: CONTROL_MODE_LOCAL_ONLY,
            CONF_LOCAL_HOST: "192.0.2.26",
            CONF_LOCAL_IDENTITY: IDENTITY,
        }
        self.read_identity.return_value = {"wifi_mac": "94:e6:ba:89:bd:bb"}
        result = await self.flow.async_step_init({CONF_LOCAL_HOST: "192.0.2.27"})
        self.assertEqual(result["errors"], {"base": "identity_mismatch"})
        self.flow.async_create_entry.assert_not_called()
        self.hass.config_entries.async_update_entry.assert_not_called()

        self.read_identity.return_value = {"wifi_mac": IDENTITY["wifi_mac"]}
        result = await self.flow.async_step_init({CONF_LOCAL_HOST: "192.0.2.27"})
        self.assertEqual(result["data"][CONF_LOCAL_HOST], "192.0.2.27")
        self.assertEqual(result["data"][CONF_LOCAL_IDENTITY]["wifi_mac"], IDENTITY["wifi_mac"])

    async def test_identity_unavailable_keeps_oauth_data(self) -> None:
        self.read_identity.side_effect = LocalIdentityError("offline")
        result = await self.flow.async_step_init({CONF_CONTROL_MODE: CONTROL_MODE_LOCAL_ONLY})
        self.assertEqual(result["errors"], {"base": "identity_unavailable"})
        self.hass.config_entries.async_update_entry.assert_not_called()
        self.assertIn(CONF_TOKEN, self.entry.data)

    async def test_tokenless_local_entry_does_not_update_data_again(self) -> None:
        self.entry.data = {
            CONF_ENTRY_DEVICE_ID: "smartthings-device-id",
            CONF_ENTRY_DEVICE_NAME: "Soundbar Q800F",
        }
        self.entry.options = {
            CONF_CONTROL_MODE: CONTROL_MODE_LOCAL_ONLY,
            CONF_LOCAL_HOST: "192.0.2.26",
            CONF_LOCAL_IDENTITY: IDENTITY,
        }
        await self.flow.async_step_init({CONF_LOCAL_HOST: "192.0.2.27"})
        self.hass.config_entries.async_update_entry.assert_not_called()

    async def test_cloud_subscription_is_removed_before_credential_cleanup(self) -> None:
        subscription = MagicMock()
        runtime = MagicMock()
        runtime.subscriptions = {self.entry.entry_id: subscription}
        self.hass.data = {"samsung_soundbar": runtime}

        with patch(
            "custom_components.samsung_soundbar.config_flow.async_remove_subscription",
            new_callable=AsyncMock,
        ) as remove:
            await self.flow.async_step_init({CONF_CONTROL_MODE: CONTROL_MODE_LOCAL_ONLY})
            await self.flow.async_step_confirm_local_identity({})
        remove.assert_awaited_once_with(self.entry, subscription)

    async def test_legacy_hybrid_options_bind_identity_for_dhcp_without_losing_oauth(self) -> None:
        original_data = dict(self.entry.data)
        result = await self.flow.async_step_init({CONF_LOCAL_HOST: "192.0.2.26"})
        self.assertEqual(result["data"][CONF_LOCAL_IDENTITY], IDENTITY)
        self.assertEqual(
            result["data"][CONF_CONTROL_MODE], CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS
        )
        self.assertEqual(self.entry.data, original_data)
        self.hass.config_entries.async_update_entry.assert_not_called()

    async def test_hybrid_identity_mismatch_blocks_manual_host_change(self) -> None:
        self.entry.options[CONF_LOCAL_IDENTITY] = IDENTITY
        self.read_identity.return_value = {"wifi_mac": "94:e6:ba:89:bd:bb"}
        result = await self.flow.async_step_init({CONF_LOCAL_HOST: "192.0.2.27"})
        self.assertEqual(result["errors"], {"base": "identity_mismatch"})
        self.flow.async_create_entry.assert_not_called()

    async def test_hybrid_missing_endpoint_does_not_break_same_host_options(
        self,
    ) -> None:
        self.read_identity.side_effect = LocalIdentityError("metadata unavailable")
        for saved in (None, IDENTITY):
            with self.subTest(identity=saved):
                if saved:
                    self.entry.options[CONF_LOCAL_IDENTITY] = saved
                result = await self.flow.async_step_init(
                    {CONF_LOCAL_HOST: "192.0.2.26"}
                )
                if saved:
                    self.assertEqual(result["data"][CONF_LOCAL_IDENTITY], saved)
                else:
                    self.assertNotIn(CONF_LOCAL_IDENTITY, result["data"])
        self.assertIn(CONF_TOKEN, self.entry.data)

    async def test_hybrid_bound_host_change_requires_readable_identity(self) -> None:
        self.entry.options[CONF_LOCAL_IDENTITY] = IDENTITY
        self.read_identity.side_effect = LocalIdentityError("metadata unavailable")
        result = await self.flow.async_step_init({CONF_LOCAL_HOST: "192.0.2.27"})
        self.assertEqual(result["errors"], {"base": "identity_unavailable"})
        self.flow.async_create_entry.assert_not_called()

    async def test_partial_options_read_preserves_dhcp_mac_binding(self) -> None:
        self.entry.options[CONF_LOCAL_IDENTITY] = IDENTITY
        self.read_identity.return_value = {"tizen_duid": IDENTITY["tizen_duid"]}
        result = await self.flow.async_step_init({CONF_LOCAL_HOST: "192.0.2.27"})
        self.assertEqual(result["data"][CONF_LOCAL_IDENTITY], IDENTITY)

    async def test_cloud_options_keep_identity_for_later_hybrid_recovery(self) -> None:
        self.entry.options[CONF_LOCAL_IDENTITY] = IDENTITY
        result = await self.flow.async_step_init(
            {CONF_CONTROL_MODE: CONTROL_MODE_SMARTTHINGS_CLOUD}
        )
        self.assertEqual(result["data"][CONF_LOCAL_IDENTITY], IDENTITY)
        self.read_identity.assert_not_awaited()
