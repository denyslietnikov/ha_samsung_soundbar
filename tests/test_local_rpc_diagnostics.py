"""Direct-control probes are explicit, validated, and verified by getters."""

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import MagicMock, patch

from homeassistant.exceptions import HomeAssistantError
from test_local_rpc import SoundbarFixture

from custom_components.samsung_soundbar import (
    DUMP_LOCAL_RPC_SCHEMA,
    _async_create_dump_local_rpc_service,
)


class TestDirectControlDiagnostics(IsolatedAsyncioTestCase):
    def setUp(self):
        self.soundbar = SoundbarFixture()
        self.response = {"result": {"success": True}}
        original = self.soundbar.respond

        async def respond(payload):
            method = payload["method"]
            if method in ("volumeControl", "muteControl"):
                if "result" in self.response and self.response["result"]["success"]:
                    if method == "volumeControl":
                        self.soundbar.volume = payload["params"]["volume"]
                    else:
                        self.soundbar.mute = payload["params"]["mute"]
                return self.response
            return await original(payload)

        self.soundbar.handler = respond

    async def probe(self, method, params, methods=None):
        data = DUMP_LOCAL_RPC_SCHEMA(
            {
                "host": "192.0.2.26",
                "methods": methods or ["powerControl"],
                "write_method": method,
                "write_params": params,
            }
        )
        with patch(
            "custom_components.samsung_soundbar.async_get_clientsession",
            return_value=self.soundbar.session,
        ):
            return await _async_create_dump_local_rpc_service(MagicMock())(
                SimpleNamespace(data=data)
            )

    async def test_volume_probe_includes_getter_and_verifies_real_change(self):
        result = await self.probe("volumeControl", {"volume": 11})
        self.assertIn("getVolume", result["methods"])
        self.assertEqual(
            result["write_verification"],
            {
                "field": "volume",
                "requested": 11,
                "before": 8,
                "after": 11,
                "readback_matches": True,
                "state_change_observed": True,
                "runtime_enabled": False,
            },
        )
        self.assertEqual(self.soundbar.methods.count("volumeControl"), 1)

    async def test_mute_probe_verifies_boolean_getter(self):
        result = await self.probe("muteControl", {"mute": True})
        self.assertTrue(result["write_verification"]["readback_matches"])
        self.assertTrue(result["write_verification"]["state_change_observed"])
        self.assertIn("getMute", result["methods"])

    async def test_same_value_readback_does_not_claim_observed_change(self):
        result = await self.probe("volumeControl", {"volume": 8})
        self.assertTrue(result["write_verification"]["readback_matches"])
        self.assertFalse(result["write_verification"]["state_change_observed"])

    async def test_parse_error_reports_code_without_retry_or_runtime_enable(self):
        self.response = {"code": -32700, "message": "Parse error"}
        result = await self.probe("volumeControl", {"volume": 11})
        self.assertEqual(result["write_error_code"], -32700)
        self.assertFalse(result["write_verification"]["readback_matches"])
        self.assertEqual(result["write_verification"]["after"], 8)
        self.assertEqual(self.soundbar.methods.count("volumeControl"), 1)
        self.assertEqual(self.soundbar.methods.count("createAccessToken"), 1)

    async def test_invalid_probe_parameters_fail_before_any_network_calls(self):
        for method, params in (
            ("volumeControl", {"volume": "8"}),
            ("volumeControl", {"volume": True}),
            ("volumeControl", {"volume": 101}),
            ("muteControl", {"mute": "true"}),
            ("muteControl", {"mute": 1}),
        ):
            with (
                self.subTest(method=method, params=params),
                self.assertRaises(HomeAssistantError),
            ):
                await self.probe(method, params)
        self.assertEqual(self.soundbar.requests, [])

    async def test_completed_without_changed_readback_is_not_verified(self):
        async def respond(payload):
            if payload["method"] == "volumeControl":
                return {"result": {"success": True}}
            return await self.soundbar.respond(payload)

        self.soundbar.handler = respond
        result = await self.probe("volumeControl", {"volume": 11})
        self.assertIsNone(result["write_error"])
        self.assertFalse(result["write_verification"]["readback_matches"])
