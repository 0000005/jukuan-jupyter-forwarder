import httpx
import websockets
import asyncio
import logging
import traceback
import inspect
import hashlib
import secrets
import base64
from html import escape
from contextlib import asynccontextmanager
from urllib.parse import parse_qs
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from starlette.background import BackgroundTask
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

try:
    from .auth import JQAuth
    from .config import (
        JQ_BASE_URL,
        JQ_USER_ID,
        PROXY_AUTH_MODE,
        PROXY_HOST,
        PROXY_PASSWORD,
        PROXY_PORT,
        PROXY_TOKEN,
    )
except ImportError:
    from auth import JQAuth
    from config import (
        JQ_BASE_URL,
        JQ_USER_ID,
        PROXY_AUTH_MODE,
        PROXY_HOST,
        PROXY_PASSWORD,
        PROXY_PORT,
        PROXY_TOKEN,
    )

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jq_proxy")

BACKEND_TIMEOUT = 60.0
BACKEND_CONNECT_MAX_ATTEMPTS = 2
BACKEND_CONNECT_RETRY_DELAY = 0.2
PROXY_AUTH_COOKIE_NAME = "_jq_proxy_auth"
PROXY_AUTH_COOKIE_MAX_AGE = 7 * 24 * 60 * 60
SUPPORTED_PROXY_AUTH_MODES = {"token", "password", "both"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.backend_client = httpx.AsyncClient(
        follow_redirects=False,
        trust_env=False,
        timeout=BACKEND_TIMEOUT,
    )
    try:
        yield
    finally:
        client = getattr(app.state, "backend_client", None)
        if client is not None:
            await client.aclose()
            app.state.backend_client = None


app = FastAPI(lifespan=lifespan)
auth_manager = JQAuth()

if PROXY_AUTH_MODE not in SUPPORTED_PROXY_AUTH_MODES:
    raise ValueError(
        f"Unsupported PROXY_AUTH_MODE={PROXY_AUTH_MODE!r}, "
        f"expected one of {sorted(SUPPORTED_PROXY_AUTH_MODES)}"
    )

# 聚宽的 Jupyter 根路径
JQ_JUPYTER_ROOT = f"/user/{JQ_USER_ID}"

def fix_url(path: str):
    """将本地请求路径映射到聚宽路径"""
    if path.startswith(JQ_JUPYTER_ROOT):
        return f"{JQ_BASE_URL}{path}"
    if path.startswith("/hub/"):
        return f"{JQ_BASE_URL}{path}"
    if path.startswith("/user/"):
        return f"{JQ_BASE_URL}{path}"
    return f"{JQ_BASE_URL}{JQ_JUPYTER_ROOT}{path}"


def _build_target_ws_url(path: str, query: str = "") -> str:
    target_ws_url = fix_url(path).replace("https://", "wss://").replace("http://", "ws://")
    if query:
        target_ws_url = f"{target_ws_url}?{query}"
    return target_ws_url


def _proxy_auth_cookie_value() -> str:
    raw = f"jq_proxy_auth:{PROXY_AUTH_MODE}:{_get_proxy_secret()}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _get_proxy_secret() -> str:
    if PROXY_AUTH_MODE == "password":
        return PROXY_PASSWORD
    if PROXY_AUTH_MODE == "both":
        return f"{PROXY_TOKEN}\0{PROXY_PASSWORD}"
    return PROXY_TOKEN


def _proxy_auth_enabled() -> bool:
    if PROXY_AUTH_MODE == "both":
        return bool(PROXY_TOKEN or PROXY_PASSWORD)
    return bool(_get_proxy_secret())


def _is_valid_proxy_token(value: str | None) -> bool:
    if not PROXY_TOKEN:
        return True
    if not value:
        return False
    return secrets.compare_digest(value, PROXY_TOKEN)


def _is_valid_proxy_password(value: str | None) -> bool:
    if not PROXY_PASSWORD:
        return True
    if not value:
        return False
    return secrets.compare_digest(value, PROXY_PASSWORD)


def _is_valid_proxy_credential(value: str | None, *, kind: str | None = None) -> bool:
    if PROXY_AUTH_MODE == "token":
        return _is_valid_proxy_token(value)
    if PROXY_AUTH_MODE == "password":
        return _is_valid_proxy_password(value)
    if kind == "token":
        return _is_valid_proxy_token(value)
    if kind == "password":
        return _is_valid_proxy_password(value)
    return _is_valid_proxy_token(value) or _is_valid_proxy_password(value)


def _is_valid_proxy_auth_cookie(cookie_value: str | None) -> bool:
    if not _proxy_auth_enabled():
        return True
    if not cookie_value:
        return False
    return secrets.compare_digest(cookie_value, _proxy_auth_cookie_value())


def _extract_proxy_credential_from_authorization(authorization: str | None) -> str | None:
    if not authorization:
        return None

    parts = authorization.strip().split(None, 1)
    if len(parts) != 2:
        return None

    scheme, value = parts
    scheme = scheme.lower()

    if scheme in {"token", "bearer"}:
        return value.strip()

    if scheme != "basic":
        return None

    try:
        decoded = base64.b64decode(value.strip()).decode("utf-8")
    except Exception:
        return None

    _, _, password = decoded.partition(":")
    if not password:
        return None
    return password


def _authorization_credential_kind(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme = authorization.strip().split(None, 1)[0].lower()
    if scheme == "basic":
        return "password"
    if scheme in {"token", "bearer"}:
        return "token"
    return None


def _client_debug_context(headers, query_params, cookies) -> str:
    authorization = headers.get("authorization")
    auth_kind = _authorization_credential_kind(authorization)
    user_agent = headers.get("user-agent", "")
    return (
        f"auth_mode={PROXY_AUTH_MODE} "
        f"has_auth_header={bool(authorization)} "
        f"auth_kind={auth_kind or 'none'} "
        f"has_token_query={'token' in query_params} "
        f"has_password_query={'password' in query_params} "
        f"has_proxy_cookie={PROXY_AUTH_COOKIE_NAME in cookies} "
        f"user_agent={user_agent!r}"
    )


def _log_http_proxy_auth_failure(request: Request, path: str):
    logger.warning(
        "HTTP proxy auth failed: "
        f"method={request.method} path={path} "
        f"{_client_debug_context(request.headers, request.query_params, request.cookies)}"
    )


def _log_http_proxy_auth_success(request: Request, path: str, source: str, should_set_cookie: bool):
    logger.info(
        "HTTP proxy auth success: "
        f"method={request.method} path={path} source={source} set_cookie={should_set_cookie} "
        f"{_client_debug_context(request.headers, request.query_params, request.cookies)}"
    )


def _log_websocket_proxy_auth_failure(websocket: WebSocket, path: str):
    logger.warning(
        "WebSocket proxy auth failed: "
        f"path={path} "
        f"{_client_debug_context(websocket.headers, websocket.query_params, websocket.cookies)}"
    )


def _log_websocket_proxy_auth_success(websocket: WebSocket, path: str, source: str):
    logger.info(
        "WebSocket proxy auth success: "
        f"path={path} source={source} "
        f"{_client_debug_context(websocket.headers, websocket.query_params, websocket.cookies)}"
    )


def _filter_proxy_query_items(query_params) -> list[tuple[str, str]]:
    return [
        (key, value)
        for key, value in query_params.multi_items()
        if key not in {"token", "password"}
    ]


def _build_filtered_query_string(query_params) -> str:
    items = _filter_proxy_query_items(query_params)
    return str(httpx.QueryParams(items))


def _build_local_url(path: str, query_params) -> str:
    query = _build_filtered_query_string(query_params)
    if query:
        return f"{path}?{query}"
    return path


def _build_login_url(path: str, query_params) -> str:
    next_url = _build_local_url(path, query_params)
    return f"/login?{httpx.QueryParams({'next': next_url})}"


def _should_redirect_to_login(path: str, request: Request) -> bool:
    if PROXY_AUTH_MODE in {"password", "both"}:
        return False
    if request.method != "GET":
        return False
    if path.startswith("/api"):
        return False
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return False
    return True


def _check_http_proxy_auth(request: Request, path: str = "") -> tuple[bool, bool, str]:
    if not _proxy_auth_enabled():
        return True, False, "disabled"

    query_token = request.query_params.get("token")
    if _is_valid_proxy_credential(query_token, kind="token"):
        return True, True, "query_token"

    query_password = request.query_params.get("password")
    if _is_valid_proxy_credential(query_password, kind="password"):
        return True, True, "query_password"

    if _is_valid_proxy_auth_cookie(request.cookies.get(PROXY_AUTH_COOKIE_NAME)):
        return True, False, "proxy_cookie"

    authorization = request.headers.get("authorization")
    header_value = _extract_proxy_credential_from_authorization(authorization)
    header_kind = _authorization_credential_kind(authorization)
    if _is_valid_proxy_credential(header_value, kind=header_kind):
        return True, False, f"authorization_{header_kind or 'unknown'}"

    _log_http_proxy_auth_failure(request, path or request.url.path)
    return False, False, "none"


def _check_websocket_proxy_auth(websocket: WebSocket, path: str = "") -> tuple[bool, str]:
    if not _proxy_auth_enabled():
        return True, "disabled"

    query_token = websocket.query_params.get("token")
    if _is_valid_proxy_credential(query_token, kind="token"):
        return True, "query_token"

    query_password = websocket.query_params.get("password")
    if _is_valid_proxy_credential(query_password, kind="password"):
        return True, "query_password"

    if _is_valid_proxy_auth_cookie(websocket.cookies.get(PROXY_AUTH_COOKIE_NAME)):
        return True, "proxy_cookie"

    authorization = websocket.headers.get("authorization")
    header_value = _extract_proxy_credential_from_authorization(authorization)
    header_kind = _authorization_credential_kind(authorization)
    if _is_valid_proxy_credential(header_value, kind=header_kind):
        return True, f"authorization_{header_kind or 'unknown'}"

    _log_websocket_proxy_auth_failure(websocket, path or websocket.url.path)
    return False, "none"


def _set_proxy_auth_cookie(response: Response):
    response.set_cookie(
        key=PROXY_AUTH_COOKIE_NAME,
        value=_proxy_auth_cookie_value(),
        max_age=PROXY_AUTH_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _render_login_page(next_url: str, error_message: str = "") -> HTMLResponse:
    if PROXY_AUTH_MODE == "password":
        credential_label = "Password"
        credential_hint = "访问密码"
        query_example = "?password=..."
    elif PROXY_AUTH_MODE == "both":
        credential_label = "Password / Token"
        credential_hint = "访问密码或 Token"
        query_example = "?password=... 或 ?token=..."
    else:
        credential_label = "Token"
        credential_hint = "访问 Token"
        query_example = "?token=..."
    error_block = ""
    if error_message:
        error_block = f"<p style='color:#b42318;margin:12px 0 0'>{escape(error_message)}</p>"

    html = f"""
    <!doctype html>
    <html lang="zh-CN">
      <head>
        <meta charset="utf-8">
        <title>Jupyter Login</title>
      </head>
      <body style="font-family:Arial,sans-serif;max-width:420px;margin:64px auto;padding:0 16px;">
        <h2 style="margin-bottom:8px;">Jupyter Login</h2>
        <p style="color:#555;margin-top:0;">请输入{credential_hint}。</p>
        <form method="post" action="/login">
          <input type="hidden" name="next" value="{escape(next_url, quote=True)}">
          <label for="password">{credential_label}</label>
          <input id="password" name="password" type="password" autofocus
                 style="display:block;width:100%;box-sizing:border-box;margin-top:8px;padding:10px;">
          <button type="submit" style="margin-top:16px;padding:10px 16px;">Log in</button>
        </form>
        <p style="margin-top:16px;color:#666;">也可以直接使用 URL 参数：<code>{query_example}</code></p>
        {error_block}
      </body>
    </html>
    """
    return HTMLResponse(html)


def _build_proxy_auth_required_response() -> Response:
    headers = {}
    if PROXY_AUTH_MODE in {"password", "both"} and PROXY_PASSWORD:
        headers["WWW-Authenticate"] = 'Basic realm="Jupyter Proxy", charset="UTF-8"'
        return Response(content="Unauthorized", status_code=401, headers=headers)
    return Response(content="Forbidden", status_code=403)


def _apply_xsrf_header(headers: dict, cookies: dict):
    xsrf = cookies.get("_xsrf")
    if xsrf:
        headers["X-XSRFToken"] = xsrf
    else:
        headers.pop("X-XSRFToken", None)


def _apply_cookie_header(headers: dict, cookies: dict):
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    else:
        headers.pop("Cookie", None)


def _response_requires_reauth(response: httpx.Response) -> bool:
    if response.status_code in (401, 403):
        return True

    if 300 <= response.status_code < 400:
        location = response.headers.get("location", "").lower()
        return "/login" in location or "/hub/api/oauth2/authorize" in location

    return False


async def _get_backend_client() -> httpx.AsyncClient:
    client = getattr(app.state, "backend_client", None)
    if client is None:
        client = httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            timeout=BACKEND_TIMEOUT,
        )
        app.state.backend_client = client
    return client


def _format_http_error_details(
    method: str,
    path: str,
    target_url: str,
    attempt: int,
    error: Exception,
    cookies: dict,
) -> str:
    cause = repr(error.__cause__) if error.__cause__ else "None"
    return (
        f"{method} {path} -> {target_url} | "
        f"attempt={attempt}/{BACKEND_CONNECT_MAX_ATTEMPTS} | "
        f"error_type={type(error).__name__} | "
        f"error_repr={error!r} | "
        f"cause={cause} | "
        f"has_xsrf={bool(cookies.get('_xsrf'))}"
    )


def _build_websocket_connect_kwargs(headers: dict) -> dict:
    signature = inspect.signature(websockets.connect)
    kwargs = {}

    if "additional_headers" in signature.parameters:
        kwargs["additional_headers"] = headers
    else:
        kwargs["extra_headers"] = headers

    if "proxy" in signature.parameters:
        kwargs["proxy"] = None

    return kwargs


def _parse_websocket_subprotocols(header_value: str | None) -> list[str]:
    if not header_value:
        return []
    return [item.strip() for item in header_value.split(",") if item.strip()]


async def _send_backend_request(method: str, path: str, target_url: str, headers: dict, request_body: bytes | None, cookies: dict):
    client = await _get_backend_client()
    _apply_cookie_header(headers, cookies)
    logger.info(
        "Forwarding request to backend: "
        f"method={method} path={path} target={target_url} "
        f"has_cookie_header={bool(headers.get('Cookie'))} "
        f"has_xsrf_header={bool(headers.get('X-XSRFToken'))}"
    )

    for attempt in range(1, BACKEND_CONNECT_MAX_ATTEMPTS + 1):
        try:
            backend_request = client.build_request(
                method,
                target_url,
                headers=dict(headers),
                content=request_body,
            )
            backend_response = await client.send(backend_request, stream=True)
            logger.info(
                "Backend response received: "
                f"method={method} path={path} target={target_url} "
                f"status={backend_response.status_code} "
                f"location={backend_response.headers.get('location', '')!r}"
            )
            return backend_response
        except httpx.ConnectError as e:
            detail = _format_http_error_details(method, path, target_url, attempt, e, cookies)
            if attempt < BACKEND_CONNECT_MAX_ATTEMPTS:
                logger.warning(f"HTTP Proxy connect retry: {detail}")
                await asyncio.sleep(BACKEND_CONNECT_RETRY_DELAY)
                continue
            logger.error(
                f"HTTP Proxy connect failed: {detail}\n"
                f"{traceback.format_exc()}"
            )
            return None
        except httpx.HTTPError as e:
            detail = _format_http_error_details(method, path, target_url, attempt, e, cookies)
            logger.error(
                f"HTTP Proxy request failed: {detail}\n"
                f"{traceback.format_exc()}"
            )
            return None
    return None


@app.get("/")
async def root_redirect(request: Request):
    """根路径重定向到 /tree"""
    is_authenticated, should_set_cookie, auth_source = _check_http_proxy_auth(request, "/")
    if not is_authenticated:
        if PROXY_AUTH_MODE in {"password", "both"}:
            return _build_proxy_auth_required_response()
        return RedirectResponse(url=_build_login_url("/tree", request.query_params), status_code=302)

    _log_http_proxy_auth_success(request, "/", auth_source, should_set_cookie)
    response = RedirectResponse(url=_build_local_url("/tree", request.query_params), status_code=302)
    if should_set_cookie:
        _set_proxy_auth_cookie(response)
    return response


@app.get("/login")
async def login_page(request: Request):
    next_url = request.query_params.get("next", "/tree")
    is_authenticated, should_set_cookie, auth_source = _check_http_proxy_auth(request, "/login")
    if is_authenticated:
        _log_http_proxy_auth_success(request, "/login", auth_source, should_set_cookie)
        response = RedirectResponse(url=next_url, status_code=302)
        if should_set_cookie:
            _set_proxy_auth_cookie(response)
        return response
    return _render_login_page(next_url)


@app.post("/login")
async def login_submit(request: Request):
    form = parse_qs((await request.body()).decode("utf-8"))
    password = form.get("password", [""])[0].strip()
    next_url = form.get("next", ["/tree"])[0].strip() or "/tree"

    if not _is_valid_proxy_credential(password):
        if PROXY_AUTH_MODE == "password":
            error_message = "Password 无效"
        elif PROXY_AUTH_MODE == "both":
            error_message = "Password 或 Token 无效"
        else:
            error_message = "Token 无效"
        return _render_login_page(next_url, error_message)

    response = RedirectResponse(url=next_url, status_code=302)
    _set_proxy_auth_cookie(response)
    return response

@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def jupyter_http_proxy(request: Request, full_path: str):
    path = f"/{full_path}"
    query = _build_filtered_query_string(request.query_params)
    method = request.method

    is_authenticated, should_set_cookie, auth_source = _check_http_proxy_auth(request, path)
    if not is_authenticated:
        if _should_redirect_to_login(path, request):
            return RedirectResponse(url=_build_login_url(path, request.query_params), status_code=302)
        return _build_proxy_auth_required_response()
    _log_http_proxy_auth_success(request, path, auth_source, should_set_cookie)
    
    # 默认直接复用当前 Cookie，只有后端要求登录时再校验/重登
    cookies = await auth_manager.get_request_cookies()
    if not cookies:
        return Response(content="JoinQuant Authentication Failed", status_code=401)

    target_url = fix_url(path)
    if query:
        target_url += f"?{query}"
    
    # 构建请求头
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.update({
        "Referer": f"{JQ_BASE_URL}{JQ_JUPYTER_ROOT}/tree",
        "Origin": JQ_BASE_URL,
    })
    
    _apply_xsrf_header(headers, cookies)

    # 只在有内容的正文请求时读取 Body
    request_body = None
    if method not in ("GET", "HEAD", "OPTIONS"):
        try:
            request_body = await request.body()
        except Exception:
            logger.warning(f"Client disconnected during body read for {method} {path}")
            return Response(status_code=499)

    backend_response = await _send_backend_request(
        method,
        path,
        target_url,
        headers,
        request_body,
        cookies,
    )
    if not backend_response:
        return Response(content="Proxy Internal Error", status_code=502)

    if _response_requires_reauth(backend_response):
        logger.info(f"后端响应要求重新登录，准备重试: {method} {path}")
        await _close_backend_response(backend_response)

        refreshed_cookies = await auth_manager.refresh_cookies_after_auth_failure()
        if not refreshed_cookies:
            return Response(content="JoinQuant Authentication Failed", status_code=401)

        _apply_xsrf_header(headers, refreshed_cookies)
        backend_response = await _send_backend_request(
            method,
            path,
            target_url,
            headers,
            request_body,
            refreshed_cookies,
        )
        if not backend_response:
            return Response(content="Proxy Internal Error", status_code=502)

    exclude_headers = {"content-encoding", "transfer-encoding", "connection", "content-length"}
    resp_headers = {
        k: v for k, v in backend_response.headers.items()
        if k.lower() not in exclude_headers
    }
    response = StreamingResponse(
        backend_response.aiter_raw(),
        status_code=backend_response.status_code,
        headers=resp_headers,
        background=BackgroundTask(_close_backend_response, backend_response),
    )
    if should_set_cookie:
        _set_proxy_auth_cookie(response)
    return response


async def _close_backend_response(response: httpx.Response):
    await response.aclose()

@app.websocket("/{full_path:path}")
async def websocket_proxy(websocket: WebSocket, full_path: str):
    ws_path = f"/{full_path}"
    is_authenticated, auth_source = _check_websocket_proxy_auth(websocket, ws_path)
    if not is_authenticated:
        await websocket.close(code=1008)
        return
    _log_websocket_proxy_auth_success(websocket, ws_path, auth_source)
    query = _build_filtered_query_string(websocket.query_params)
    target_ws_url = _build_target_ws_url(ws_path, query)
    
    cookies = await auth_manager.get_request_cookies()
    if not cookies:
        await websocket.accept()
        await websocket.close(code=4001)
        return

    cookie_str = "; ".join([f"{k}={v}" for k, v in cookies.items()])
    headers = {
        "Cookie": cookie_str,
        "Origin": JQ_BASE_URL,
    }
    requested_subprotocols = _parse_websocket_subprotocols(
        websocket.headers.get("sec-websocket-protocol")
    )
    connect_kwargs = _build_websocket_connect_kwargs(headers)
    if requested_subprotocols:
        connect_kwargs["subprotocols"] = requested_subprotocols

    logger.info(
        "Connecting to remote WebSocket: "
        f"path={ws_path} target={target_ws_url} has_cookie_header={bool(cookie_str)} "
        f"requested_subprotocols={requested_subprotocols}"
    )
    
    try:
        async with websockets.connect(target_ws_url, **connect_kwargs) as target_ws:
            logger.info(
                "Remote WebSocket connected: "
                f"path={ws_path} target={target_ws_url} negotiated_subprotocol={target_ws.subprotocol!r}"
            )
            await websocket.accept(subprotocol=target_ws.subprotocol)

            async def forward_to_remote():
                try:
                    while True:
                        data = await websocket.receive_text()
                        await target_ws.send(data)
                except Exception:
                    pass

            async def forward_to_local():
                try:
                    while True:
                        data = await target_ws.recv()
                        await websocket.send_text(data)
                except Exception:
                    pass

            await asyncio.gather(forward_to_remote(), forward_to_local())
            
    except Exception as e:
        logger.error(
            f"WebSocket Proxy Error: "
            f"path={ws_path} target={target_ws_url} "
            f"error_type={type(e).__name__} error_repr={e!r}\n"
            f"{traceback.format_exc()}"
        )
        try:
            await websocket.close(code=1011)
        except:
            pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=PROXY_HOST, port=PROXY_PORT)
