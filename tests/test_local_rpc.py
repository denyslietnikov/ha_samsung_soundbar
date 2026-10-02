"""Local protocol and volume transactions, without a real soundbar or Cloud."""

import asyncio
import json
from unittest import IsolatedAsyncioTestCase
from unittest.mock import MagicMock, patch

from aiohttp import ClientConnectionError, ClientResponseError

from custom_components.samsung_soundbar.api_extension.SoundbarDevice import (
    SoundbarDevice,
)
from custom_components.samsung_soundbar.const import CONTROL_MODE_LOCAL_ONLY
from custom_components.samsung_soundbar.local_device import LocalDevice
from custom_components.samsung_soundbar.local_rpc import (
    LocalRpcAuthError,
    LocalRpcCommandError,
    LocalRpcError,
    LocalSoundbarRpcClient,
)


class RpcResponse:
    def __init__(self, payload, handler):
        self.payload = payload
        self.handler = handler
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    def raise_for_status(self):
        pass

    async def json(self, **kwargs):
        return await self.handler(self.payload)


class SoundbarFixture:
    """Stateful RPC responses; relative keys can be dropped or combined."""

    def __init__(self):
        self.requests = []
        self.responses = []
        self.volume = 8
        self.power = "powerOn"
        self.mute = False
        self.source = "E_ARC"
        self.mode = "GAME"
        self.direct_volume = False
        self.steps = []
        self.session = MagicMock()
        self.session.post.side_effect = self.post
        self.handler = self.respond

    def post(self, url, *, data, **kwargs):
        assert url == "https://192.0.2.26:1516/"
        payload = json.loads(data)
        self.requests.append(payload)
        response = RpcResponse(payload, self.handler)
        self.responses.append(response)
        return response

    async def respond(self, payload):
        method = payload["method"]
        params = payload.get("params", {})
        if method == "createAccessToken":
            result = {"AccessToken": "private-local-token"}
        elif method == "setVolume":
            if not self.direct_volume:
                return {"error": {"code": -32601, "message": "Method not found"}}
            self.volume = params["volume"]
            result = {"success": True}
        elif method == "remoteKeyControl":
            key = params["remoteKey"]
            if key == "MUTE":
                self.mute = not self.mute
            else:
                step = self.steps.pop(0) if self.steps else 1
                self.volume += step if key == "VOL_UP" else -step
            result = {"success": True}
        else:
            result = {
                "powerControl": {"power": self.power},
                "getVolume": {"volume": str(self.volume)},
                "getMute": {"mute": self.mute},
                "inputSelectControl": {"inputSource": self.source},
                "soundModeControl": {"soundMode": self.mode},
                "getCodec": {"codec": "PCM"},
                "getIdentifier": {"identifier": "22_AV_HW-Q800F"},
            }[method]
        return {"result": result}

    @property
    def methods(self):
        return [request["method"] for request in self.requests]


class TestLocalRpc(IsolatedAsyncioTestCase):
    def setUp(self):
        self.soundbar = SoundbarFixture()
        self.rpc = LocalSoundbarRpcClient("192.0.2.26", self.soundbar.session)

    async def test_status_fixture_and_response_cleanup(self):
        self.assertEqual(
            await self.rpc.status(),
            {
                "power": "powerOn",
                "volume": 8,
                "mute": False,
                "input_source": "E_ARC",
                "sound_mode": "GAME",
                "codec": "PCM",
                "identifier": "22_AV_HW-Q800F",
            },
        )
        self.assertEqual(self.soundbar.methods.count("createAccessToken"), 1)
        self.assertTrue(all(response.closed for response in self.soundbar.responses))

    async def test_direct_volume_does_not_send_relative_keys(self):
        self.soundbar.direct_volume = True
        await self.rpc.set_volume(11)
        self.assertEqual(await self.rpc.volume(), 11)
        self.assertNotIn("remoteKeyControl", self.soundbar.methods)

    async def test_relative_volume_reads_every_step_including_dropped_commands(self):
        self.soundbar.steps = [0, 2, 0, 1]
        await self.rpc.set_volume(11)
        self.assertEqual(self.soundbar.volume, 11)
        self.assertEqual(
            self.soundbar.methods,
            [
                "createAccessToken",
                "setVolume",
                "getVolume",
                "remoteKeyControl",
                "getVolume",
                "remoteKeyControl",
                "getVolume",
                "remoteKeyControl",
                "getVolume",
                "remoteKeyControl",
                "getVolume",
            ],
        )

    async def test_combined_steps_can_overshoot_and_reverse_direction(self):
        self.soundbar.steps = [3, 1]
        await self.rpc.set_volume(10)
        keys = [
            request["params"]["remoteKey"]
            for request in self.soundbar.requests
            if request["method"] == "remoteKeyControl"
        ]
        self.assertEqual(keys, ["VOL_UP", "VOL_DOWN"])
        self.assertEqual(self.soundbar.volume, 10)

    async def test_unchanged_volume_stops_after_three_keys(self):
        self.soundbar.steps = [0] * 10
        with self.assertRaisesRegex(LocalRpcCommandError, "did not change"):
            await self.rpc.set_volume(10)
        self.assertEqual(self.soundbar.methods.count("remoteKeyControl"), 3)
        self.assertEqual(await self.rpc.volume(), 8)

    async def test_command_limit_stops_oscillating_readback(self):
        self.soundbar.steps = [2] * 10
        with (
            patch(
                "custom_components.samsung_soundbar.local_rpc.MAX_VOLUME_COMMANDS", 4
            ),
            self.assertRaisesRegex(LocalRpcCommandError, "command limit"),
        ):
            await self.rpc.set_volume(9)
        self.assertEqual(self.soundbar.methods.count("remoteKeyControl"), 4)

    async def test_equal_readback_sends_no_keys(self):
        await self.rpc.set_volume(8)
        self.assertNotIn("remoteKeyControl", self.soundbar.methods)

    async def test_timeout_auth_or_rejection_does_not_start_relative_fallback(self):
        original = self.soundbar.respond
        for error in (
            TimeoutError(),
            LocalRpcAuthError("invalid token"),
            LocalRpcCommandError("invalid params"),
        ):
            with self.subTest(error=type(error).__name__):

                async def respond(payload, error=error):
                    if payload["method"] == "setVolume":
                        raise error
                    return await original(payload)

                self.soundbar.handler = respond
                with self.assertRaises(LocalRpcError):
                    await self.rpc.set_volume(9)
                self.assertNotIn("remoteKeyControl", self.soundbar.methods)

    async def test_success_false_is_a_command_failure(self):
        async def respond(payload):
            return {"result": {"success": False}}

        self.rpc._token = "private-local-token"
        self.soundbar.handler = respond
        with self.assertRaisesRegex(LocalRpcCommandError, "success: false"):
            await self.rpc.set_volume(10)
        self.assertEqual(self.soundbar.methods, ["setVolume"])

    async def test_invalid_volume_and_mute_are_not_silently_coerced(self):
        for value in (None, True, 8.5, "bad", -1, 101):
            with self.subTest(volume=value):
                self.soundbar.volume = value

                # Return the original type, not the fixture's string representation.
                async def respond(payload, value=value):
                    return {"result": {"volume": value}}

                self.rpc._token = "private-local-token"
                self.soundbar.handler = respond
                with self.assertRaises(LocalRpcError):
                    await self.rpc.volume()
        for value in (None, "unknown", 2, 0.5):
            with self.subTest(mute=value):

                async def respond(payload, value=value):
                    return {"result": {"mute": value}}

                self.soundbar.handler = respond
                with self.assertRaises(LocalRpcError):
                    await self.rpc.is_muted()

    async def test_false_string_mute_and_valid_volume_strings(self):
        self.soundbar.mute = "false"
        self.assertFalse(await self.rpc.is_muted())
        self.assertEqual(await self.rpc.volume(), 8)

    async def test_invalid_target_sends_nothing(self):
        for value in (-1, 101, None, True, 8.5, "8"):
            with self.subTest(target=value), self.assertRaises(ValueError):
                await self.rpc.set_volume(value)
        self.assertEqual(self.soundbar.requests, [])

    async def test_poll_and_second_volume_write_wait_for_complete_transaction(self):
        started, release = asyncio.Event(), asyncio.Event()
        original = self.soundbar.respond

        async def respond(payload):
            if payload["method"] == "remoteKeyControl" and not started.is_set():
                started.set()
                await release.wait()
            return await original(payload)

        self.soundbar.handler = respond
        write = asyncio.create_task(self.rpc.set_volume(10))
        await started.wait()
        poll = asyncio.create_task(self.rpc.volume())
        second = asyncio.create_task(self.rpc.set_volume(7))
        await asyncio.sleep(0)
        self.assertFalse(poll.done())
        self.assertFalse(second.done())
        release.set()
        await write
        self.assertEqual(await poll, 10)
        await second
        self.assertEqual(self.soundbar.volume, 7)

    async def test_cancellation_releases_both_locks(self):
        started = asyncio.Event()
        original = self.soundbar.respond

        async def respond(payload):
            if payload["method"] == "remoteKeyControl":
                started.set()
                await asyncio.Event().wait()
            return await original(payload)

        self.soundbar.handler = respond
        task = asyncio.create_task(self.rpc.set_volume(9))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.soundbar.handler = original
        self.assertEqual(await asyncio.wait_for(self.rpc.volume(), 1), 8)
        self.assertTrue(all(response.closed for response in self.soundbar.responses))

    async def test_adjustment_deadline_releases_lock(self):
        original = self.soundbar.respond

        async def respond(payload):
            if payload["method"] == "remoteKeyControl":
                await asyncio.sleep(1)
            return await original(payload)

        self.soundbar.handler = respond
        with (
            patch(
                "custom_components.samsung_soundbar.local_rpc.VOLUME_ADJUSTMENT_TIMEOUT",
                0.01,
            ),
            self.assertRaisesRegex(LocalRpcCommandError, "timed out"),
        ):
            await self.rpc.set_volume(9)
        self.assertEqual(await self.rpc.volume(), 8)

    async def test_token_rejection_refreshes_once_and_redacts_errors(self):
        self.rpc._token = "expired-secret"
        original = self.soundbar.respond

        async def respond(payload):
            if payload.get("params", {}).get("AccessToken") == "expired-secret":
                return {"error": {"message": "invalid token expired-secret"}}
            return await original(payload)

        self.soundbar.handler = respond
        self.assertEqual(await self.rpc.volume(), 8)
        self.assertEqual(
            self.soundbar.methods, ["getVolume", "createAccessToken", "getVolume"]
        )

        async def reject(payload):
            if payload["method"] == "createAccessToken":
                return await original(payload)
            return {
                "error": {
                    "message": "invalid token " + payload["params"]["AccessToken"]
                }
            }

        self.soundbar.handler = reject
        self.soundbar.requests.clear()
        with self.assertRaises(LocalRpcAuthError) as context:
            await self.rpc.volume()
        self.assertNotIn("private-local-token", str(context.exception))
        self.assertEqual(self.soundbar.methods.count("createAccessToken"), 1)
        self.assertIsNone(self.rpc.token_length)

    async def test_http_unauthorized_refreshes_token(self):
        self.rpc._token = "expired-secret"
        original = self.soundbar.respond

        async def respond(payload):
            if payload.get("params", {}).get("AccessToken") == "expired-secret":
                raise ClientResponseError(
                    MagicMock(), (), status=401, message="Unauthorized"
                )
            return await original(payload)

        self.soundbar.handler = respond
        self.assertEqual(await self.rpc.volume(), 8)
        self.assertEqual(self.soundbar.methods.count("createAccessToken"), 1)

    async def test_unauthenticated_auth_failure_does_not_create_token(self):
        async def respond(payload):
            return {"error": {"message": "authorization required"}}

        self.soundbar.handler = respond
        with self.assertRaises(LocalRpcAuthError):
            await self.rpc.call("getVolume", authenticated=False)
        self.assertEqual(self.soundbar.methods, ["getVolume"])

    async def test_status_requests_never_overlap(self):
        active = 0
        maximum = 0
        original = self.soundbar.respond

        async def respond(payload):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(0)
                return await original(payload)
            finally:
                active -= 1

        self.soundbar.handler = respond
        await asyncio.gather(self.rpc.status(), self.rpc.status())
        self.assertEqual(maximum, 1)
        self.assertEqual(self.soundbar.methods.count("createAccessToken"), 1)

    async def test_relative_down_and_boundary_targets(self):
        for target in (0, 100, 0):
            with self.subTest(target=target):
                await self.rpc.set_volume(target)
                self.assertEqual(await self.rpc.volume(), target)

    async def test_rpc_readback_reaches_shared_ha_state_and_recovers(self):
        cloud_session = MagicMock()
        cloud_session.get.side_effect = AssertionError("No Cloud GET allowed")
        cloud_session.post.side_effect = AssertionError("No Cloud POST allowed")
        device = SoundbarDevice(
            LocalDevice("local-id"),
            session=cloud_session,
            max_volume=100,
            device_name="Soundbar Q800F",
            control_mode=CONTROL_MODE_LOCAL_ONLY,
            local_rpc=self.rpc,
        )
        await device.update_local_status(min_age=None)
        self.assertTrue(device.available)
        self.assertEqual(device.volume_level, 0.08)
        self.assertEqual(device.sound_mode, "Game Pro")
        self.assertEqual(device.input_source, "TV ARC/eARC")

        self.soundbar.source = "WIFI_AIRPLAY"
        self.soundbar.mode = "SURROUND"
        self.soundbar.mute = True
        await device.update_local_input_source(min_age=None)
        await device.update_local_input_source(min_age=None)
        self.assertEqual(device.input_source, "WIFI")
        self.assertEqual(device.sound_from_detail_name, "AirPlay")
        self.assertEqual(device.sound_mode, "Surround")
        self.assertTrue(device.volume_muted)
        await device.set_volume(0.10)
        self.assertEqual(device.volume_level, 0.10)

        self.soundbar.session.post.side_effect = ClientConnectionError("offline")
        await device.update_local_input_source(min_age=None)
        self.assertFalse(device.available)
        self.soundbar.session.post.side_effect = self.soundbar.post
        self.soundbar.source = "E_ARC"
        await device.update_local_input_source(min_age=None)
        self.assertTrue(device.available)
        self.assertEqual(device.sound_from_detail_name, "External Device")
        cloud_session.get.assert_not_called()
        cloud_session.post.assert_not_called()

    async def test_transport_failure_does_not_refresh_token_and_recovers(self):
        await self.rpc.volume()
        self.soundbar.session.post.side_effect = ClientConnectionError("offline")
        with self.assertRaises(LocalRpcError):
            await self.rpc.volume()
        self.soundbar.session.post.side_effect = self.soundbar.post
        self.assertEqual(await self.rpc.volume(), 8)
        self.assertEqual(self.soundbar.methods.count("createAccessToken"), 1)

    async def test_optional_methods_do_not_hide_core_state(self):
        original = self.soundbar.respond

        async def respond(payload):
            if payload["method"] in ("getCodec", "getIdentifier"):
                return {"error": {"code": -32601, "message": "Method not found"}}
            return await original(payload)

        self.soundbar.handler = respond
        status = await self.rpc.status()
        self.assertEqual(status["volume"], 8)
        self.assertIsNone(status["codec"])
        self.assertIsNone(status["identifier"])

    async def test_status_waits_for_all_requests_before_reporting_error(self):
        original = self.soundbar.respond

        async def respond(payload):
            if payload["method"] == "powerControl":
                raise LocalRpcError("offline")
            await asyncio.sleep(0)
            return await original(payload)

        self.soundbar.handler = respond
        with self.assertRaises(LocalRpcError):
            await self.rpc.status()
        self.assertIn("getIdentifier", self.soundbar.methods)
        self.assertTrue(all(response.closed for response in self.soundbar.responses))
