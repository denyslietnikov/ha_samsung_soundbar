"""Source and metadata contracts for the two local transport modes."""

import ast
import json
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "custom_components" / "samsung_soundbar"
REMOVED_SERVICES = {
    "set_speaker_level",
    "set_rear_speaker_mode",
    "set_active_voice_amplifier",
    "dump_execute_payload",
    "dump_status_summary",
}


class LocalPresentationContractTests(unittest.TestCase):
    def test_dependencies_are_local_and_pins_are_preserved(self):
        manifest = json.loads((INTEGRATION / "manifest.json").read_text())
        self.assertEqual(manifest["iot_class"], "local_polling")
        self.assertNotIn("application_credentials", manifest.get("dependencies", []))
        self.assertEqual(
            manifest["requirements"],
            [
                "smartthings-local==0.1.24",
                "cbor2>=6.2.0.post2",
            ],
        )
        self.assertNotIn("pysmartthings", (ROOT / "requirements-test.txt").read_text())

    def test_no_removed_services_or_eq_control(self):
        services = yaml.safe_load((INTEGRATION / "services.yaml").read_text())
        self.assertFalse(REMOVED_SERVICES.intersection(services))
        self.assertIn("dump_ocf_status", services)
        self.assertIn("dump_local_rpc", services)
        select = ast.parse((INTEGRATION / "select.py").read_text())
        self.assertNotIn(
            "EqPresetSelectEntity",
            {node.name for node in ast.walk(select) if isinstance(node, ast.ClassDef)},
        )

    def test_presentation_has_no_retired_transport_branches(self):
        for name in (
            "media_player.py",
            "select.py",
            "number.py",
            "switch.py",
            "sensor.py",
            "entity_updates.py",
            "services.py",
            "device_info.py",
            "repairs.py",
        ):
            with self.subTest(module=name):
                self.assertIsNone(
                    re.search(
                        r"cloud|hybrid_mode|smartthings_device|status_attribute_value",
                        (INTEGRATION / name).read_text(),
                        re.IGNORECASE,
                    )
                )

    def test_translations_only_offer_local_modes_and_preserve_native_groups(self):
        for language, prefixes in (
            ("en", ("Sound:", "Source:", "Tone:")),
            ("de", ("Klang:", "Quelle:", "Ton:")),
        ):
            data = json.loads(
                (INTEGRATION / "translations" / f"{language}.json").read_text()
            )
            with self.subTest(language=language):
                self.assertIsNone(
                    re.search(
                        r"cloud|oauth|application_credentials",
                        json.dumps(data),
                        re.IGNORECASE,
                    )
                )
                self.assertEqual(
                    data["selector"]["control_mode"]["options"],
                    {
                        "local_only": "Local only (RPC)",
                        "hybrid_local_ocf": "Hybrid Local + OCF",
                    },
                )
                self.assertNotIn("eq_preset", data["entity"]["select"])
                self.assertTrue(
                    data["entity"]["select"]["sound_mode_preset"]["name"].startswith(
                        prefixes[0]
                    )
                )
                self.assertTrue(
                    data["entity"]["select"]["input_preset"]["name"].startswith(
                        prefixes[1]
                    )
                )
                self.assertTrue(
                    data["entity"]["number"]["woofer_level"]["name"].startswith(
                        prefixes[2]
                    )
                )
                self.assertIn("ocf_missing_credential", data["issues"])
                self.assertIn("ocf_credential", data["issues"])

    def test_ci_does_not_name_removed_files(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/tests.yaml").read_text())
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                if "ruff check" not in step.get("run", ""):
                    continue
                for path in step["run"].split():
                    if path.endswith(".py"):
                        self.assertTrue((ROOT / path).is_file(), path)


if __name__ == "__main__":
    unittest.main()
