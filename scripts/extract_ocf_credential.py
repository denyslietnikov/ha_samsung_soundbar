"""Validate or explicitly export one device's existing OCF credential offline."""

from __future__ import annotations

import argparse
import base64
import binascii
import io
import json
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

MAX_INPUT_BYTES = 8 * 1024 * 1024
PORTABLE_FIELDS = {
    "schema_version",
    "target_device_uuid",
    "client_identity_uuid",
    "psk_encoding",
    "psk",
}


class CredentialError(ValueError):
    """A public, redacted validation or file-operation error."""


def _uuid(value: object) -> UUID:
    try:
        if isinstance(value, UUID):
            result = value
        elif isinstance(value, bytes):
            result = UUID(bytes=value)
        elif isinstance(value, str):
            result = UUID(value.removeprefix("uuid:"))
        else:
            raise TypeError
        if result.int == 0:
            raise ValueError
        return result
    except (ValueError, TypeError, AttributeError):
        raise CredentialError("Invalid or missing UUID.") from None


def _validate_key(value: object) -> bytes:
    if not isinstance(value, bytes) or len(value) not in (16, 32):
        raise CredentialError("The PSK must contain exactly 16 or 32 bytes.")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class OcfCredential:
    """Only the selected device; the default repr reveals no field values."""

    target_device_uuid: UUID
    client_identity_uuid: UUID
    psk: bytes

    def __post_init__(self) -> None:
        _uuid(self.target_device_uuid)
        _uuid(self.client_identity_uuid)
        _validate_key(self.psk)


def _bounded_input(data: bytes) -> None:
    if not isinstance(data, bytes) or not data or len(data) > MAX_INPUT_BYTES:
        raise CredentialError("Input is empty, invalid or exceeds the size limit.")


def _decode_cbor(data: bytes) -> dict:
    _bounded_input(data)
    try:
        import cbor2
    except ImportError:
        raise CredentialError(
            "Install dependencies from scripts/requirements-ocf.txt."
        ) from None
    stream = io.BytesIO(data)
    try:
        value = cbor2.CBORDecoder(
            stream, max_depth=32, allow_duplicate_keys=False, strict_hashable_types=True
        ).decode()
    except (
        cbor2.CBORDecodeError,
        ValueError,
        TypeError,
        EOFError,
        OverflowError,
        RecursionError,
    ):
        raise CredentialError("Invalid or unsupported CBOR credential store.") from None
    if stream.tell() != len(data) or not isinstance(value, dict):
        raise CredentialError("Expected exactly one CBOR map without trailing data.")
    return value


def _decode_key(data: object, encoding: object) -> bytes:
    if encoding == "raw":
        return _validate_key(data)
    if encoding != "base64" or not isinstance(data, (str, bytes)):
        raise CredentialError("Unsupported PSK encoding.")
    try:
        decoded = base64.b64decode(data, validate=True)
    except (ValueError, TypeError, binascii.Error):
        raise CredentialError("Invalid base64 PSK.") from None
    return _validate_key(decoded)


def extract_credential(data: bytes, target_device_uuid: str | UUID) -> OcfCredential:
    """Select exactly one PSK record; never infer identity from device ownership."""
    target = _uuid(target_device_uuid)
    credentials = _decode_cbor(data).get("cred")
    if isinstance(credentials, bytes):
        credentials = _decode_cbor(credentials)
    if not isinstance(credentials, dict) or not isinstance(
        credentials.get("creds"), list
    ):
        raise CredentialError("Missing or invalid credential records.")
    matches = []
    for record in credentials["creds"]:
        if not isinstance(record, dict):
            raise CredentialError("Invalid credential record.")
        if type(record.get("credtype")) is not int or record["credtype"] != 1:
            continue
        if _uuid(record.get("subjectuuid")) == target:
            matches.append(record)
    if len(matches) != 1:
        raise CredentialError(
            "Expected exactly one PSK record for the selected device."
        )
    private = matches[0].get("privatedata")
    if not isinstance(private, dict):
        raise CredentialError("Missing private credential data.")
    encodings = {"oic.sec.encoding.raw": "raw", "oic.sec.encoding.base64": "base64"}
    encoding = private.get("encoding")
    if not isinstance(encoding, str) or encoding not in encodings:
        raise CredentialError("Unsupported PSK encoding.")
    return OcfCredential(
        target,
        _uuid(credentials.get("rowneruuid")),
        _decode_key(private.get("data"), encodings[encoding]),
    )


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise CredentialError("Duplicate portable credential field.")
        result[key] = value
    return result


def parse_portable_credential(
    data: bytes, target_device_uuid: str | UUID
) -> OcfCredential:
    """Validate a previously exported bundle without contacting the device."""
    target = _uuid(target_device_uuid)
    _bounded_input(data)
    try:
        value = json.loads(data, object_pairs_hook=_unique_json_object)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise CredentialError("Invalid portable credential JSON.") from None
    if not isinstance(value, dict) or set(value) != PORTABLE_FIELDS:
        raise CredentialError("Invalid portable credential fields.")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise CredentialError("Unsupported portable credential version.")
    if _uuid(value["target_device_uuid"]) != target:
        raise CredentialError("Portable credential does not match the selected device.")
    if value["psk_encoding"] != "base64":
        raise CredentialError("Unsupported portable PSK encoding.")
    return OcfCredential(
        target,
        _uuid(value["client_identity_uuid"]),
        _decode_key(value["psk"], "base64"),
    )


def _read_input(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            with os.fdopen(fd, "rb", closefd=False) as stream:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise CredentialError("Input must be a regular file.")
                data = stream.read(MAX_INPUT_BYTES + 1)
        finally:
            os.close(fd)
    except OSError:
        raise CredentialError("Cannot read input file.") from None
    _bounded_input(data)
    return data


def export_credential(credential: OcfCredential, destination: Path) -> None:
    """Create a 0600 plaintext bundle exclusively; callers must encrypt backups."""
    data = {
        "schema_version": 1,
        "target_device_uuid": str(credential.target_device_uuid),
        "client_identity_uuid": str(credential.client_identity_uuid),
        "psk_encoding": "base64",
        "psk": base64.b64encode(credential.psk).decode("ascii"),
    }
    try:
        # Publish only a complete file. Cleanup never touches the destination,
        # which another writer may replace immediately after publication.
        with tempfile.TemporaryDirectory(
            prefix=".ocf-export-", dir=destination.parent
        ) as directory:
            pending = Path(directory) / "credential"
            fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as stream:
                    os.fchmod(fd, 0o600)
                    json.dump(data, stream, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(fd)
            finally:
                os.close(fd)
            os.link(pending, destination, follow_symlinks=False)
    except OSError:
        raise CredentialError(
            "Cannot export: destination exists or a file operation failed."
        ) from None


class _PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.exit(
            2, "Invalid arguments. Use --help; never pass a PSK on the command line.\n"
        )


def main(argv: list[str] | None = None) -> int:
    parser = _PrivateArgumentParser(
        prog="extract_ocf_credential.py",
        description="Offline validation; no network requests or secret output to the terminal.",
        allow_abbrev=False,
    )
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument(
        "--store", metavar="FILE", help="Selected CBOR OCF store, not a whole backup."
    )
    inputs.add_argument(
        "--credential",
        metavar="FILE",
        help="Previously exported schema-v1 JSON bundle.",
    )
    parser.add_argument("--target-device-uuid", required=True, metavar="UUID")
    parser.add_argument(
        "--output",
        metavar="FILE",
        help="Explicit plaintext export, mode 0600; never overwritten.",
    )
    args = parser.parse_args(argv)
    try:
        target = _uuid(args.target_device_uuid)
        data = _read_input(Path(args.store or args.credential))
        credential = (
            extract_credential(data, target)
            if args.store
            else parse_portable_credential(data, target)
        )
        if args.output:
            export_credential(credential, Path(args.output))
            print(
                "Credential exported to a private plaintext file. Encrypt it before archiving."
            )
        else:
            print(
                "Credential structure validated offline; no secret exported. Authentication was not tested."
            )
        return 0
    except CredentialError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - never expose private parser/file exceptions at the CLI boundary
        print(
            "Credential operation failed; private details were suppressed.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
