"""DHCP recovery preserves identity, options and existing registry identifiers."""

import asyncio
import json
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from custom_components.samsung_soundbar.config_flow import SamsungSoundbarConfigFlow
from custom_components.samsung_soundbar.const import (
    CONF_CONTROL_MODE,
    CONF_ENTRY_DEVICE_ID,
    CONF_LOCAL_HOST,
    CONF_LOCAL_IDENTITY,
    CONF_LOCAL_PORT,
    CONF_LOCAL_TIMEOUT,
    CONF_LOCAL_VERIFY_SSL,
    CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
    CONTROL_MODE_LOCAL_ONLY,
    CONTROL_MODE_SMARTTHINGS_CLOUD,
)
from custom_components.samsung_soundbar.local_identity import LocalIdentityError

IDENTITY = {
    "wifi_mac": "94:e6:ba:89:bd:ba",
    "tizen_duid": "uuid:f289154e-3c4a-41d8-ba8f-86850cf9be07",
    "upnp_udn": "uuid:ffa77b59-a763-4caa-ba60-028de1be4bdf",
}
OLD_HOST = "192.0.2.26"
NEW_HOST = "192.0.2.42"


class TestDhcpRecovery(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.flow = SamsungSoundbarConfigFlow()
        self.flow.context = {"source": "dhcp"}
        self.flow.hass = MagicMock()
        self.flow.hass.data = {}
        self.flow.async_abort = MagicMock(side_effect=lambda **kw: kw)
        self.flow.async_create_entry = MagicMock()
        self.entry = MagicMock()
        self.entry.entry_id = "existing-entry-id"
        self.entry.unique_id = "existing-cloud-id"
        self.entry.disabled_by = None
        self.entry.data = {
            CONF_ENTRY_DEVICE_ID: "existing-cloud-id",
            CONF_CONTROL_MODE: CONTROL_MODE_HYBRID_LOCAL_SMARTTHINGS,
            CONF_LOCAL_HOST: OLD_HOST,
            CONF_LOCAL_IDENTITY: dict(IDENTITY),
            "token": {"access_token": "test-oauth-token"},
            "location_id": "existing-location-id",
        }
        self.entry.options = {
            CONF_LOCAL_HOST: OLD_HOST,
            CONF_LOCAL_PORT: 1517,
            CONF_LOCAL_TIMEOUT: 3,
            CONF_LOCAL_VERIFY_SSL: True,
            "local_fallback_to_cloud": False,
            "device_volume": 50,
        }
        self.entry.update_listeners = [AsyncMock()]
        entries = self.flow.hass.config_entries
        entries.async_entries.return_value = [self.entry]
        entries.async_get_entry.return_value = self.entry
        entries.async_update_entry.side_effect = self.update_entry
        self.identity_patch = patch(
            "custom_components.samsung_soundbar.dhcp_recovery.async_read_local_identity",
            new_callable=AsyncMock,
        )
        self.read_identity = self.identity_patch.start()
        self.addCleanup(self.identity_patch.stop)
        self.read_identity.return_value = dict(IDENTITY)
        self.session_patch = patch(
            "custom_components.samsung_soundbar.dhcp_recovery.async_get_clientsession",
            return_value=MagicMock(),
        )
        self.get_session = self.session_patch.start()
        self.addCleanup(self.session_patch.stop)
        self.oauth_patch = patch(
            "homeassistant.helpers.config_entry_oauth2_flow.AbstractOAuth2FlowHandler.async_step_user",
            new_callable=AsyncMock,
        )
        self.oauth = self.oauth_patch.start()
        self.addCleanup(self.oauth_patch.stop)
        self.rpc_patch = patch(
            "custom_components.samsung_soundbar.config_flow.LocalSoundbarRpcClient"
        )
        self.rpc = self.rpc_patch.start()
        self.addCleanup(self.rpc_patch.stop)

    @staticmethod
    def discovery(host=NEW_HOST, mac="94E6BA89BDBA") -> DhcpServiceInfo:
        return DhcpServiceInfo(ip=host, hostname="arbitrary-name", macaddress=mac)

    @staticmethod
    def update_entry(entry, *, data, options) -> bool:
        entry.data, entry.options = data, options
        return True

    def assert_unchanged(self) -> None:
        self.flow.hass.config_entries.async_update_entry.assert_not_called()
        self.flow.hass.config_entries.async_schedule_reload.assert_not_called()
        self.flow.async_create_entry.assert_not_called()

    async def test_existing_hybrid_host_changes_without_losing_ids_or_auth(
        self,
    ) -> None:
        original_data = dict(self.entry.data)
        original_options = dict(self.entry.options)
        result = await self.flow.async_step_dhcp(self.discovery())
        self.assertEqual(result["reason"], "dhcp_host_updated")
        self.assertEqual(self.entry.entry_id, "existing-entry-id")
        self.assertEqual(self.entry.unique_id, "existing-cloud-id")
        self.assertEqual(self.entry.data, {**original_data, CONF_LOCAL_HOST: NEW_HOST})
        self.assertEqual(
            self.entry.options, {**original_options, CONF_LOCAL_HOST: NEW_HOST}
        )
        self.read_identity.assert_awaited_once_with(
            self.get_session.return_value, NEW_HOST, 3
        )
        self.flow.hass.config_entries.async_schedule_reload.assert_not_called()
        self.flow.async_create_entry.assert_not_called()
        self.oauth.assert_not_awaited()
        self.rpc.assert_not_called()

    async def test_local_only_data_settings_and_identity_in_options_are_preserved(
        self,
    ) -> None:
        self.entry.data.pop("token")
        self.entry.data.pop(CONF_LOCAL_HOST)
        self.entry.options[CONF_CONTROL_MODE] = CONTROL_MODE_LOCAL_ONLY
        self.entry.options[CONF_LOCAL_IDENTITY] = dict(IDENTITY)
        self.entry.data[CONF_LOCAL_IDENTITY] = {"wifi_mac": "00:11:22:33:44:55"}
        result = await self.flow.async_step_dhcp(self.discovery())
        self.assertEqual(result["reason"], "dhcp_host_updated")
        self.assertEqual(self.entry.options[CONF_LOCAL_IDENTITY], IDENTITY)
        self.assertEqual(
            self.entry.data[CONF_LOCAL_IDENTITY], {"wifi_mac": "00:11:22:33:44:55"}
        )
        self.assertNotIn("token", self.entry.data)
        self.oauth.assert_not_awaited()
        self.rpc.assert_not_called()

    async def test_failed_setup_entry_schedules_one_reload(self) -> None:
        self.entry.update_listeners = []
        await self.flow.async_step_dhcp(self.discovery())
        await self.flow.async_step_dhcp(self.discovery())
        self.flow.hass.config_entries.async_schedule_reload.assert_called_once_with(
            "existing-entry-id"
        )
        self.read_identity.assert_awaited_once()

    async def test_same_host_and_repeated_events_do_not_reload_or_probe(self) -> None:
        result = await self.flow.async_step_dhcp(self.discovery(OLD_HOST))
        self.assertEqual(result["reason"], "already_configured")
        self.read_identity.assert_not_awaited()
        self.assert_unchanged()
        await self.flow.async_step_dhcp(self.discovery())
        self.flow.hass.config_entries.async_update_entry.reset_mock()
        result = await self.flow.async_step_dhcp(self.discovery())
        self.assertEqual(result["reason"], "already_configured")
        self.read_identity.assert_awaited_once()
        self.assert_unchanged()

    async def test_unrelated_dhcp_mac_never_probes_the_network(self) -> None:
        for mac in ("001122334455", "invalid", "000000000000"):
            with self.subTest(mac=mac):
                result = await self.flow.async_step_dhcp(self.discovery(mac=mac))
                self.assertEqual(result["reason"], "dhcp_not_configured")
        self.read_identity.assert_not_awaited()
        self.get_session.assert_not_called()
        self.assert_unchanged()

    async def test_cloud_disabled_or_unbound_entries_are_ignored(self) -> None:
        scenarios = [
            (CONTROL_MODE_SMARTTHINGS_CLOUD, None, IDENTITY),
            (CONTROL_MODE_LOCAL_ONLY, "user", IDENTITY),
            (CONTROL_MODE_LOCAL_ONLY, None, {}),
            (CONTROL_MODE_LOCAL_ONLY, None, {"tizen_duid": IDENTITY["tizen_duid"]}),
        ]
        for mode, disabled_by, identity in scenarios:
            with self.subTest(mode=mode, disabled=disabled_by, identity=identity):
                self.entry.options[CONF_CONTROL_MODE] = mode
                self.entry.disabled_by = disabled_by
                self.entry.data[CONF_LOCAL_IDENTITY] = identity
                result = await self.flow.async_step_dhcp(self.discovery())
                self.assertEqual(result["reason"], "dhcp_not_configured")
        self.read_identity.assert_not_awaited()
        self.assert_unchanged()

    async def test_invalid_discovery_addresses_are_ignored(self) -> None:
        for host in ("not-a-host", "0.0.0.0", "127.0.0.1", "224.0.0.1", "::1"):
            with self.subTest(host=host):
                await self.flow.async_step_dhcp(self.discovery(host))
        self.read_identity.assert_not_awaited()
        self.assert_unchanged()

    async def test_endpoint_mac_uuid_conflicts_and_model_only_are_rejected(
        self,
    ) -> None:
        for observed in (
            {**IDENTITY, "wifi_mac": "94:e6:ba:89:bd:bb"},
            {**IDENTITY, "tizen_duid": "uuid:11111111-2222-3333-4444-555555555555"},
            {"identifier": "22_AV_HW-Q800F"},
        ):
            with self.subTest(observed=observed):
                self.read_identity.return_value = observed
                result = await self.flow.async_step_dhcp(self.discovery())
                self.assertEqual(result["reason"], "dhcp_identity_mismatch")
        self.assert_unchanged()

    async def test_partial_endpoint_read_does_not_erase_saved_identifiers(self) -> None:
        for key in ("wifi_mac", "tizen_duid", "upnp_udn"):
            with self.subTest(endpoint=key):
                self.entry.data[CONF_LOCAL_HOST] = OLD_HOST
                self.entry.options[CONF_LOCAL_HOST] = OLD_HOST
                self.read_identity.return_value = {key: IDENTITY[key]}
                result = await self.flow.async_step_dhcp(self.discovery())
                self.assertEqual(result["reason"], "dhcp_host_updated")
                self.assertEqual(self.entry.data[CONF_LOCAL_IDENTITY], IDENTITY)

    async def test_probe_failure_preserves_old_host_and_later_event_recovers(
        self,
    ) -> None:
        self.read_identity.side_effect = LocalIdentityError("endpoints unavailable")
        result = await self.flow.async_step_dhcp(self.discovery())
        self.assertEqual(result["reason"], "dhcp_identity_unavailable")
        self.assert_unchanged()
        self.read_identity.side_effect = None
        result = await self.flow.async_step_dhcp(self.discovery())
        self.assertEqual(result["reason"], "dhcp_host_updated")

    async def test_configuration_changed_while_probing_is_not_overwritten(self) -> None:
        for key, value in (
            (CONF_LOCAL_HOST, "192.0.2.99"),
            (CONF_CONTROL_MODE, CONTROL_MODE_SMARTTHINGS_CLOUD),
            (CONF_LOCAL_IDENTITY, {"wifi_mac": "00:11:22:33:44:55"}),
        ):
            with self.subTest(key=key):
                self.entry.options.pop(CONF_CONTROL_MODE, None)
                self.entry.options.pop(CONF_LOCAL_IDENTITY, None)
                self.entry.options[CONF_LOCAL_HOST] = OLD_HOST

                async def probe(*_, key=key, value=value):
                    self.entry.options[key] = value
                    return IDENTITY

                self.read_identity.side_effect = probe
                result = await self.flow.async_step_dhcp(self.discovery())
                self.assertEqual(result["reason"], "dhcp_configuration_changed")
                self.assertEqual(self.entry.options[key], value)
                self.assert_unchanged()

    async def test_removed_or_disabled_entry_is_not_updated_after_probe(self) -> None:
        for removed in (True, False):
            with self.subTest(removed=removed):
                self.flow.hass.config_entries.async_get_entry.return_value = self.entry
                self.entry.disabled_by = None

                async def probe(*_, removed=removed):
                    if removed:
                        self.flow.hass.config_entries.async_get_entry.return_value = (
                            None
                        )
                    else:
                        self.entry.disabled_by = "user"
                    return IDENTITY

                self.read_identity.side_effect = probe
                result = await self.flow.async_step_dhcp(self.discovery())
                self.assertEqual(result["reason"], "dhcp_configuration_changed")
                self.assert_unchanged()

    async def test_simultaneous_duplicate_events_share_probe_and_update(self) -> None:
        started, release = asyncio.Event(), asyncio.Event()

        async def probe(*_):
            started.set()
            await release.wait()
            return IDENTITY

        self.read_identity.side_effect = probe
        first = asyncio.create_task(self.flow.async_step_dhcp(self.discovery()))
        await started.wait()
        other = SamsungSoundbarConfigFlow()
        other.hass = self.flow.hass
        other.context = {"source": "dhcp"}
        second = asyncio.create_task(other.async_step_dhcp(self.discovery()))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second)
        self.assertEqual(results[0]["reason"], "dhcp_host_updated")
        self.assertEqual(results[1]["reason"], "already_configured")
        self.read_identity.assert_awaited_once()
        self.flow.hass.config_entries.async_update_entry.assert_called_once()

    async def test_cancelled_probe_releases_recovery_lock(self) -> None:
        started = asyncio.Event()

        async def probe(*_):
            started.set()
            await asyncio.Event().wait()

        self.read_identity.side_effect = probe
        task = asyncio.create_task(self.flow.async_step_dhcp(self.discovery()))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_unchanged()
        self.read_identity.side_effect = None
        result = await self.flow.async_step_dhcp(self.discovery())
        self.assertEqual(result["reason"], "dhcp_host_updated")

    def test_q800f_manifest_matcher_uses_supported_oui_index(self) -> None:
        manifest = json.loads(
            (
                Path(__file__).parents[1]
                / "custom_components/samsung_soundbar/manifest.json"
            ).read_text()
        )
        self.assertIn({"macaddress": "94E6BA*"}, manifest["dhcp"])
        self.assertTrue(
            all(
                len(matcher["macaddress"].split("*")[0]) >= 6
                for matcher in manifest["dhcp"]
                if "macaddress" in matcher
            )
        )
