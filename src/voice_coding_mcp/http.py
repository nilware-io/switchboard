"""The HTTP app: Host/Origin checks, request log, OAuth routes, /mcp and /healthz.

Starlette app; the MCP endpoint is the SDK's `StreamableHTTPSessionManager(stateless=True,
json_response=True)` behind `BearerMiddleware`, and the app lifespan runs `session_manager.run()`.
Requests get `200 application/json` bodies, no SSE (friendlier to proxies). Host/Origin checks
stay ours (the SDK's DNS-rebinding check is off with `security_settings=None`).

Routing is our own rather than Starlette's:

- paths match case-insensitively with one optional trailing slash, on the raw (undecoded) path;
- a known path with a method it does not serve is 404 `Not found.` (Starlette would answer 405),
  except `GET`/`DELETE /mcp`, which are 405 with `Allow: POST` and a JSON-RPC error;
- `GET /healthz` is answered before the Host check, everything else after Host and Origin.

uvicorn runs with `proxy_headers=False`, `access_log=False`, `log_config=None`,
`server_header=False`; `web.client_ip` trusts forwarding headers only from loopback proxies.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx2
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.requests import HTTPConnection
from starlette.responses import PlainTextResponse, Response
from starlette.routing import BaseRoute, Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .auth.bearer import BearerMiddleware, bearer_token
from .auth.cimd import CimdResolver
from .auth.router import DEFAULT_AUTHORIZE_RATE_LIMIT, AuthRouterOptions, RateLimitSettings, create_auth_routes
from .auth.store import OAuthStore
from .clock import Clock
from .config import Config, config_warnings
from .idle_close import IdleCloser
from .log import Logger, fingerprint, strip_query
from .mcp import McpServerOptions, build_remote_control, create_mcp_server
from .notify import TextSender
from .remote.discord import DiscordNotifier, combine
from .remote.tools import RemoteTools, RemoteToolsOptions
from .remote.watcher import Watcher, WatcherOptions
from .tasks.manager import TaskManager
from .web import NO_STORE, BodyError, client_ip, content_type, json_response, read_json_body

CLAUDE_ORIGINS: tuple[str, ...] = ("https://claude.ai", "https://claude.com")
"""Origins of the Claude web apps; requests from browsers elsewhere are refused."""

MCP_BODY_LIMIT_BYTES = 1024 * 1024
"""Body limit of JSON-RPC requests on /mcp."""


@dataclass(frozen=True)
class AuthRouterOverrides:
    """Optional AuthRouterOptions fields (everything but config, store and logger), mainly for tests."""

    state_dir: str | None = None
    now: Clock | None = None
    http_client: httpx2.AsyncClient | None = None
    rate_limit: RateLimitSettings | None = DEFAULT_AUTHORIZE_RATE_LIMIT
    cimd: CimdResolver | None = None


@dataclass(frozen=True)
class CreateAppOptions:
    config: Config
    logger: Logger
    store: OAuthStore
    manager: TaskManager
    auth: AuthRouterOverrides = AuthRouterOverrides()
    send_text: TextSender | None = None
    """Plain-text pushes for the notification watcher (`HttpNotifier.send_text`); without it the
    watcher stays off."""
    remote_tools: RemoteTools | None = None
    """Default: built from the config."""
    discord: DiscordNotifier | None = None
    """Posts to the owner's Discord channel (closed with the app's lifespan)."""
    discord_problem: str | None = None
    """Why Discord is off although `discord.enabled` is on (an incident at start)."""


def _json_rpc_error(status: int, code: int, message: str, headers: dict[str, str] | None = None) -> Response:
    return json_response(status, {"jsonrpc": "2.0", "error": {"code": code, "message": message}, "id": None}, headers)


def _text(status: int, text: str, headers: dict[str, str] | None = None) -> Response:
    return PlainTextResponse(text, status_code=status, headers=headers)


def _token_fingerprint(headers: Headers) -> str | None:
    """The token fingerprint for the log line, when the request carries a bearer token."""
    token = bearer_token(headers.get("authorization"))
    return fingerprint(token) if token else None


def _raw_path(scope: Scope) -> str:
    raw = scope.get("raw_path")
    return raw.decode("latin-1") if isinstance(raw, bytes) else str(scope.get("path", ""))


def _original_url(scope: Scope) -> str:
    query = scope.get("query_string", b"")
    path = _raw_path(scope)
    return f"{path}?{query.decode('latin-1')}" if query else path


class _McpEndpoint:
    """JSON body check (1 MB limit) in front of the SDK handler: the body is read and checked here
    (413, 400 or 415 as JSON-RPC errors), then replayed to the SDK."""

    def __init__(self, manager: StreamableHTTPSessionManager) -> None:
        self._manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            body = await self._read_json_body(scope, receive)
        except BodyError as err:
            if err.status == 413:
                response = _json_rpc_error(413, -32600, "Request body too large.")
            else:
                response = _json_rpc_error(err.status, -32700, "Parse error.")
            await response(scope, receive, send)
            return
        if body is None:
            await self._manager.handle_request(scope, receive, send)
            return
        headers = [(k, v) for k, v in scope["headers"] if k.lower() != b"content-length"]
        headers.append((b"content-length", str(len(body)).encode("latin-1")))
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self._manager.handle_request({**scope, "headers": headers}, replay, send)

    @staticmethod
    async def _read_json_body(scope: Scope, receive: Receive) -> bytes | None:
        """The JSON body to replay, or None when there is nothing to check (no body, or not
        `application/json`) and the SDK reads it itself. An empty body counts as `{}`."""
        headers = Headers(scope=scope)
        if "content-length" not in headers and "transfer-encoding" not in headers:
            return None
        if content_type(headers)[0] != "application/json":
            return None

        async def chunks() -> AsyncIterator[bytes]:
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    raise BodyError(400)
                yield message.get("body", b"")
                if not message.get("more_body", False):
                    return

        raw, _ = await read_json_body(headers, chunks(), MCP_BODY_LIMIT_BYTES)
        return raw


class _App:
    """Request log, health check, Host and Origin checks, auth routes, /mcp, 404 and the error handler."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        options: CreateAppOptions,
        auth_routes: list[BaseRoute],
        mcp_endpoint: ASGIApp,
    ) -> None:
        self._app = app
        config = options.config
        self._logger = options.logger.child({"component": "http"})
        self._allowed_origins = {config.issuer, *CLAUDE_ORIGINS}
        self._public_host = config.public_host.lower()
        self._auth_routes = auth_routes
        self._mcp = BearerMiddleware(mcp_endpoint, store=options.store, config=config, logger=options.logger)
        known = ["/healthz", "/mcp", *(getattr(route, "path", "") for route in auth_routes)]
        self._canonical = {path.lower(): path for path in known if path}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        start = time.monotonic()
        headers = Headers(scope=scope)
        token = _token_fingerprint(headers)
        original_url = _original_url(scope)
        status: list[int] = []

        async def tracked_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                status.append(int(message["status"]))
            await send(message)

        try:
            await self._dispatch(
                scope, receive, tracked_send, headers=headers, original_url=original_url, status=status
            )
        finally:
            if status:
                self._logger.info(
                    "request",
                    {
                        "method": scope["method"],
                        "path": strip_query(original_url),
                        "status": status[0],
                        "ms": int((time.monotonic() - start) * 1000),
                        "ip": client_ip(HTTPConnection(scope)),
                        "token": token,
                    },
                )

    def _route_path(self, scope: Scope) -> str | None:
        """Path matching: case-insensitive, one optional trailing slash, on the raw path."""
        raw = _raw_path(scope)
        if len(raw) > 1 and raw.endswith("/"):
            raw = raw[:-1]
        return self._canonical.get(raw.lower())

    async def _dispatch(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        headers: Headers,
        original_url: str,
        status: list[int],
    ) -> None:
        method = scope["method"]
        path = self._route_path(scope)

        # Health check: no auth and no Host check, so local monitoring can reach it.
        if path == "/healthz" and method in ("GET", "HEAD"):
            await json_response(200, {"ok": True})(scope, receive, send)
            return

        # DNS rebinding protection: the Host header must be the public host.
        if headers.get("host", "").lower() != self._public_host:
            await _text(403, "Forbidden: unknown host.", NO_STORE)(scope, receive, send)
            return

        # Browsers send Origin; only our own origin and the Claude apps are allowed.
        origin = headers.get("origin")
        if origin is not None and origin.lower() not in self._allowed_origins:
            self._logger.warn("origin refused", {"origin": origin[:100], "path": strip_query(original_url)})
            await _text(403, "Forbidden: origin not allowed.", NO_STORE)(scope, receive, send)
            return

        try:
            await self._route(scope, receive, send, path, method)
        except Exception as err:
            if status:
                raise
            self._logger.error("request failed", {"path": strip_query(original_url), "error": err})
            await _text(500, "Internal error.")(scope, receive, send)

    async def _route(self, scope: Scope, receive: Receive, send: Send, path: str | None, method: str) -> None:
        if path is not None:
            routed: Scope = {**scope, "path": path}
            for route in self._auth_routes:
                match, child_scope = route.matches(routed)
                if match == Match.FULL:
                    routed.update(child_scope)
                    await route.handle(routed, receive, send)
                    return
            if path == "/mcp":
                if method == "POST":
                    await self._mcp(routed, receive, send)
                    return
                if method in ("GET", "HEAD", "DELETE"):
                    response = _json_rpc_error(405, -32000, "Method not allowed.", {"Allow": "POST"})
                    await response(scope, receive, send)
                    return
        await _text(404, "Not found.", NO_STORE)(scope, receive, send)


def create_app(options: CreateAppOptions) -> Starlette:
    """Builds the app. Order: request log (method, path without query, status, ms, ip,
    token fingerprint), `GET /healthz` (no Host check), Host must equal `config.public_host`
    (403 text), Origin must be the issuer or a Claude origin (403 text), auth routes, `/mcp`."""
    config = options.config
    overrides = options.auth
    for warning in config_warnings(config):
        options.logger.warn("config warning", {"warning": warning})
    auth_routes = create_auth_routes(
        AuthRouterOptions(
            config=config,
            store=options.store,
            state_dir=overrides.state_dir or config.state_dir,
            logger=options.logger,
            now=overrides.now,
            http_client=overrides.http_client,
            rate_limit=overrides.rate_limit,
            cimd=overrides.cimd,
        )
    )
    remote = build_remote_control(options.manager, config, options.logger.child({"component": "mcp"}))
    idle_close = IdleCloser(
        options.manager,
        remote,
        idle_minutes=config.remote_control.idle_close_minutes,
        logger=options.logger.child({"component": "idle-close"}),
    )
    remote_tools = options.remote_tools or RemoteTools(
        RemoteToolsOptions(
            config=config,
            manager=options.manager,
            logger=options.logger,
            notify=options.send_text,
            discord=options.discord,
            discord_problem=options.discord_problem,
        )
    )
    discord = options.discord
    # One notify path: the watcher's pushes go to notify.url and, when configured, to Discord.
    watch_send = combine(
        options.send_text, discord.send_watch if discord is not None and discord.wants("watch") else None
    )
    watcher = (
        Watcher(
            WatcherOptions(
                watch=config.watch,
                check=remote_tools.collect_pending,
                send=watch_send,
                state_dir=config.state_dir,
                logger=options.logger.child({"component": "watch"}),
            )
        )
        if config.watch.interval_seconds > 0 and watch_send is not None
        else None
    )
    server = create_mcp_server(
        McpServerOptions(
            manager=options.manager,
            config=config,
            logger=options.logger,
            remote_control=remote,
            remote_tools=remote_tools,
        )
    )
    session_manager = StreamableHTTPSessionManager(
        app=server._lowlevel_server,
        stateless=True,
        json_response=True,
        security_settings=None,
        max_request_body_size=MCP_BODY_LIMIT_BYTES,
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            await remote_tools.start()
            idle_close.start()
            if watcher is not None:
                watcher.start()
            try:
                yield
            finally:
                if watcher is not None:
                    await watcher.stop()
                await idle_close.stop()
                await remote_tools.stop()
                await remote.aclose()
                if discord is not None:
                    await discord.aclose()

    return Starlette(
        lifespan=lifespan,
        middleware=[
            Middleware(
                _App,
                options=options,
                auth_routes=auth_routes,
                mcp_endpoint=_McpEndpoint(session_manager),
            )
        ],
    )
