"""Q800F file-to-credential workflow; all device replies and secrets are synthetic."""

import base64
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

import cbor2

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/recover_q800f_credential.py"
TARGET = UUID("00000000-0000-4000-8000-000000000001")
CLIENT = UUID("00000000-0000-4000-8000-000000000002")
PSK = bytes(range(16))


class TestOcfCredentialRecovery(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "Q800F recovery launcher is not implemented")
        spec = importlib.util.spec_from_file_location("_ocf_recovery_test", SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.module
        with patch.object(sys, "path", [str(SCRIPT.parent), *sys.path]):
            spec.loader.exec_module(self.module)
        self.addCleanup(sys.modules.pop, spec.name, None)
        self.addCleanup(sys.modules.pop, "extract_ocf_credential", None)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = (
            Path(self.directory.name) / "f838d92eedf51cc435481f58169d8288cc505895"
        )
        self.original = cbor2.dumps(
            {
                "cred": cbor2.dumps(
                    {
                        "rowneruuid": str(CLIENT),
                        "creds": [
                            {
                                "credtype": 1,
                                "subjectuuid": str(TARGET),
                                "privatedata": {
                                    "encoding": "oic.sec.encoding.raw",
                                    "data": PSK,
                                },
                            }
                        ],
                    }
                )
            }
        )
        self.source.write_bytes(self.original)
        self.public = MagicMock(
            side_effect=lambda _host, href, **_kwargs: SimpleNamespace(
                successful=True,
                payload=cbor2.dumps(
                    {"mnmo": "HW-Q800F"} if href == "/oic/p" else {"di": str(TARGET)}
                ),
            )
        )
        self.discover = MagicMock(return_value=SimpleNamespace(ports=(46379,)))
        self.auth = MagicMock()
        self.session = MagicMock()
        self.session.get.return_value = (69, cbor2.dumps({"di": str(TARGET)}))
        self.factory = MagicMock(return_value=self.session)
        patcher = patch.object(
            self.module,
            "_load_protocol",
            return_value=(self.public, self.discover, self.auth, self.factory),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = self.module.main(
                ["--store", str(self.source), "--host", "192.0.2.26", *extra]
            )
        output = stdout.getvalue() + stderr.getvalue()
        for secret in (
            str(TARGET),
            str(CLIENT),
            PSK.hex(),
            base64.b64encode(PSK).decode(),
        ):
            self.assertNotIn(secret, output)
        self.assertNotIn("Traceback", output)
        self.assertEqual(self.source.read_bytes(), self.original)
        return result, output

    def test_hashed_file_needs_no_manually_supplied_uuid(self):
        result, output = self.run_main()
        self.assertEqual(result, 0)
        self.assertIn("authenticated", output)
        self.assertIn("no secret exported", output)
        self.auth.assert_called_once_with(identity=CLIENT.bytes, key=PSK)
        self.session.get.assert_called_once_with(["oic", "d"], timeout=6)
        self.session.post.assert_not_called()
        self.session.close.assert_called_once()
        self.assertEqual(list(Path(self.directory.name).iterdir()), [self.source])

    def test_explicit_output_reuses_private_portable_export(self):
        destination = Path(self.directory.name) / "q800f.ocf-credential.json"
        result, _ = self.run_main("--output", str(destination))
        self.assertEqual(result, 0)
        exported = json.loads(destination.read_bytes())
        self.assertEqual(exported["target_device_uuid"], str(TARGET))
        self.assertEqual(exported["client_identity_uuid"], str(CLIENT))
        self.assertEqual(exported["psk"], base64.b64encode(PSK).decode())
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_wrong_model_is_rejected_before_authentication(self):
        self.public.side_effect = None
        self.public.return_value = SimpleNamespace(
            successful=True, payload=cbor2.dumps({"mnmo": "other"})
        )
        self.assertEqual(self.run_main()[0], 1)
        self.factory.assert_not_called()

    def test_authenticated_identity_mismatch_does_not_export(self):
        self.session.get.return_value = (69, cbor2.dumps({"di": str(CLIENT)}))
        destination = Path(self.directory.name) / "credential.json"
        self.assertEqual(self.run_main("--output", str(destination))[0], 1)
        self.assertFalse(destination.exists())
        self.session.close.assert_called_once()

    def test_handshake_failure_closes_session_and_redacts_error(self):
        self.session.connect.side_effect = RuntimeError(base64.b64encode(PSK).decode())
        self.assertEqual(self.run_main()[0], 1)
        self.session.close.assert_called_once()

    def test_ambiguous_port_is_not_scanned(self):
        self.discover.return_value.ports = (12345, 46379)
        self.assertEqual(self.run_main()[0], 1)
        self.factory.assert_not_called()

    def test_public_read_failure_has_no_write_or_export(self):
        self.public.side_effect = None
        self.public.return_value = SimpleNamespace(successful=False, payload=b"")
        self.assertEqual(self.run_main()[0], 1)
        self.factory.assert_not_called()

    def test_missing_file_does_not_open_network(self):
        self.source.unlink()
        with contextlib.redirect_stderr(io.StringIO()):
            result = self.module.main(
                ["--store", str(self.source), "--host", "192.0.2.26"]
            )
        self.assertEqual(result, 1)
        self.public.assert_not_called()

    def test_invalid_host_is_rejected_without_network(self):
        with contextlib.redirect_stderr(io.StringIO()):
            result = self.module.main(
                ["--store", str(self.source), "--host", "https://example.com"]
            )
        self.assertEqual(result, 1)
        self.public.assert_not_called()


if __name__ == "__main__":
    unittest.main()
