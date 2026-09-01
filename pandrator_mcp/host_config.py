"""Configuration renderers for supported MCP hosts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

HostKind = Literal[
    "codex",
    "claude-code",
    "opencode",
    "antigravity",
]

_SERVER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


@dataclass(frozen=True, slots=True)
class HostConfig:
    host: HostKind
    server_name: str
    content_type: str
    suggested_filename: str
    content: str

    def as_dict(self) -> dict[str, str]:
        return {
            "schema_version": "1",
            "host": self.host,
            "server_name": self.server_name,
            "content_type": self.content_type,
            "suggested_filename": self.suggested_filename,
            "content": self.content,
        }


def _validated_name(value: str) -> str:
    name = str(value or "").strip()
    if not _SERVER_NAME.fullmatch(name):
        raise ValueError(
            "Host server names must contain 1-120 letters, numbers, "
            "dots, underscores, or hyphens."
        )
    return name


def _stdio_command(
    *,
    target: str,
    configuration_path: Path,
    executable: str,
) -> tuple[str, list[str]]:
    selected_executable = str(executable or "").strip()
    if not selected_executable:
        raise ValueError("The Pandrator MCP executable is required.")
    selected_target = _validated_name(target)
    path = configuration_path.expanduser().resolve(strict=False)
    return (
        selected_executable,
        [
            "stdio",
            "--target",
            selected_target,
            "--config",
            str(path),
        ],
    )


def render_host_config(
    host: HostKind,
    *,
    target: str,
    configuration_path: Path,
    executable: str = "pandrator-mcp",
    server_name: str | None = None,
) -> HostConfig:
    """Render one pasteable host fragment without target credentials."""

    if host not in {
        "codex",
        "claude-code",
        "opencode",
        "antigravity",
    }:
        raise ValueError(f"Unsupported MCP host: {host}")
    selected_name = _validated_name(
        server_name or f"pandrator-{target}"
    )
    command, arguments = _stdio_command(
        target=target,
        configuration_path=configuration_path,
        executable=executable,
    )

    if host == "codex":
        quoted_name = json.dumps(selected_name, ensure_ascii=False)
        quoted_command = json.dumps(command, ensure_ascii=False)
        quoted_arguments = ", ".join(
            json.dumps(item, ensure_ascii=False)
            for item in arguments
        )
        content = (
            f"[mcp_servers.{quoted_name}]\n"
            f"command = {quoted_command}\n"
            f"args = [{quoted_arguments}]\n"
            "enabled = true\n"
            'default_tools_approval_mode = "writes"\n'
            "startup_timeout_sec = 20\n"
            "tool_timeout_sec = 120\n"
        )
        return HostConfig(
            host=host,
            server_name=selected_name,
            content_type="application/toml",
            suggested_filename=".codex/config.toml",
            content=content,
        )

    if host == "opencode":
        payload = {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                selected_name: {
                    "type": "local",
                    "command": [command, *arguments],
                }
            },
        }
        filename = "opencode.json"
    else:
        server: dict[str, object] = {
            "command": command,
            "args": arguments,
        }
        if host == "claude-code":
            server["type"] = "stdio"
            filename = ".mcp.json"
        else:
            filename = ".agents/mcp_config.json"
        payload = {"mcpServers": {selected_name: server}}

    return HostConfig(
        host=host,
        server_name=selected_name,
        content_type="application/json",
        suggested_filename=filename,
        content=(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ),
    )


def render_http_host_config(
    host: HostKind,
    *,
    endpoint: str,
    bearer_token: str,
    server_name: str = "pandrator",
) -> HostConfig:
    """Render an explicitly secret-bearing local HTTP host configuration."""

    if host not in {
        "codex",
        "claude-code",
        "opencode",
        "antigravity",
    }:
        raise ValueError(f"Unsupported MCP host: {host}")
    selected_name = _validated_name(server_name)
    selected_endpoint = str(endpoint or "").strip().rstrip("/")
    parsed = urlsplit(selected_endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.path != "/mcp"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Managed MCP host configuration requires the loopback HTTP /mcp endpoint."
        )
    token = str(bearer_token or "").strip()
    if len(token) < 43 or len(token) > 4096 or any(character.isspace() for character in token):
        raise ValueError("The managed MCP bearer credential is invalid.")
    authorization = f"Bearer {token}"

    if host == "codex":
        quoted_name = json.dumps(selected_name, ensure_ascii=False)
        content = (
            f"[mcp_servers.{quoted_name}]\n"
            f"url = {json.dumps(selected_endpoint, ensure_ascii=False)}\n"
            "enabled = true\n"
            'default_tools_approval_mode = "writes"\n'
            "startup_timeout_sec = 20\n"
            "tool_timeout_sec = 120\n\n"
            f"[mcp_servers.{quoted_name}.http_headers]\n"
            f"Authorization = {json.dumps(authorization, ensure_ascii=False)}\n"
        )
        return HostConfig(
            host=host,
            server_name=selected_name,
            content_type="application/toml",
            suggested_filename=".codex/config.toml",
            content=content,
        )

    if host == "opencode":
        payload = {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                selected_name: {
                    "type": "remote",
                    "url": selected_endpoint,
                    "enabled": True,
                    "oauth": False,
                    "headers": {"Authorization": authorization},
                }
            },
        }
        filename = "opencode.json"
    elif host == "claude-code":
        payload = {
            "mcpServers": {
                selected_name: {
                    "type": "http",
                    "url": selected_endpoint,
                    "headers": {"Authorization": authorization},
                }
            }
        }
        filename = ".mcp.json"
    else:
        payload = {
            "mcpServers": {
                selected_name: {
                    "serverUrl": selected_endpoint,
                    "headers": {"Authorization": authorization},
                }
            }
        }
        filename = ".agents/mcp_config.json"

    return HostConfig(
        host=host,
        server_name=selected_name,
        content_type="application/json",
        suggested_filename=filename,
        content=json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )
