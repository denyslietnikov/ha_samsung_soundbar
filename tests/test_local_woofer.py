"""Internal relative RPC safety remains separate from the removed button UI."""

from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock

from homeassistant.exceptions import HomeAssistantError

from custom_components.samsung_soundbar.api_extension.SoundbarDevice import (
    SoundbarDevice,
)
from custom_components.samsung_soundbar.const import CONTROL_MODE_LOCAL_ONLY
from custom_components.samsung_soundbar.local_device import LocalDevice
from custom_components.samsung_soundbar.local_rpc import LocalRpcParseError


class TestLocalWoofer(IsolatedAsyncioTestCase):
    def test_button_platform_is_not_loaded(self):
        from custom_components.samsung_soundbar import PLATFORMS

        self.assertNotIn("button", PLATFORMS)

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

    async def test_relative_error_is_not_replayed_or_sent_to_cloud(self):
        self.rpc.remote_key.side_effect = LocalRpcParseError("Parse error", code=-32700)
        with self.assertRaises(HomeAssistantError):
            await self.device.adjust_local_woofer(True)
        self.rpc.remote_key.assert_awaited_once_with("WOOFER_PLUS")
