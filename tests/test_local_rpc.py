"""Local protocol and volume transactions, without a real soundbar or Cloud."""

import asyncio
import json
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import ClientConnectionError, ClientResponseError
from homeassistant.exceptions import HomeAssistantError

from custom_components.samsung_soundbar.api_extension.SoundbarDevice import (
    SoundbarDevice,
)
from custom_components.samsung_soundbar.const import CONTROL_MODE_LOCAL_ONLY
from custom_components.samsung_soundbar.local_device import LocalDevice
from custom_components.samsung_soundbar.local_rpc import (
    LocalRpcAuthError,
    LocalRpcCommandError,
    LocalRpcError,
    LocalRpcMethodNotFoundError,
    LocalRpcParseError,
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
        self.volume_control = False
        self.mute_control = False
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
        elif method in ("setVolume", "volumeControl", "muteControl"):
            supported = {
                "setVolume": self.direct_volume,
                "volumeControl": self.volume_control,
                "muteControl": self.mute_control,
            }[method]
            if not supported:
                return {"error": {"code": -32601, "message": "Method not found"}}
            if method == "muteControl":
                self.mute = params["mute"]
            else:
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
        # Existing transaction tests exercise the legacy relative path.
        self.rpc._volume_control_supported = False

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

    async def test_bare_and_enveloped_errors_are_not_success_or_unbounded_auth_retry(
        self,
    ):
        self.rpc._token = "private-local-token"
        for wrapped in (True, False):
            for code, kind in (
                (-32700, LocalRpcParseError),
                (-32601, LocalRpcMethodNotFoundError),
                (-32602, LocalRpcCommandError),
            ):
                with self.subTest(wrapped=wrapped, code=code):
                    self.rpc._token = "private-local-token"

                    async def respond(payload, code=code, wrapped=wrapped):
                        error = {
                            "code": code,
                            "message": "Rejected private-local-token",
                        }
                        return {"error": error} if wrapped else error

                    self.soundbar.handler = respond
                    with self.assertRaises(kind) as caught:
                        await self.rpc.call("volumeControl", {"volume": 10})
                    self.assertEqual(caught.exception.code, code)
                    self.assertNotIn("private-local-token", str(caught.exception))
        self.assertEqual(len(self.soundbar.requests), 6)
        self.assertNotIn("createAccessToken", self.soundbar.methods)

    async def test_missing_result_is_not_reported_as_success(self):
        self.rpc._token = "private-local-token"

        async def respond(payload):
            return {"jsonrpc": "2.0", "id": payload["id"]}

        self.soundbar.handler = respond
        with self.assertRaisesRegex(LocalRpcError, "neither result"):
            await self.rpc.call("powerControl")

    async def test_identifier_cached_only_after_two_matching_reads(self):
        for _ in range(4):
            self.assertEqual((await self.rpc.status())["identifier"], "22_AV_HW-Q800F")
        self.assertEqual(self.soundbar.methods.count("getIdentifier"), 2)
        await self.rpc.identifier()
        self.assertEqual(self.soundbar.methods.count("getIdentifier"), 3)
        fresh_client = LocalSoundbarRpcClient("192.0.2.26", self.soundbar.session)
        await fresh_client.status()
        self.assertEqual(self.soundbar.methods.count("getIdentifier"), 4)

    async def test_identifier_change_requires_matching_confirmation(self):
        original = self.soundbar.respond
        identifiers = iter(("old", "new", "new"))

        async def respond(payload):
            if payload["method"] == "getIdentifier":
                return {"result": {"identifier": next(identifiers)}}
            return await original(payload)

        self.soundbar.handler = respond
        values = [(await self.rpc.status())["identifier"] for _ in range(4)]
        self.assertEqual(values, ["old", "new", "new", "new"])
        self.assertEqual(self.soundbar.methods.count("getIdentifier"), 3)

    async def test_known_legacy_volume_path_does_not_retry_unsupported_method(self):
        await self.rpc.set_volume(8)
        await self.rpc.mute_toggle()
        self.assertNotIn("volumeControl", self.soundbar.methods)
        self.assertNotIn("muteControl", self.soundbar.methods)

    async def test_absolute_volume_and_mute_use_direct_controls(self):
        self.soundbar.volume_control = self.soundbar.mute_control = True
        client = LocalSoundbarRpcClient("192.0.2.26", self.soundbar.session)
        await client.set_volume(11)
        await client.set_mute(True)
        self.assertEqual(await client.volume(), 11)
        self.assertTrue(await client.is_muted())
        self.assertIn("volumeControl", self.soundbar.methods)
        self.assertIn("muteControl", self.soundbar.methods)
        self.assertNotIn("remoteKeyControl", self.soundbar.methods)

    async def test_direct_volume_unsupported_is_cached_and_falls_back(self):
        client = LocalSoundbarRpcClient("192.0.2.26", self.soundbar.session)
        await client.set_volume(9)
        await client.set_volume(10)
        self.assertEqual(self.soundbar.methods.count("volumeControl"), 1)
        self.assertEqual(await client.volume(), 10)

    async def test_mute_fallback_reads_device_and_never_double_toggles(self):
        client = LocalSoundbarRpcClient("192.0.2.26", self.soundbar.session)
        await client.set_mute(True)
        await client.set_mute(True)
        self.assertEqual(self.soundbar.methods.count("muteControl"), 1)
        self.assertEqual(self.soundbar.methods.count("remoteKeyControl"), 1)
        self.assertTrue(self.soundbar.mute)

    async def test_direct_rejection_never_enters_relative_fallback(self):
        async def respond(payload):
            if payload["method"] == "createAccessToken":
                return {"result": {"AccessToken": "test-token"}}
            return {"result": {"success": False}}

        self.soundbar.handler = respond
        client = LocalSoundbarRpcClient("192.0.2.26", self.soundbar.session)
        with self.assertRaises(LocalRpcCommandError):
            await client.set_volume(9)
        with self.assertRaises(LocalRpcCommandError):
            await client.set_mute(True)
        self.assertNotIn("remoteKeyControl", self.soundbar.methods)
        self.assertNotIn("setVolume", self.soundbar.methods)

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

    async def test_parse_error_on_read_recovers_expired_runtime_token(self):
        self.rpc._token = "expired-secret"
        original = self.soundbar.respond

        async def respond(payload):
            if payload.get("params", {}).get("AccessToken") == "expired-secret":
                return {"code": -32700, "message": "Parse error expired-secret"}
            return await original(payload)

        self.soundbar.handler = respond
        self.assertEqual(await self.rpc.volume(), 8)
        self.assertEqual(
            self.soundbar.methods, ["getVolume", "createAccessToken", "getVolume"]
        )

    async def test_repeated_parse_read_failure_has_one_refresh_budget(self):
        self.rpc._token = "expired-secret"
        original = self.soundbar.respond

        async def respond(payload):
            if payload["method"] == "createAccessToken":
                return await original(payload)
            return {"error": {"code": -32700, "message": "Parse error"}}

        self.soundbar.handler = respond
        with self.assertRaises(LocalRpcParseError):
            await self.rpc.power_state()
        self.assertEqual(
            self.soundbar.methods, ["powerControl", "createAccessToken", "powerControl"]
        )
        self.assertIsNone(self.rpc.token_length)

    async def test_parse_write_is_not_replayed_and_next_read_gets_fresh_token(self):
        self.rpc._token = "expired-secret"
        original = self.soundbar.respond

        async def respond(payload):
            if payload["method"] == "remoteKeyControl":
                return {"code": -32700, "message": "Parse error"}
            return await original(payload)

        self.soundbar.handler = respond
        with self.assertRaises(LocalRpcParseError):
            await self.rpc.remote_key("WOOFER_PLUS")
        self.assertEqual(self.soundbar.methods, ["remoteKeyControl"])
        self.assertEqual(await self.rpc.volume(), 8)
        self.assertEqual(
            self.soundbar.methods,
            ["remoteKeyControl", "createAccessToken", "getVolume"],
        )

    async def test_relative_auth_rejection_is_not_replayed(self):
        self.rpc._token = "expired-secret"

        async def respond(payload):
            return {"error": {"message": "invalid token expired-secret"}}

        self.soundbar.handler = respond
        with self.assertRaises(LocalRpcAuthError):
            await self.rpc.remote_key("WOOFER_PLUS")
        self.assertEqual(self.soundbar.methods, ["remoteKeyControl"])

    async def test_parse_error_does_not_retry_control_writes_or_unknown_getters(self):
        for method, params in (
            ("powerControl", {"power": "powerOn"}),
            ("inputSelectControl", {"inputSource": "BT"}),
            ("getUnverifiedMethod", {}),
        ):
            with self.subTest(method=method):
                self.rpc._token = "test-token"
                self.soundbar.requests.clear()

                async def respond(payload):
                    return {"code": -32700, "message": "Parse error"}

                self.soundbar.handler = respond
                with self.assertRaises(LocalRpcParseError):
                    await self.rpc.call(method, params)
                self.assertEqual(self.soundbar.methods, [method])

    async def test_power_on_waits_for_confirmed_on_readback(self):
        original = self.soundbar.respond
        states = iter(("powerOff", "powerOn"))

        async def respond(payload):
            if payload["method"] == "powerControl":
                if "power" in payload.get("params", {}):
                    return {"result": {"success": True}}
                return {"result": {"power": next(states)}}
            return await original(payload)

        self.soundbar.handler = respond
        with patch(
            "custom_components.samsung_soundbar.local_rpc.asyncio.sleep",
            new=AsyncMock(),
        ) as sleep:
            await self.rpc.power_on()
        self.assertEqual(self.soundbar.methods.count("powerControl"), 3)
        sleep.assert_awaited_once()

    async def test_power_on_ack_without_state_change_is_not_success(self):
        self.soundbar.power = "powerOff"
        with (
            patch(
                "custom_components.samsung_soundbar.local_rpc.asyncio.sleep",
                new=AsyncMock(),
            ),
            self.assertRaisesRegex(LocalRpcError, "not confirmed"),
        ):
            await self.rpc.power_on()
        writes = [p for p in self.soundbar.requests if "power" in p.get("params", {})]
        self.assertEqual(len(writes), 1)

    async def test_power_off_waits_for_confirmed_off_readback(self):
        self.soundbar.power = "powerOff"
        await self.rpc.power_off()
        self.assertEqual(self.soundbar.methods.count("powerControl"), 2)

    async def test_auth_refresh_then_write_timeout_remains_uncertain(self):
        original = self.soundbar.respond
        attempts = 0

        async def respond(payload):
            nonlocal attempts
            if payload["method"] == "volumeControl":
                attempts += 1
                if attempts == 1:
                    return {"error": {"message": "invalid token"}}
                raise TimeoutError()
            return await original(payload)

        self.soundbar.handler = respond
        client = LocalSoundbarRpcClient("192.0.2.26", self.soundbar.session)
        with self.assertRaises(LocalRpcCommandError):
            await client.set_volume(9)
        self.assertEqual(attempts, 2)

    async def test_ambiguous_direct_writes_never_fall_back_to_cloud(self):
        original = self.soundbar.respond
        cloud = LocalDevice("local-id")
        cloud.set_volume = AsyncMock()
        cloud.mute = AsyncMock()
        for method in ("volumeControl", "muteControl"):
            with self.subTest(method=method):
                self.soundbar.requests.clear()

                async def respond(payload, method=method):
                    if payload["method"] == method:
                        raise TimeoutError()
                    return await original(payload)

                self.soundbar.handler = respond
                client = LocalSoundbarRpcClient("192.0.2.26", self.soundbar.session)
                device = SoundbarDevice(
                    cloud,
                    MagicMock(),
                    100,
                    "Q800F",
                    control_mode="hybrid_local_smartthings",
                    local_rpc=client,
                )
                with self.assertRaises(HomeAssistantError):
                    if method == "volumeControl":
                        await device.set_volume(0.09)
                    else:
                        await device.mute_volume(True)
                self.assertEqual(self.soundbar.methods.count(method), 1)
                cloud.set_volume.assert_not_awaited()
                cloud.mute.assert_not_awaited()

    async def test_unconfirmed_power_keeps_lan_and_does_not_issue_cloud_write(self):
        cloud = LocalDevice("local-id")
        cloud.switch_on = AsyncMock()
        self.soundbar.power = "powerOff"
        device = SoundbarDevice(
            cloud,
            MagicMock(),
            100,
            "Q800F",
            control_mode="hybrid_local_smartthings",
            local_rpc=self.rpc,
        )
        with patch(
            "custom_components.samsung_soundbar.local_rpc.asyncio.sleep",
            new=AsyncMock(),
        ), self.assertRaises(HomeAssistantError):
            await device.switch_on()
        self.assertTrue(device.local_available)
        self.assertEqual(device.state, "off")
        cloud.switch_on.assert_not_awaited()

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

    async def test_failed_source_getter_keeps_other_snapshot_fields(self):
        original = self.soundbar.respond

        async def respond(payload):
            if payload["method"] == "inputSelectControl":
                return {"error": {"code": -32601, "message": "Method not found"}}
            return await original(payload)

        self.soundbar.handler = respond
        snapshot = await self.rpc.status()
        self.assertEqual(snapshot["power"], "powerOn")
        self.assertEqual(snapshot["volume"], 8)
        self.assertIsNone(snapshot["input_source"])
        self.assertIn("input_source", self.rpc.status_errors)
        self.soundbar.handler = original
        await self.rpc.status()
        self.assertEqual(self.rpc.status_errors, {})

    async def test_invalid_volume_is_a_field_error_not_a_transport_failure(self):
        self.soundbar.volume = "invalid"
        snapshot = await self.rpc.status()
        self.assertEqual(snapshot["power"], "powerOn")
        self.assertIsNone(snapshot["volume"])
        self.assertIn("volume", self.rpc.status_errors)

    async def test_transport_error_in_optional_getter_rejects_snapshot(self):
        original = self.soundbar.respond

        async def respond(payload):
            if payload["method"] == "getMute":
                raise ClientConnectionError("offline")
            return await original(payload)

        self.soundbar.handler = respond
        with self.assertRaises(LocalRpcError):
            await self.rpc.status()

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
