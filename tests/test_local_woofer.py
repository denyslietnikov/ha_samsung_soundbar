"""Relative Woofer actions never invent a level or use Cloud fallback."""

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock

from homeassistant.exceptions import HomeAssistantError

from custom_components.samsung_soundbar import button
from custom_components.samsung_soundbar.api_extension.SoundbarDevice import (
    SoundbarDevice,
)
from custom_components.samsung_soundbar.const import CONTROL_MODE_LOCAL_ONLY
from custom_components.samsung_soundbar.local_device import LocalDevice
from custom_components.samsung_soundbar.local_rpc import LocalRpcParseError


class TestLocalWoofer(IsolatedAsyncioTestCase):
    def setUp(self):
        self.rpc = MagicMock()
        self.rpc.remote_key = AsyncMock()
        self.cloud = LocalDevice("stable-id")
        self.cloud.status.ocf_model_number = "HW-Q800F"
        self.device = SoundbarDevice(
            self.cloud,
            MagicMock(),
            100,
            "Q800F",
            control_mode=CONTROL_MODE_LOCAL_ONLY,
            local_rpc=self.rpc,
        )
        self.device._SoundbarDevice__local_available = True

    async def test_buttons_have_stable_ids_and_send_one_relative_key_each(self):
        added = MagicMock()
        entry = MagicMock()
        entry.runtime_data = SimpleNamespace(device=self.device)
        await button.async_setup_entry(MagicMock(), entry, added)
        entities = added.call_args.args[0]
        self.assertEqual(
            [entity.unique_id for entity in entities],
            ["stable-id_btn_woofer_plus", "stable-id_btn_woofer_minus"],
        )
        await entities[0].async_press()
        await entities[1].async_press()
        self.assertEqual(self.rpc.remote_key.await_count, 2)
        self.assertEqual(
            [call.args[0] for call in self.rpc.remote_key.await_args_list],
            ["WOOFER_PLUS", "WOOFER_MINUS"],
        )

    async def test_relative_error_is_not_replayed_or_sent_to_cloud(self):
        self.rpc.remote_key.side_effect = LocalRpcParseError("Parse error", code=-32700)
        with self.assertRaises(HomeAssistantError):
            await self.device.adjust_local_woofer(True)
        self.rpc.remote_key.assert_awaited_once_with("WOOFER_PLUS")

    async def test_unverified_model_does_not_expose_woofer_buttons(self):
        self.cloud.status.ocf_model_number = "HW-UNKNOWN"
        added = MagicMock()
        entry = MagicMock()
        entry.runtime_data = SimpleNamespace(device=self.device)
        await button.async_setup_entry(MagicMock(), entry, added)
        self.assertEqual(added.call_args.args[0], [])

    async def test_model_discovered_after_setup_creates_buttons_once(self):
        self.cloud.status.ocf_model_number = None
        added = MagicMock()
        entry = MagicMock()
        entry.runtime_data = SimpleNamespace(device=self.device)
        await button.async_setup_entry(MagicMock(), entry, added)
        self.assertEqual(added.call_args.args[0], [])
        self.cloud.status.ocf_model_number = "HW-Q800F"
        self.device._SoundbarDevice__notify_update_listeners()
        self.assertEqual(len(added.call_args.args[0]), 2)
        self.device._SoundbarDevice__notify_update_listeners()
        self.assertEqual(added.call_count, 2)
