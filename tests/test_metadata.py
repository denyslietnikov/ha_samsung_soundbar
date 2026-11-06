"""Regression checks for the integration's HACS and hassfest metadata."""

import json
import re
import unittest
from pathlib import Path

import yaml
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "custom_components" / "samsung_soundbar"


class MetadataTests(unittest.TestCase):
    def test_manifest_order_and_shared_dependency(self):
        manifest = json.loads((INTEGRATION / "manifest.json").read_text())
        self.assertEqual(list(manifest)[:2], ["domain", "name"])
        self.assertEqual(list(manifest)[2:], sorted(list(manifest)[2:]))
        requirement = Requirement(manifest["requirements"][0])
        self.assertEqual(requirement.name, "pysmartthings")
        self.assertEqual(str(requirement.specifier), ">=4.0.3")
        self.assertIn("media_entertainment.speakers_audio", manifest["categories"])

    def test_entity_services_target_media_players(self):
        services = yaml.safe_load((INTEGRATION / "services.yaml").read_text())
        for name, description in services.items():
            if "target" in description:
                with self.subTest(service=name):
                    self.assertNotIn("device", description["target"])
                    self.assertEqual(
                        description["target"]["entity"],
                        {"integration": "samsung_soundbar", "domain": "media_player"},
                    )

    def test_selector_labels_preserve_legacy_service_values(self):
        services = yaml.safe_load((INTEGRATION / "services.yaml").read_text())
        cases = (
            (
                "select_soundmode",
                "sound_mode",
                ["standard", "surround", "game", "adaptive sound"],
            ),
            (
                "set_speaker_level",
                "speaker_identifier",
                [
                    "Spk_Center",
                    "Spk_Side",
                    "Spk_Wide",
                    "Spk_Front_Top",
                    "Spk_Rear",
                    "Spk_Rear_Top",
                ],
            ),
            ("set_rear_speaker_mode", "speaker_mode", ["Rear", "Front"]),
        )
        for service, field, expected in cases:
            with self.subTest(service=service):
                selector = services[service]["fields"][field]["selector"]["select"]
                self.assertNotIn("translation_key", selector)
                self.assertEqual(
                    [option["value"] for option in selector["options"]], expected
                )
                self.assertTrue(all(option["label"] for option in selector["options"]))

    def test_translations_use_valid_keys_and_fixable_issue_schema(self):
        pattern = re.compile(r"^(?!.+[_-]{2})(?![_-])[a-z0-9_-]+(?<![_-])$")
        for path in (INTEGRATION / "translations").glob("*.json"):
            translated = json.loads(path.read_text())
            for selector, description in translated.get("selector", {}).items():
                for option in description.get("options", {}):
                    with self.subTest(
                        language=path.stem, selector=selector, option=option
                    ):
                        self.assertRegex(option, pattern)
            for issue, description in translated.get("issues", {}).items():
                with self.subTest(language=path.stem, issue=issue):
                    self.assertNotIn("description", description)
                    self.assertTrue(
                        description["fix_flow"]["step"]["choose"]["description"]
                    )

    def test_workflows_use_node24_actions(self):
        minimum_major = {
            "actions/checkout": 6,
            "actions/setup-python": 6,
            "softprops/action-gh-release": 3,
        }
        for path in (ROOT / ".github" / "workflows").glob("*.yaml"):
            workflow = yaml.safe_load(path.read_text())
            for job in workflow["jobs"].values():
                with self.subTest(workflow=path.name):
                    self.assertEqual(job["runs-on"], "ubuntu-26.04")
                for step in job.get("steps", []):
                    action, _, version = step.get("uses", "").partition("@")
                    if action in minimum_major:
                        with self.subTest(workflow=path.name, action=action):
                            self.assertGreaterEqual(
                                int(version.removeprefix("v").split(".")[0]),
                                minimum_major[action],
                            )
