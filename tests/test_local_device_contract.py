"""Local device source has no executable remote API or fallback path."""

import ast
import os
import subprocess
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, MagicMock


class TestLocalDeviceContract(TestCase):
    def test_import_works_with_retired_sdk_blocked(self):
        script = """
import importlib.abc
import sys
class NoRemoteSdk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'pysmartthings' or fullname.startswith('pysmartthings.'):
            raise AssertionError('Retired SDK imported')
sys.meta_path.insert(0, NoRemoteSdk())
import custom_components.samsung_soundbar
import custom_components.samsung_soundbar.config_flow
assert not any(name == 'pysmartthings' or name.startswith('pysmartthings.') for name in sys.modules)
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_development_setup_uses_canonical_local_requirements(self):
        setup = Path("scripts/setup").read_text()
        self.assertIn("-r requirements-test.txt", setup)
        self.assertNotIn("pysmartthings", setup)
        self.assertFalse(Path("Pipfile").exists())
        self.assertFalse(Path("Pipfile.lock").exists())

    def test_runtime_contains_no_retired_client_or_remote_api(self):
        root = Path("custom_components/samsung_soundbar")
        for path in root.rglob("*.py"):
            source = path.read_text()
            with self.subTest(path=path):
                self.assertNotIn("api.smartthings.com", source)
                self.assertNotIn("config_entry_oauth2_flow", source)
                self.assertNotIn("pysmartthings", source)
                if path.name != "entry_migration.py":
                    self.assertNotIn("cloud", source.lower())
        for removed in (
            "auth.py",
            "subscription.py",
            "application_credentials.py",
            "cloud_errors.py",
            "cloud_repairs.py",
            "api_extension/smartthings_compat.py",
        ):
            self.assertFalse((root / removed).exists())

    def test_device_has_only_local_dependencies_and_commands(self):
        source = Path(
            "custom_components/samsung_soundbar/api_extension/SoundbarDevice.py"
        ).read_text()
        tree = ast.parse(source)
        imports = [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ]
        self.assertFalse(
            any("pysmartthings" in name or "cloud" in name for name in imports)
        )
        device = next(node for node in tree.body if isinstance(node, ast.ClassDef))
        names = {
            node.name
            for node in device.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertTrue(
            {"local_rpc_enabled", "ocf_enabled", "set_volume", "set_woofer"} <= names
        )
        self.assertFalse(
            {
                "update_cloud_status",
                "media_play",
                "set_custom_execution_data",
                "get_device_status_raw",
            }
            & names
        )


class TestLocalCommands(IsolatedAsyncioTestCase):
    def setUp(self):
        from custom_components.samsung_soundbar.api_extension.SoundbarDevice import (
            SoundbarDevice,
        )
        from custom_components.samsung_soundbar.const import (
            CONTROL_MODE_HYBRID_LOCAL_OCF,
        )
        from custom_components.samsung_soundbar.local_device import LocalDevice

        self.rpc = MagicMock()
        self.rpc.status = AsyncMock(return_value={"power": "powerOn", "volume": 8})
        self.rpc.set_volume = AsyncMock()
        self.device = SoundbarDevice(
            LocalDevice("preserved-id"),
            MagicMock(),
            20,
            "Q800F",
            control_mode=CONTROL_MODE_HYBRID_LOCAL_OCF,
            local_rpc=self.rpc,
        )

    def test_unrecognized_control_mode_is_not_silently_accepted(self):
        from custom_components.samsung_soundbar.api_extension.SoundbarDevice import (
            SoundbarDevice,
        )
        from custom_components.samsung_soundbar.local_device import LocalDevice

        with self.assertRaises(ValueError):
            SoundbarDevice(
                LocalDevice("same-id"),
                MagicMock(),
                100,
                "Q800F",
                control_mode="unsupported",
                local_rpc=self.rpc,
            )

    async def test_volume_uses_real_scale_and_obeys_configured_limit(self):
        await self.device.set_volume(0.08)
        self.rpc.set_volume.assert_awaited_once_with(8)
        await self.device.set_volume(0.5)
        self.rpc.set_volume.assert_awaited_with(20)
        self.assertEqual(self.device.volume_level, 0.08)

    async def test_bad_volume_is_rejected_before_transport(self):
        from homeassistant.exceptions import ServiceValidationError

        for value in (None, True, "8", -0.1, 1.1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ServiceValidationError):
                await self.device.set_volume(value)
        self.rpc.set_volume.assert_not_awaited()

    async def test_rpc_failure_has_no_alternate_transport(self):
        from homeassistant.exceptions import HomeAssistantError

        from custom_components.samsung_soundbar.local_rpc import LocalRpcError

        self.rpc.set_volume.side_effect = LocalRpcError("offline")
        with self.assertRaises(HomeAssistantError):
            await self.device.set_volume(0.08)
        self.rpc.set_volume.assert_awaited_once()
        self.assertFalse(self.device.local_available)

    async def test_transaction_validates_volume_before_building_expectation(self):
        from homeassistant.exceptions import ServiceValidationError

        coordinator = MagicMock()
        coordinator.in_command = False
        coordinator.async_write = AsyncMock()
        self.device.coordinator = coordinator
        with self.assertRaises(ServiceValidationError):
            await self.device.set_volume(None)
        coordinator.async_write.assert_not_awaited()
        await self.device.set_volume(0.5)
        self.assertEqual(
            coordinator.async_write.call_args.args[1], {"volume_level": 0.2}
        )

    async def test_unproven_ocf_setting_is_rejected_without_rpc_or_wake(self):
        from homeassistant.exceptions import HomeAssistantError

        with self.assertRaises(HomeAssistantError):
            await self.device.set_woofer(0)
        with self.assertRaises(HomeAssistantError):
            await self.device.set_bass_mode(True)
        self.rpc.status.assert_not_awaited()
        self.rpc.power_on.assert_not_called()
