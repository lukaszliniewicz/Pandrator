"""Authenticated loopback Streamable HTTP transport for Pandrator MCP."""

from __future__ import annotations

import hmac
from pathlib import Path
from typing import Any

from . import __version__
from .context import McpRuntime
from .server import build_server

MCP_HTTP_HOST = "127.0.0.1"
MCP_HTTP_PORT = 8099
MCP_HTTP_PATH = "/mcp"
MCP_HEALTH_PATH = "/health"
MCP_PROTOCOL_VERSION = "2026-07-28"
MAXIMUM_MCP_REQUEST_BYTES = 16 * 1024 * 1024


def read_bearer_token(path: str | Path) -> str:
    """Load a pre-provisioned local credential without accepting weak values."""

    selected = Path(path).expanduser().resolve(strict=False)
    try:
        token = selected.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise RuntimeError("The managed MCP credential could not be read.") from error
    if len(token) < 43 or len(token) > 4096 or any(character.isspace() for character in token):
        raise RuntimeError("The managed MCP credential is invalid.")
    return token


class BearerAuthenticationMiddleware:
    """Require one exact bearer credential for every protocol request."""

    def __init__(self, app: Any, *, token: str) -> None:
        self.app = app
        self._expected = f"Bearer {token}".encode("utf-8")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("path") == MCP_HEALTH_PATH:
            await self.app(scope, receive, send)
            return

        values = [
            value
            for name, value in scope.get("headers", ())
            if name.lower() == b"authorization"
        ]
        authorized = (
            len(values) == 1
            and len(values[0]) <= 4096
            and hmac.compare_digest(values[0], self._expected)
        )
        if authorized:
            await self.app(scope, receive, send)
            return

        from starlette.responses import JSONResponse

        response = JSONResponse(
            {"error": "authentication_required"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)


def build_http_app(
    runtime: McpRuntime,
    *,
    token: str,
    host: str = MCP_HTTP_HOST,
) -> Any:
    """Build a stateless July 2026 HTTP MCP application for loopback use."""

    if host not in {"127.0.0.1", "::1"}:
        raise ValueError("Managed MCP HTTP may bind only to a loopback IP address.")

    try:
        from mcp.server.transport_security import TransportSecuritySettings
        from starlette.requests import Request
        from starlette.responses import JSONResponse
    except ImportError as error:
        raise RuntimeError(
            "pandrator-mcp requires the pinned mcp==2.1.1 runtime dependency."
        ) from error

    server = build_server(runtime)

    @server.custom_route(
        MCP_HEALTH_PATH,
        methods=["GET"],
        include_in_schema=False,
    )
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "service": "pandrator-mcp",
                "version": __version__,
                "protocol_version": MCP_PROTOCOL_VERSION,
                "transport": "streamable-http",
                "endpoint": MCP_HTTP_PATH,
            }
        )

    allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    allowed_origins = [
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
    ]
    app = server.streamable_http_app(
        streamable_http_path=MCP_HTTP_PATH,
        json_response=True,
        stateless_http=True,
        max_request_body_size=MAXIMUM_MCP_REQUEST_BYTES,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        ),
        host=host,
    )
    return BearerAuthenticationMiddleware(app, token=token)


def run_http_server(
    runtime: McpRuntime,
    *,
    token_file: str | Path,
    host: str = MCP_HTTP_HOST,
    port: int = MCP_HTTP_PORT,
) -> None:
    """Serve authenticated Streamable HTTP until the process is stopped."""

    if isinstance(port, bool) or not 1 <= int(port) <= 65535:
        raise ValueError("The managed MCP HTTP port must be between 1 and 65535.")
    token = read_bearer_token(token_file)
    app = build_http_app(runtime, token=token, host=host)
    try:
        import uvicorn
    except ImportError as error:
        raise RuntimeError(
            "pandrator-mcp requires the HTTP runtime bundled with mcp==2.1.1."
        ) from error
    uvicorn.run(
        app,
        host=host,
        port=int(port),
        access_log=False,
        proxy_headers=False,
        server_header=False,
    )
