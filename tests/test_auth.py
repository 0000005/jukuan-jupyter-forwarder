import unittest
from unittest.mock import AsyncMock, patch

import httpx

from src.auth import JQAuth


class _RaisingAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, *args, **kwargs):
        request = httpx.Request("GET", "https://www.joinquant.com/user/index/isLogin")
        raise httpx.ConnectError("boom", request=request)


class AuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_login_status_returns_none_on_network_error(self):
        auth = JQAuth()
        auth.cookies = {"session": "alive"}

        with patch("src.auth.httpx.AsyncClient", _RaisingAsyncClient):
            result = await auth.check_login_status()

        self.assertIsNone(result)

    async def test_get_valid_cookies_reuses_existing_cookie_when_status_unknown(self):
        auth = JQAuth()
        auth.cookies = {"session": "alive"}
        auth.check_login_status = AsyncMock(return_value=None)
        auth.login = AsyncMock(return_value=True)

        cookies = await auth.get_valid_cookies()

        self.assertEqual(cookies, {"session": "alive"})
        auth.login.assert_not_awaited()

    async def test_get_valid_cookies_relogin_when_session_invalid(self):
        auth = JQAuth()
        auth.cookies = {"session": "stale"}
        auth.check_login_status = AsyncMock(return_value=False)

        async def fake_login():
            auth.cookies = {"session": "fresh", "_xsrf": "token"}
            return True

        auth.login = AsyncMock(side_effect=fake_login)

        cookies = await auth.get_valid_cookies()

        self.assertEqual(cookies, {"session": "fresh", "_xsrf": "token"})
        auth.login.assert_awaited_once()

    async def test_get_request_cookies_returns_cached_cookie_without_login_check(self):
        auth = JQAuth()
        auth.cookies = {"session": "alive"}
        auth.check_login_status = AsyncMock(return_value=True)
        auth.login = AsyncMock(return_value=True)

        cookies = await auth.get_request_cookies()

        self.assertEqual(cookies, {"session": "alive"})
        auth.check_login_status.assert_not_awaited()
        auth.login.assert_not_awaited()

    async def test_refresh_cookies_after_auth_failure_relogin_when_session_invalid(self):
        auth = JQAuth()
        auth.cookies = {"session": "stale"}
        auth.check_login_status = AsyncMock(return_value=False)

        async def fake_login():
            auth.cookies = {"session": "fresh"}
            return True

        auth.login = AsyncMock(side_effect=fake_login)

        cookies = await auth.refresh_cookies_after_auth_failure()

        self.assertEqual(cookies, {"session": "fresh"})
        auth.check_login_status.assert_awaited_once()
        auth.login.assert_awaited_once()
