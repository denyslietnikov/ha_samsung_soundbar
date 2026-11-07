"""Offline OCF credential extraction tests using synthetic secrets only."""

import base64
import contextlib
import importlib.util
import io
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import cbor2

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/extract_ocf_credential.py"
TARGET = UUID("00000000-0000-4000-8000-000000000001")
CLIENT = UUID("00000000-0000-4000-8000-000000000002")
OTHER = UUID("00000000-0000-4000-8000-000000000003")
PSK = bytes(range(16))
OTHER_PSK = b"other-device-key"


def record(*, target=TARGET, key=PSK, encoding="oic.sec.encoding.raw", kind=1):
    return {
        "credtype": kind,
        "subjectuuid": str(target),
        "privatedata": {"encoding": encoding, "data": key},
    }


def store(records=None, *, owner=CLIENT, nested=True):
    credentials = {
        "rowneruuid": owner if isinstance(owner, bytes) else str(owner),
        "creds": [record()] if records is None else records,
    }
    return cbor2.dumps({"cred": cbor2.dumps(credentials) if nested else credentials})


class TestOcfCredentialImport(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "Offline extractor is not implemented")
        spec = importlib.util.spec_from_file_location("_ocf_extract_test", SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.module
        spec.loader.exec_module(self.module)
        self.addCleanup(sys.modules.pop, spec.name, None)

    def extract(self, payload=None):
        return self.module.extract_credential(
            store() if payload is None else payload, TARGET
        )

    def assert_private_error(self, operation):
        with self.assertRaises(self.module.CredentialError) as error:
            operation()
        text = str(error.exception) + repr(error.exception)
        for secret in (
            str(TARGET),
            str(CLIENT),
            PSK.hex(),
            base64.b64encode(PSK).decode(),
        ):
            self.assertNotIn(secret, text)

    def test_nested_cbor_selects_only_target_psk(self):
        credential = self.extract(
            store([record(target=OTHER, key=OTHER_PSK), record()])
        )
        self.assertEqual(credential.target_device_uuid, TARGET)
        self.assertEqual(credential.client_identity_uuid, CLIENT)
        self.assertEqual(credential.psk, PSK)

    def test_decoded_cred_map_and_32_byte_key(self):
        key = bytes(range(32))
        self.assertEqual(self.extract(store([record(key=key)], nested=False)).psk, key)

    def test_base64_text_and_bytes(self):
        for data in (base64.b64encode(PSK), base64.b64encode(PSK).decode()):
            with self.subTest(kind=type(data).__name__):
                credential = self.extract(
                    store([record(key=data, encoding="oic.sec.encoding.base64")])
                )
                self.assertEqual(credential.psk, PSK)

    def test_raw_uuid_and_nul_bytes_are_preserved(self):
        item = record()
        item["subjectuuid"] = TARGET.bytes
        credential = self.extract(store([item], owner=CLIENT.bytes))
        self.assertEqual(credential.client_identity_uuid.bytes, CLIENT.bytes)
        self.assertIn(b"\x00", credential.client_identity_uuid.bytes)

    def test_certificate_and_unrelated_private_data_are_not_selected(self):
        other = record(target=OTHER)
        other["privatedata"] = None
        credential = self.extract(store([record(kind=8), other, record()]))
        self.assertEqual(credential.psk, PSK)

    def test_missing_or_multiple_target_records_are_rejected(self):
        for records in ([], [record(target=OTHER)], [record(), record()]):
            with self.subTest(count=len(records)):
                self.assert_private_error(
                    lambda records=records: self.extract(store(records))
                )

    def test_boolean_or_float_credential_type_is_not_psk(self):
        for value in (True, 1.0, "1"):
            with self.subTest(value=value):
                self.assert_private_error(
                    lambda value=value: self.extract(store([record(kind=value)]))
                )

    def test_invalid_credential_structure(self):
        for value in (
            None,
            [],
            {"cred": None},
            {"cred": {"creds": {}}},
            {"cred": {"rowneruuid": str(CLIENT), "creds": [None]}},
        ):
            with self.subTest(kind=type(value).__name__):
                self.assert_private_error(
                    lambda value=value: self.extract(cbor2.dumps(value))
                )

    def test_malformed_truncated_or_trailing_cbor(self):
        for data in (b"", b"\xff", store()[:-1], store() + cbor2.dumps("extra")):
            with self.subTest(length=len(data)):
                self.assert_private_error(lambda data=data: self.extract(data))

    def test_duplicate_cbor_keys_are_rejected(self):
        key = cbor2.dumps("cred")
        value = cbor2.dumps({"rowneruuid": str(CLIENT), "creds": [record()]})
        self.assert_private_error(
            lambda: self.extract(b"\xa2" + key + value + key + value)
        )

    def test_nested_trailing_cbor_and_excessive_depth_are_rejected(self):
        inner = cbor2.dumps({"rowneruuid": str(CLIENT), "creds": [record()]})
        self.assert_private_error(
            lambda: self.extract(cbor2.dumps({"cred": inner + b"\x00"}))
        )
        self.assert_private_error(lambda: self.extract(b"\x81" * 100 + b"\x00"))

    def test_invalid_client_uuid(self):
        for value in (None, "not-an-identity", str(UUID(int=0)), b"short"):
            with self.subTest(kind=type(value).__name__):
                self.assert_private_error(
                    lambda value=value: self.extract(store(owner=value))
                )

    def test_invalid_target_uuid(self):
        for value in (None, "invalid", True, b"short", UUID(int=0)):
            with self.subTest(kind=type(value).__name__):
                self.assert_private_error(
                    lambda value=value: self.module.extract_credential(store(), value)
                )

    def test_unknown_missing_encoding_or_wrong_raw_type(self):
        for item in (
            record(encoding="unknown"),
            record(key=PSK.hex()),
            record(key="not base64!", encoding="oic.sec.encoding.base64"),
        ):
            self.assert_private_error(lambda item=item: self.extract(store([item])))
        item = record()
        del item["privatedata"]["encoding"]
        self.assert_private_error(lambda: self.extract(store([item])))

    def test_invalid_key_lengths(self):
        for length in (0, 15, 17, 31, 33):
            with self.subTest(length=length):
                self.assert_private_error(
                    lambda length=length: self.extract(
                        store([record(key=b"a" * length)])
                    )
                )

    def test_repr_does_not_include_credentials(self):
        text = repr(self.extract())
        for secret in (
            str(TARGET),
            str(CLIENT),
            PSK.hex(),
            repr(PSK),
            base64.b64encode(PSK).decode(),
        ):
            self.assertNotIn(secret, text)

    def test_export_is_private_and_round_trips_without_other_devices(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "saved.ocf-credential.json"
            credential = self.extract(
                store([record(target=OTHER, key=OTHER_PSK), record()])
            )
            self.module.export_credential(credential, destination)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            payload = destination.read_bytes()
            data = json.loads(payload)
            self.assertEqual(
                set(data),
                {
                    "schema_version",
                    "target_device_uuid",
                    "client_identity_uuid",
                    "psk_encoding",
                    "psk",
                },
            )
            self.assertEqual(data["schema_version"], 1)
            self.assertEqual(data["psk_encoding"], "base64")
            self.assertNotIn(str(OTHER).encode(), payload)
            self.assertNotIn(base64.b64encode(OTHER_PSK), payload)
            restored = self.module.parse_portable_credential(payload, TARGET)
            self.assertEqual(restored, credential)

    def test_existing_output_and_symlink_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "existing"
            original.write_bytes(b"keep")
            link = Path(directory) / "link"
            link.symlink_to(original)
            for destination in (original, link):
                self.assert_private_error(
                    lambda destination=destination: self.module.export_credential(
                        self.extract(), destination
                    )
                )
            self.assertEqual(original.read_bytes(), b"keep")

    def test_export_failure_removes_only_its_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "new"
            with patch.object(self.module.os, "fsync", side_effect=OSError(PSK.hex())):
                self.assert_private_error(
                    lambda: self.module.export_credential(self.extract(), destination)
                )
            self.assertFalse(destination.exists())

    def test_interrupted_export_leaves_no_plaintext_file(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "new"
            with (
                patch.object(self.module.os, "fsync", side_effect=KeyboardInterrupt),
                self.assertRaises(KeyboardInterrupt),
            ):
                self.module.export_credential(self.extract(), destination)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_failure_cleanup_cannot_unlink_a_replacement_after_identity_check(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "new"
            original_lstat = Path.lstat

            def raced_lstat(path, *args, **kwargs):
                result = original_lstat(path, *args, **kwargs)
                if path == destination:
                    path.unlink()
                    path.write_bytes(b"replacement")
                return result

            def fail_sync(_fd):
                if not destination.exists():
                    destination.write_bytes(b"replacement")
                raise OSError("synthetic failure")

            with (
                patch.object(self.module.os, "fsync", side_effect=fail_sync),
                patch.object(Path, "lstat", raced_lstat),
            ):
                self.assert_private_error(
                    lambda: self.module.export_credential(self.extract(), destination)
                )
            self.assertTrue(
                destination.exists(), "cleanup deleted another writer's file"
            )
            self.assertEqual(destination.read_bytes(), b"replacement")

    def test_output_fd_is_closed_when_fdopen_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "new"
            descriptors = []
            real_open = self.module.os.open

            def tracked_open(path, flags, *args, **kwargs):
                fd = real_open(path, flags, *args, **kwargs)
                if flags & self.module.os.O_WRONLY:
                    descriptors.append(fd)
                return fd

            def cleanup():
                for fd in descriptors:
                    with contextlib.suppress(OSError):
                        self.module.os.close(fd)

            self.addCleanup(cleanup)
            with (
                patch.object(self.module.os, "open", side_effect=tracked_open),
                patch.object(
                    self.module.os, "fdopen", side_effect=OSError("synthetic failure")
                ),
            ):
                self.assert_private_error(
                    lambda: self.module.export_credential(self.extract(), destination)
                )
            for fd in descriptors:
                with self.assertRaises(OSError):
                    self.module.os.fstat(fd)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_nonregular_input_and_dangling_output_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assert_private_error(lambda: self.module._read_input(Path(directory)))
            fifo = Path(directory) / "fifo"
            self.module.os.mkfifo(fifo)
            self.assert_private_error(lambda: self.module._read_input(fifo))
            link = Path(directory) / "dangling"
            link.symlink_to(Path(directory) / "missing")
            self.assert_private_error(
                lambda: self.module.export_credential(self.extract(), link)
            )
            self.assertTrue(link.is_symlink())
            self.assertFalse(link.exists())

    def test_failed_export_does_not_remove_a_replaced_output(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "new"

            def replace_output(_fd):
                if destination.exists():
                    destination.unlink()
                destination.write_bytes(b"external file")
                raise OSError(PSK.hex())

            with patch.object(self.module.os, "fsync", side_effect=replace_output):
                self.assert_private_error(
                    lambda: self.module.export_credential(self.extract(), destination)
                )
            self.assertEqual(destination.read_bytes(), b"external file")

    def test_portable_rejects_wrong_target_version_encoding_and_length(self):
        valid = {
            "schema_version": 1,
            "target_device_uuid": str(TARGET),
            "client_identity_uuid": str(CLIENT),
            "psk_encoding": "base64",
            "psk": base64.b64encode(PSK).decode(),
        }
        for changes in (
            {"target_device_uuid": str(OTHER)},
            {"schema_version": 2},
            {"schema_version": True},
            {"psk_encoding": "raw"},
            {"psk": "broken!"},
            {"psk": base64.b64encode(b"short").decode()},
            {"unexpected": "not allowed"},
        ):
            with self.subTest(changes=list(changes)):
                blob = json.dumps({**valid, **changes}).encode()
                self.assert_private_error(
                    lambda blob=blob: self.module.parse_portable_credential(
                        blob, TARGET
                    )
                )

    def test_portable_rejects_duplicate_json_keys(self):
        data = b'{"schema_version":1,"schema_version":2}'
        self.assert_private_error(
            lambda: self.module.parse_portable_credential(data, TARGET)
        )

    def test_oversized_inputs_are_rejected(self):
        blob = b"x" * (self.module.MAX_INPUT_BYTES + 1)
        self.assert_private_error(lambda: self.extract(blob))
        self.assert_private_error(
            lambda: self.module.parse_portable_credential(blob, TARGET)
        )

    def test_cli_validation_is_read_only_and_does_not_open_sockets(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "store.dat"
            source.write_bytes(store())
            before = source.stat().st_mtime_ns
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
                patch("socket.socket", side_effect=AssertionError("must stay offline")),
            ):
                result = self.module.main(
                    ["--store", str(source), "--target-device-uuid", str(TARGET)]
                )
            self.assertEqual(result, 0)
            self.assertEqual(source.read_bytes(), store())
            self.assertEqual(source.stat().st_mtime_ns, before)
            self.assertEqual(list(Path(directory).iterdir()), [source])
            self.assertNotIn(str(TARGET), stdout.getvalue() + stderr.getvalue())
            self.assertNotIn(str(CLIENT), stdout.getvalue() + stderr.getvalue())
            self.assertIn("no secret exported", stdout.getvalue().lower())

    def test_cli_export_and_portable_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "store.dat"
            destination = Path(directory) / "export.ocf-credential.json"
            source.write_bytes(store())
            args = [
                "--store",
                str(source),
                "--target-device-uuid",
                str(TARGET),
                "--output",
                str(destination),
            ]
            first = self.run_cli(args)
            self.assertEqual(first.returncode, 0, first.stderr)
            second = self.run_cli(
                ["--credential", str(destination), "--target-device-uuid", str(TARGET)]
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(self.run_cli(args).returncode, 1)
            for result in (first, second):
                self.assert_no_cli_secrets(result)

    def run_cli(self, args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def assert_no_cli_secrets(self, result):
        text = result.stdout + result.stderr
        for secret in (
            str(TARGET),
            str(CLIENT),
            PSK.hex(),
            base64.b64encode(PSK).decode(),
        ):
            self.assertNotIn(secret, text)
        self.assertNotIn("Traceback", text)

    def test_cli_failures_and_argument_errors_are_redacted(self):
        cases = (
            ["--store", PSK.hex(), "--target-device-uuid", str(TARGET)],
            ["--store", "/missing", "--target-device-uuid", PSK.hex()],
            ["--unknown", PSK.hex()],
            ["--psk", base64.b64encode(PSK).decode()],
        )
        for args in cases:
            with self.subTest(option=args[0]):
                result = self.run_cli(args)
                self.assertNotEqual(result.returncode, 0)
                self.assert_no_cli_secrets(result)


if __name__ == "__main__":
    unittest.main()
