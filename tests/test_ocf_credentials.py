"""Portable OCF credential contract; fixtures contain no real keys."""

import base64
import importlib
import json
import unittest
from pathlib import Path
from uuid import UUID

TARGET = UUID("00000000-0000-4000-8000-000000000001")
CLIENT = UUID("00000000-0000-4000-8000-000000000002")
PSK = bytes(range(16))


def bundle(**updates):
    return json.dumps(
        {
            "schema_version": 1,
            "target_device_uuid": str(TARGET),
            "client_identity_uuid": str(CLIENT),
            "psk_encoding": "base64",
            "psk": base64.b64encode(PSK).decode(),
            **updates,
        }
    )


class TestOcfCredentials(unittest.TestCase):
    def setUp(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "custom_components/samsung_soundbar/ocf_credentials.py"
        )
        self.assertTrue(path.exists(), "OCF credential storage is not implemented")
        self.module = importlib.import_module(
            "custom_components.samsung_soundbar.ocf_credentials"
        )

    def test_portable_bundle_and_nul_identity(self):
        credential = self.module.parse_credential_bundle(bundle())
        self.assertEqual(credential.target_device_uuid, TARGET)
        self.assertEqual(credential.client_identity_uuid.bytes, CLIENT.bytes)
        self.assertEqual(credential.psk, PSK)
        self.assertIn(b"\x00", credential.client_identity_uuid.bytes)
        self.assertEqual(
            self.module.parse_credential_bundle(json.dumps(credential.to_storage())),
            credential,
        )

    def test_repr_is_private(self):
        text = repr(self.module.parse_credential_bundle(bundle()))
        for secret in (
            str(TARGET),
            str(CLIENT),
            PSK.hex(),
            repr(PSK),
            base64.b64encode(PSK).decode(),
        ):
            self.assertNotIn(secret, text)

    def test_invalid_bundles_have_only_safe_errors(self):
        cases = [
            "",
            "{",
            "[]",
            '{"schema_version":1,"schema_version":1}',
            bundle(schema_version=2),
            bundle(schema_version=True),
            bundle(psk="invalid!"),
            bundle(psk=base64.b64encode(b"short").decode()),
            bundle(psk_encoding="raw"),
            bundle(client_identity_uuid=str(UUID(int=0))),
            bundle(target_device_uuid="invalid"),
            bundle(extra="forbidden"),
        ]
        for value in cases:
            with self.subTest(length=len(value)):
                with self.assertRaises(self.module.OcfCredentialError) as error:
                    self.module.parse_credential_bundle(value)
                for secret in (
                    str(TARGET),
                    str(CLIENT),
                    PSK.hex(),
                    base64.b64encode(PSK).decode(),
                ):
                    self.assertNotIn(
                        secret, str(error.exception) + repr(error.exception)
                    )

    def test_32_byte_key_and_input_limit(self):
        value = bundle(psk=base64.b64encode(bytes(range(32))).decode())
        self.assertEqual(len(self.module.parse_credential_bundle(value).psk), 32)
        with self.assertRaises(self.module.OcfCredentialError):
            self.module.parse_credential_bundle(" " * 65537)

    def test_direct_construction_rejects_invalid_credentials(self):
        with self.assertRaises(self.module.OcfCredentialError):
            self.module.OcfCredential(TARGET, CLIENT, b"short")


if __name__ == "__main__":
    unittest.main()
