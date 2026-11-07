"""Recover one Q800F credential from a copied store with read-only LAN verification."""

from __future__ import annotations

import ipaddress
import logging
import sys
from pathlib import Path

import extract_ocf_credential as offline


def _load_protocol():
    try:
        from smartthings_local.protocol.auth import PskAuth
        from smartthings_local.protocol.dtls_session import DtlsCoapSession
        from smartthings_local.protocol.ocf_discovery import (
            discover_ocf_secure_ports,
            read_plaintext_ocf_resource,
        )
    except ImportError:
        raise offline.CredentialError(
            "Install dependencies from scripts/requirements-ocf-recovery.txt."
        ) from None
    return (
        read_plaintext_ocf_resource,
        discover_ocf_secure_ports,
        PskAuth,
        DtlsCoapSession,
    )


def recover_credential(source: Path, host: str) -> offline.OcfCredential:
    """Read the selected file, match live identity, then prove PSK authentication."""
    data = offline._read_input(source)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise offline.CredentialError(
            "Host must be a numeric LAN IP address."
        ) from None
    if (
        address.is_global
        or address.is_loopback
        or address.is_multicast
        or address.is_unspecified
    ):
        raise offline.CredentialError("Host must be a unicast LAN IP address.")

    previous_logging = logging.root.manager.disable
    try:
        # This is a standalone process; suppress dependency logs containing identities.
        logging.disable(logging.CRITICAL)
        read, discover, auth_type, session_type = _load_protocol()

        def public_map(href):
            response = read(host, href, timeout=3, retries=1)
            if not response.successful or not response.payload:
                raise offline.CredentialError(
                    "Q800F discovery failed; check power and LAN access."
                )
            return offline._decode_cbor(response.payload)

        if public_map("/oic/p").get("mnmo") != "HW-Q800F":
            raise offline.CredentialError(
                "The selected host did not identify as HW-Q800F."
            )
        target = offline._uuid(public_map("/oic/d").get("di"))
        credential = offline.extract_credential(data, target)
        ports = discover(host, timeout=3, retries=1, order="directory-first").ports
        if len(ports) != 1 or type(ports[0]) is not int or not 1 <= ports[0] <= 65535:
            raise offline.CredentialError(
                "Q800F did not advertise one usable secure endpoint."
            )
        session = session_type(
            host,
            ports[0],
            auth=auth_type(
                identity=credential.client_identity_uuid.bytes, key=credential.psk
            ),
            write_max_attempts=1,
            rate_limit_rps=2,
        )
        try:
            session.connect(timeout=12)
            session.start_reader()
            code, body = session.get(["oic", "d"], timeout=6)
            if (
                code != 69
                or not body
                or offline._uuid(offline._decode_cbor(body).get("di")) != target
            ):
                raise offline.CredentialError(
                    "Authenticated Q800F identity verification failed."
                )
        finally:
            session.close()
        return credential
    except offline.CredentialError:
        raise
    except Exception:  # noqa: BLE001 - suppress dependency exceptions containing private data
        raise offline.CredentialError(
            "Q800F authentication failed; no credential exported."
        ) from None
    finally:
        logging.disable(previous_logging)


def main(argv: list[str] | None = None) -> int:
    parser = offline._PrivateArgumentParser(
        prog="recover_q800f_credential.py",
        description="Read a copied OCF store and verify its Q800F key over LAN. No device writes.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--store",
        required=True,
        metavar="FILE",
        help="Copied hashed backup file; no renaming needed.",
    )
    parser.add_argument(
        "--host",
        required=True,
        metavar="LAN_IP",
        help="Q800F address; UUID is read automatically.",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        help="Explicit private plaintext export; never overwritten.",
    )
    args = parser.parse_args(argv)
    try:
        credential = recover_credential(Path(args.store), args.host)
        if args.output:
            offline.export_credential(credential, Path(args.output))
            print(
                "Q800F credential authenticated and exported to a private plaintext file. Encrypt it before archiving."
            )
        else:
            print("Q800F credential authenticated; no secret exported.")
        return 0
    except offline.CredentialError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - no private file paths or parser exceptions at the CLI boundary
        print("Recovery failed; private details were suppressed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
