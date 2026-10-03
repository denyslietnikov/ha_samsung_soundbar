"""Cloud HTTP/OAuth failures keep their semantics and have bounded retries."""

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, MagicMock

from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    OAuth2TokenRequestError,
    OAuth2TokenRequestReauthError,
    OAuth2TokenRequestTransientError,
)
from pysmartthings import SmartThings
from pysmartthings.exceptions import (
    SmartThingsAuthenticationFailedError,
    SmartThingsForbiddenError,
    SmartThingsRateLimitError,
)

from custom_components.samsung_soundbar.api_extension.SoundbarDevice import (
    SoundbarDevice,
)
from custom_components.samsung_soundbar.auth import SmartThingsAuthProvider
from custom_components.samsung_soundbar.cloud_errors import (
    CloudAccessError,
    CloudRateLimitError,
    SmartThingsHttpSession,
    cloud_error_for_status,
    normalize_cloud_error,
)
from custom_components.samsung_soundbar.local_device import LocalDevice


class TestCloudStatus(TestCase):
    def test_access_quota_and_temporary_outage_are_not_auth(self):
        for status, expected in (
            (402, CloudAccessError),
            (403, CloudAccessError),
            (429, CloudRateLimitError),
            (503, ConfigEntryNotReady),
        ):
            with self.subTest(status=status):
                error = cloud_error_for_status(status)
                self.assertIsInstance(error, expected)
                self.assertNotIsInstance(error, ConfigEntryAuthFailed)
        self.assertIsNone(cloud_error_for_status(401))
        self.assertIsNone(cloud_error_for_status(422))
        self.assertIsNone(cloud_error_for_status(200))

    def test_retry_after_is_bounded_and_invalid_values_have_a_default(self):
        for value, expected in (
            ("120", 120),
            ("-1", 15),
            ("99999", 3600),
            ("bad", 60),
            (None, 60),
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    cloud_error_for_status(429, {"Retry-After": value}).retry_after,
                    expected,
                )
        self.assertIsInstance(
            normalize_cloud_error(SmartThingsForbiddenError("secret")), CloudAccessError
        )
        self.assertIsInstance(
            normalize_cloud_error(SmartThingsRateLimitError("secret")),
            CloudRateLimitError,
        )


class TestCloudRequests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.http = MagicMock()
        self.provider = SimpleNamespace(
            async_get_access_token=AsyncMock(return_value="test-token")
        )
        self.device = SoundbarDevice(
            LocalDevice("soundbar-id"),
            self.http,
            max_volume=100,
            device_name="Q800F",
            auth_provider=self.provider,
        )

    async def test_real_sdk_sees_http_failure_before_model_parsing(self):
        for status, expected in (
            (402, CloudAccessError),
            (403, CloudAccessError),
            (429, CloudRateLimitError),
            (503, ConfigEntryNotReady),
        ):
            with self.subTest(status=status):
                response = MagicMock(status=status, headers={"Retry-After": "120"})
                response.text = AsyncMock(return_value="secret response body")
                session = MagicMock(request=AsyncMock(return_value=response))
                api = SmartThings(session=SmartThingsHttpSession(session))
                api.authenticate("test-token")
                with self.assertRaises(expected) as caught:
                    await api.get_devices()
                response.release.assert_called_once()
                response.text.assert_not_awaited()
                self.assertNotIn("secret", str(caught.exception))
        response = MagicMock(status=200, headers={})
        response.text = AsyncMock(return_value='{"items": []}')
        api = SmartThings(
            session=SmartThingsHttpSession(
                MagicMock(request=AsyncMock(return_value=response))
            )
        )
        api.authenticate("test-token")
        self.assertEqual(await api.get_devices(), [])

    async def test_manual_get_and_post_retry_401_only_once(self):
        for verb in ("get", "post"):
            with self.subTest(verb=verb):
                self.provider.async_get_access_token.reset_mock()
                responses = [MagicMock(status=401, headers={}) for _ in range(2)]
                action = AsyncMock(side_effect=responses)
                setattr(self.http, verb, action)
                with self.assertRaises(ConfigEntryAuthFailed):
                    if verb == "get":
                        await self.device._SoundbarDevice__get_status_response(
                            "https://api.smartthings.com/v1/status"
                        )
                    else:
                        await self.device._SoundbarDevice__post_execute_command_raw(
                            ["/test"]
                        )
                self.assertEqual(action.await_count, 2)
                self.assertEqual(self.provider.async_get_access_token.await_count, 2)
                self.provider.async_get_access_token.assert_awaited_with(
                    force_refresh=True
                )
                for response in responses:
                    response.release.assert_called_once()

    async def test_manual_access_or_quota_does_not_refresh_oauth(self):
        for verb in ("get", "post"):
            for status, expected in (
                (403, CloudAccessError),
                (429, CloudRateLimitError),
            ):
                with self.subTest(verb=verb, status=status):
                    self.provider.async_get_access_token.reset_mock()
                    action = AsyncMock(
                        return_value=MagicMock(status=status, headers={})
                    )
                    setattr(self.http, verb, action)
                    with self.assertRaises(expected):
                        if verb == "get":
                            await self.device._SoundbarDevice__get_status_response(
                                "https://api.smartthings.com/v1/status"
                            )
                        else:
                            await self.device._SoundbarDevice__post_execute_command_raw(
                                ["/test"]
                            )
                    action.assert_awaited_once()
                    self.provider.async_get_access_token.assert_awaited_once_with(
                        force_refresh=False
                    )

    async def test_sdk_retry_normalizes_second_failure(self):
        for error, expected in (
            (SmartThingsForbiddenError("denied"), CloudAccessError),
            (SmartThingsRateLimitError("limit"), CloudRateLimitError),
            (SmartThingsAuthenticationFailedError("invalid"), ConfigEntryAuthFailed),
        ):
            with self.subTest(error=error):
                self.provider.async_get_access_token.reset_mock()
                action = AsyncMock(
                    side_effect=[SmartThingsAuthenticationFailedError("expired"), error]
                )
                with self.assertRaises(expected):
                    await self.device._SoundbarDevice__call_smartthings(action, "test")
                self.assertEqual(action.await_count, 2)
                self.provider.async_get_access_token.assert_awaited_once_with(
                    force_refresh=True
                )

    async def test_oauth_status_takes_precedence_over_helper_reauth_class(self):
        for status, exception_type, expected in (
            (401, OAuth2TokenRequestReauthError, ConfigEntryAuthFailed),
            (403, OAuth2TokenRequestReauthError, CloudAccessError),
            (402, OAuth2TokenRequestReauthError, CloudAccessError),
            (429, OAuth2TokenRequestTransientError, CloudRateLimitError),
            (503, OAuth2TokenRequestTransientError, ConfigEntryNotReady),
            (400, OAuth2TokenRequestError, ConfigEntryNotReady),
            (401, OAuth2TokenRequestError, ConfigEntryAuthFailed),
        ):
            with self.subTest(status=status):
                error = exception_type(
                    domain="samsung_soundbar", request_info=MagicMock(), status=status
                )
                oauth = MagicMock(async_ensure_token_valid=AsyncMock(side_effect=error))
                api = MagicMock()
                provider = SmartThingsAuthProvider(
                    MagicMock(),
                    SimpleNamespace(data={"token": {"access_token": "test-token"}}),
                    oauth,
                    api,
                )
                with self.assertRaises(expected):
                    await provider.async_get_access_token()
                api.authenticate.assert_not_called()
