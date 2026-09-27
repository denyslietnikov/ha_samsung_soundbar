"""Tests for Q800F LAN metadata identity."""

from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock

from custom_components.samsung_soundbar.local_identity import (
    LocalIdentityError,
    async_read_local_identity,
    identities_match,
)


class TestLocalIdentity(IsolatedAsyncioTestCase):
    @staticmethod
    def response(*, json=None, body=b"", error=None, json_error=None):
        response = MagicMock()
        response.raise_for_status = MagicMock(side_effect=error)
        response.json = AsyncMock(return_value=json, side_effect=json_error)
        response.content.read = AsyncMock(return_value=body)
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=response)
        context.__aexit__ = AsyncMock(return_value=None)
        return context

    async def test_tizen_and_upnp_identifiers(self) -> None:
        session = MagicMock()
        session.get.side_effect = [
            self.response(json={"device": {"wifiMac": "94:E6:BA:89:BD:BA", "duid": "uuid:f289154e-3c4a-41d8-ba8f-86850cf9be07"}}),
            self.response(body=b'<root xmlns="urn:samsung.com:device-1-0"><device><UDN>uuid:ffa77b59-a763-4caa-ba60-028de1be4bdf</UDN></device></root>'),
        ]
        identity = await async_read_local_identity(session, "192.0.2.26", 8)
        self.assertEqual(identity["wifi_mac"], "94:e6:ba:89:bd:ba")
        self.assertIn("tizen_duid", identity)
        self.assertIn("upnp_udn", identity)
        self.assertTrue(identities_match(identity, {"wifi_mac": identity["wifi_mac"]}))
        self.assertFalse(identities_match(identity, {"wifi_mac": "94:e6:ba:89:bd:bb"}))
        self.assertFalse(identities_match(identity, {"other": "value"}))

    async def test_malformed_tizen_falls_back_to_upnp(self) -> None:
        session = MagicMock()
        session.get.side_effect = [
            self.response(json={"device": {"wifiMac": "00:00:00:00:00:00"}}),
            self.response(body=b'<root><device><UDN>uuid:ffa77b59-a763-4caa-ba60-028de1be4bdf</UDN></device></root>'),
        ]
        identity = await async_read_local_identity(session, "192.0.2.26", 8)
        self.assertEqual(list(identity), ["upnp_udn"])

    async def test_no_stable_identity_rejected(self) -> None:
        session = MagicMock()
        session.get.side_effect = [
            self.response(json={"device": {"wifiMac": None}}),
            self.response(body=b"<broken"),
        ]
        with self.assertRaises(LocalIdentityError):
            await async_read_local_identity(session, "192.0.2.26", 8)

    async def test_malformed_json_and_xml_are_rejected(self) -> None:
        session = MagicMock()
        session.get.side_effect = [
            self.response(json_error=ValueError("malformed JSON")),
            self.response(body=b"<broken"),
        ]
        with self.assertRaises(LocalIdentityError):
            await async_read_local_identity(session, "192.0.2.26", 8)

    async def test_tizen_timeout_falls_back_to_upnp(self) -> None:
        session = MagicMock()
        session.get.side_effect = [
            TimeoutError("Tizen unavailable"),
            self.response(body=b'<root><device><UDN>uuid:ffa77b59-a763-4caa-ba60-028de1be4bdf</UDN></device></root>'),
        ]
        identity = await async_read_local_identity(session, "192.0.2.26", 8)
        self.assertIn("upnp_udn", identity)
