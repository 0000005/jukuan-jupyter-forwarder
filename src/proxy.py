import httpx
import websockets
import asyncio
import logging
import traceback
import inspect
import hashlib
import secrets
from html import escape
from contextlib import asynccontextmanager
from urllib.parse import parse_qs
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from starlette.background import BackgroundTask
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

try:
    from .auth import JQAuth
    from .config import JQ_BASE_URL, JQ_USER_ID, PROXY_HOST, PROXY_PORT, PROXY_TOKEN
except ImportError:
    from auth import JQAuth
    from config import JQ_BASE_URL, JQ_USER_ID, PROXY_HOST, PROXY_PORT, PROXY_TOKEN

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jq_proxy")

BACKEND_TIMEOUT = 60.0
BACKEND_CONNECT_MAX_ATTEMPTS = 2
BACKEND_CONNECT_RETRY_DELAY = 0.2
PROXY_AUTH_COOKIE_NAME = "_jq_proxy_auth"
PROXY_AUTH_COOKIE_MAX_AGE = 7 * 24 * 60 * 60


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

# 聚宽的 Jupyter 根路径
JQ_JUPYTER_ROOT = f"/user/{JQ_USER_ID}"

def fix_url(path: str):
    """将本地请求路径映射到聚宽路径"""
    if path.startswith(JQ_JUPYTER_ROOT):
        return f"{JQ_BASE_URL}{path}"
    return f"{JQ_BASE_URL}{JQ_JUPYTER_ROOT}{path}"


def _build_target_ws_url(path: str, query: str = "") -> str:
    target_ws_url = fix_url(path).replace("https://", "wss://").replace("http://", "ws://")
    if query:
        target_ws_url = f"{target_ws_url}?{query}"
    return target_ws_url


def _proxy_auth_cookie_value() -> str:
    raw = f"jq_proxy_auth:{PROXY_TOKEN}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _is_valid_proxy_token(token: str | None) -> bool:
    if not PROXY_TOKEN:
        return True
    if not token:
        return False
    return secrets.compare_digest(token, PROXY_TOKEN)


def _is_valid_proxy_auth_cookie(cookie_value: str | None) -> bool:
    if not PROXY_TOKEN:
        return True
    if not cookie_value:
        return False
    return secrets.compare_digest(cookie_value, _proxy_auth_cookie_value())


def _extract_proxy_token_from_authorization(authorization: str | None) -> str | None:
    if not authorization:
        return None

    parts = authorization.strip().split(None, 1)
    if len(parts) != 2:
        return None

    scheme, token = parts
    if scheme.lower() not in {"token", "bearer"}:
        return None
    return token.strip()


def _filter_proxy_query_items(query_params) -> list[tuple[str, str]]:
    return [(key, value) for key, value in query_params.multi_items() if key != "token"]


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
    if request.method != "GET":
        return False
    if path.startswith("/api"):
        return False
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return False
    return True


def _check_http_proxy_auth(request: Request) -> tuple[bool, bool]:
    if not PROXY_TOKEN:
        return True, False

    query_token = request.query_params.get("token")
    if _is_valid_proxy_token(query_token):
        return True, True

    if _is_valid_proxy_auth_cookie(request.cookies.get(PROXY_AUTH_COOKIE_NAME)):
        return True, False

    header_token = _extract_proxy_token_from_authorization(request.headers.get("authorization"))
    if _is_valid_proxy_token(header_token):
        return True, False

    return False, False


def _check_websocket_proxy_auth(websocket: WebSocket) -> bool:
    if not PROXY_TOKEN:
        return True

    query_token = websocket.query_params.get("token")
    if _is_valid_proxy_token(query_token):
        return True

    if _is_valid_proxy_auth_cookie(websocket.cookies.get(PROXY_AUTH_COOKIE_NAME)):
        return True

    header_token = _extract_proxy_token_from_authorization(websocket.headers.get("authorization"))
    if _is_valid_proxy_token(header_token):
        return True

    return False


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
        <p style="color:#555;margin-top:0;">请输入访问 token。</p>
        <form method="post" action="/login">
          <input type="hidden" name="next" value="{escape(next_url, quote=True)}">
          <label for="password">Token</label>
          <input id="password" name="password" type="password" autofocus
                 style="display:block;width:100%;box-sizing:border-box;margin-top:8px;padding:10px;">
          <button type="submit" style="margin-top:16px;padding:10px 16px;">Log in</button>
        </form>
        <p style="margin-top:16px;color:#666;">也可以直接使用 URL 参数：<code>?token=...</code></p>
        {error_block}
      </body>
    </html>
    """
    return HTMLResponse(html)


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


async def _send_backend_request(method: str, path: str, target_url: str, headers: dict, request_body: bytes | None, cookies: dict):
    client = await _get_backend_client()
    _apply_cookie_header(headers, cookies)

    for attempt in range(1, BACKEND_CONNECT_MAX_ATTEMPTS + 1):
        try:
            backend_request = client.build_request(
                method,
                target_url,
                headers=dict(headers),
                content=request_body,
            )
            backend_response = await client.send(backend_request, stream=True)
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
    is_authenticated, should_set_cookie = _check_http_proxy_auth(request)
    if not is_authenticated:
        return RedirectResponse(url=_build_login_url("/tree", request.query_params), status_code=302)

    response = RedirectResponse(url=_build_local_url("/tree", request.query_params), status_code=302)
    if should_set_cookie:
        _set_proxy_auth_cookie(response)
    return response


@app.get("/login")
async def login_page(request: Request):
    next_url = request.query_params.get("next", "/tree")
    is_authenticated, should_set_cookie = _check_http_proxy_auth(request)
    if is_authenticated:
        response = RedirectResponse(url=next_url, status_code=302)
        if should_set_cookie:
            _set_proxy_auth_cookie(response)
        return response
    return _render_login_page(next_url)


@app.post("/login")
async def login_submit(request: Request):
    form = parse_qs((await request.body()).decode("utf-8"))
    token = form.get("password", [""])[0].strip()
    next_url = form.get("next", ["/tree"])[0].strip() or "/tree"

    if not _is_valid_proxy_token(token):
        return _render_login_page(next_url, "Token 无效")

    response = RedirectResponse(url=next_url, status_code=302)
    _set_proxy_auth_cookie(response)
    return response

@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def jupyter_http_proxy(request: Request, full_path: str):
    path = f"/{full_path}"
    query = _build_filtered_query_string(request.query_params)
    method = request.method

    is_authenticated, should_set_cookie = _check_http_proxy_auth(request)
    if not is_authenticated:
        if _should_redirect_to_login(path, request):
            return RedirectResponse(url=_build_login_url(path, request.query_params), status_code=302)
        return Response(content="Forbidden", status_code=403)
    
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
    if not _check_websocket_proxy_auth(websocket):
        await websocket.close(code=1008)
        return

    ws_path = f"/{full_path}"
    query = _build_filtered_query_string(websocket.query_params)
    target_ws_url = _build_target_ws_url(ws_path, query)
    
    cookies = await auth_manager.get_request_cookies()
    if not cookies:
        await websocket.accept()
        await websocket.close(code=4001)
        return

    await websocket.accept()
    cookie_str = "; ".join([f"{k}={v}" for k, v in cookies.items()])
    headers = {
        "Cookie": cookie_str,
        "Origin": JQ_BASE_URL,
    }
    connect_kwargs = _build_websocket_connect_kwargs(headers)

    logger.info(f"Connecting to remote WebSocket: {target_ws_url}")
    
    try:
        async with websockets.connect(target_ws_url, **connect_kwargs) as target_ws:
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
