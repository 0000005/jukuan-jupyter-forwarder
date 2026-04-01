import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from src import proxy as proxy_module


class _FakeBackendResponse:
    def __init__(self, status_code=302, headers=None, body=b"redirect"):
        self.status_code = status_code
        self.headers = headers or {"location": "/tree", "content-type": "text/plain"}
        self._body = body
        self.closed = False

    async def aiter_raw(self):
        yield self._body

    async def aclose(self):
        self.closed = True


class _FakeAsyncClient:
    response = _FakeBackendResponse()
    events = []
    requests = []
    send_count = 0

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    def build_request(self, method, url, headers=None, content=None):
        request = {
            "method": method,
            "url": url,
            "headers": dict(headers or {}),
            "content": content,
        }
        _FakeAsyncClient.requests.append(request)
        return request

    async def send(self, request, stream=False):
        _FakeAsyncClient.send_count += 1
        if self.events:
            event = self.events.pop(0)
            if isinstance(event, Exception):
                raise event
            return event
        return self.response

    async def aclose(self):
        return None


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.cookie_mock = AsyncMock(return_value={"session": "alive", "_xsrf": "xsrf-token"})
        self.refresh_mock = AsyncMock(return_value={"session": "fresh", "_xsrf": "fresh-xsrf"})
        self.cookie_patcher = patch.object(proxy_module.auth_manager, "get_request_cookies", self.cookie_mock)
        self.refresh_patcher = patch.object(proxy_module.auth_manager, "refresh_cookies_after_auth_failure", self.refresh_mock)
        self.httpx_patcher = patch("src.proxy.httpx.AsyncClient", _FakeAsyncClient)
        self.cookie_patcher.start()
        self.refresh_patcher.start()
        self.httpx_patcher.start()
        self.client = TestClient(proxy_module.app)
        self.client.cookies.set(
            proxy_module.PROXY_AUTH_COOKIE_NAME,
            proxy_module._proxy_auth_cookie_value(),
        )

    def tearDown(self):
        self.client.close()
        self.cookie_patcher.stop()
        self.refresh_patcher.stop()
        self.httpx_patcher.stop()
        _FakeAsyncClient.response = _FakeBackendResponse()
        _FakeAsyncClient.events = []
        _FakeAsyncClient.requests = []
        _FakeAsyncClient.send_count = 0

    def test_http_proxy_preserves_backend_status_and_headers(self):
        response = self.client.get("/tree", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/tree")
        self.assertEqual(response.text, "redirect")
        self.assertEqual(
            _FakeAsyncClient.requests[-1]["headers"]["X-XSRFToken"],
            "xsrf-token",
        )
        self.refresh_mock.assert_not_awaited()

    def test_http_proxy_redirects_unauthenticated_html_request_to_login(self):
        unauthenticated_client = TestClient(proxy_module.app)
        try:
            response = unauthenticated_client.get("/tree", follow_redirects=False)
        finally:
            unauthenticated_client.close()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/login?next=%2Ftree")
        self.assertEqual(_FakeAsyncClient.send_count, 0)

    def test_http_proxy_accepts_token_query_and_sets_cookie(self):
        unauthenticated_client = TestClient(proxy_module.app)
        try:
            response = unauthenticated_client.get("/tree?token=wobeidaohao", follow_redirects=False)
        finally:
            unauthenticated_client.close()

        self.assertEqual(response.status_code, 302)
        self.assertIn(proxy_module.PROXY_AUTH_COOKIE_NAME, response.headers["set-cookie"])
        self.assertNotIn("token=wobeidaohao", _FakeAsyncClient.requests[-1]["url"])

    def test_http_proxy_returns_502_when_backend_request_fails(self):
        request = proxy_module.httpx.Request("GET", "https://www.joinquant.com/user/58026470838/tree")
        _FakeAsyncClient.events = [
            proxy_module.httpx.ConnectError("boom", request=request),
            proxy_module.httpx.ConnectError("boom", request=request),
        ]

        response = self.client.get("/tree")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.text, "Proxy Internal Error")
        self.assertEqual(_FakeAsyncClient.send_count, 2)

    def test_http_proxy_retries_once_when_backend_redirects_to_login(self):
        _FakeAsyncClient.events = [
            _FakeBackendResponse(status_code=302, headers={"location": "/user/58026470838/login"}),
            _FakeBackendResponse(status_code=200, headers={"content-type": "text/plain"}, body=b"ok"),
        ]

        response = self.client.get("/tree", follow_redirects=False)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "ok")
        self.refresh_mock.assert_awaited_once()
        self.assertEqual(len(_FakeAsyncClient.requests), 2)
        self.assertEqual(_FakeAsyncClient.requests[0]["headers"]["X-XSRFToken"], "xsrf-token")
        self.assertEqual(_FakeAsyncClient.requests[1]["headers"]["X-XSRFToken"], "fresh-xsrf")

    def test_http_proxy_retries_connect_error_once_then_succeeds(self):
        request = proxy_module.httpx.Request("GET", "https://www.joinquant.com/user/58026470838/tree")
        _FakeAsyncClient.events = [
            proxy_module.httpx.ConnectError("boom", request=request),
            _FakeBackendResponse(status_code=200, headers={"content-type": "text/plain"}, body=b"ok"),
        ]

        response = self.client.get("/tree")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "ok")
        self.assertEqual(_FakeAsyncClient.send_count, 2)
        self.refresh_mock.assert_not_awaited()

    def test_http_proxy_treats_oauth_authorize_redirect_as_auth_failure(self):
        _FakeAsyncClient.events = [
            _FakeBackendResponse(status_code=302, headers={"location": "/hub/api/oauth2/authorize?client_id=user-58026470838"}),
            _FakeBackendResponse(status_code=200, headers={"content-type": "text/plain"}, body=b"ok"),
        ]

        response = self.client.get("/tree", follow_redirects=False)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "ok")
        self.refresh_mock.assert_awaited_once()

    def test_build_filtered_query_string_removes_proxy_token(self):
        query = proxy_module.httpx.QueryParams([
            ("token", "wobeidaohao"),
            ("session_id", "session-1"),
            ("content", "0"),
        ])

        result = proxy_module._build_filtered_query_string(query)

        self.assertEqual(result, "session_id=session-1&content=0")

    def test_build_target_ws_url_keeps_query_string(self):
        url = proxy_module._build_target_ws_url(
            "/api/kernels/kernel-1/channels",
            "session_id=session-1",
        )

        self.assertEqual(
            url,
            "wss://www.joinquant.com/user/58026470838/api/kernels/kernel-1/channels?session_id=session-1",
        )

    def test_build_websocket_connect_kwargs_supports_new_signature(self):
        def fake_connect(uri, *, additional_headers=None, proxy=True, **kwargs):
            return None

        with patch("src.proxy.websockets.connect", fake_connect):
            kwargs = proxy_module._build_websocket_connect_kwargs({"Cookie": "a=b"})

        self.assertEqual(kwargs["additional_headers"], {"Cookie": "a=b"})
        self.assertIsNone(kwargs["proxy"])
        self.assertNotIn("extra_headers", kwargs)

    def test_build_websocket_connect_kwargs_supports_old_signature(self):
        def fake_connect(uri, *, extra_headers=None, **kwargs):
            return None

        with patch("src.proxy.websockets.connect", fake_connect):
            kwargs = proxy_module._build_websocket_connect_kwargs({"Cookie": "a=b"})

        self.assertEqual(kwargs["extra_headers"], {"Cookie": "a=b"})
        self.assertNotIn("additional_headers", kwargs)
