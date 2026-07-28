"""Drive Pandrator's native-client enrollment for release qualification.

This helper is intentionally separate from the user-facing MCP CLI. It lets a
release smoke exercise the real browser/PKCE enrollment path without placing a
disposable owner password in argv, logs, a target profile, or a host
configuration. The password is read once from stdin.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

_NONCE = re.compile(r'name="authorization_nonce"\s+value="([^"]+)"')
_CREDENTIAL_MATERIAL = re.compile(r"\b(?:pan_|mrt_)[A-Za-z0-9_-]+")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Approve a disposable Pandrator MCP enrollment using an owner "
            "password supplied on stdin."
        )
    )
    parser.add_argument("--mcp-executable", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--expected-origin", required=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    return parser


def _exact_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("The expected origin must be an exact HTTP(S) origin.")
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            "",
            "",
            "",
        )
    ).rstrip("/")


def _read_password() -> str:
    value = sys.stdin.readline().rstrip("\r\n")
    if not value:
        raise ValueError("An owner password must be supplied on stdin.")
    return value


def _authorization_url(process: subprocess.Popen[str], expected_origin: str) -> str:
    assert process.stdout is not None
    instruction = process.stdout.readline().strip()
    authorization_url = process.stdout.readline().strip()
    expected_instruction = "Open this trusted Pandrator authorization URL in a browser:"
    if instruction != expected_instruction:
        raise RuntimeError("The MCP CLI emitted an unexpected enrollment prompt.")
    parsed = urlsplit(authorization_url)
    actual_origin = urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), "", "", "")
    ).rstrip("/")
    if (
        actual_origin != expected_origin
        or parsed.path != "/api/v1/auth/automation/authorize"
        or not parsed.query
    ):
        raise RuntimeError(
            "The enrollment authorization URL does not match the expected target."
        )
    return authorization_url


def _approve(
    authorization_url: str,
    *,
    password: str,
    timeout: float,
) -> None:
    session = requests.Session()
    session.trust_env = False
    response = session.get(
        authorization_url,
        allow_redirects=False,
        timeout=(3, min(timeout, 20.0)),
    )
    response.raise_for_status()
    nonce = _NONCE.search(response.text)
    if nonce is None:
        raise RuntimeError("The enrollment page omitted its authorization nonce.")
    parsed = urlsplit(authorization_url)
    approval_url = urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, "", "")
    )
    approved = session.post(
        approval_url,
        data={
            "authorization_nonce": nonce.group(1),
            "password": password,
            "decision": "approve",
        },
        allow_redirects=True,
        timeout=(3, min(timeout, 20.0)),
    )
    approved.raise_for_status()


def _safe_result(process: subprocess.Popen[str], *, timeout: float) -> dict[str, object]:
    assert process.stdout is not None
    assert process.stderr is not None
    output, errors = process.communicate(timeout=timeout)
    if process.returncode:
        raise RuntimeError(
            "The MCP enrollment process failed: "
            + (errors.strip() or f"exit code {process.returncode}")
        )
    if _CREDENTIAL_MATERIAL.search(output) or "access_token" in output:
        raise RuntimeError("Enrollment output contained credential material.")
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError("The MCP CLI returned invalid enrollment JSON.") from error
    allowed = {
        "target",
        "client_id",
        "subject",
        "scopes",
        "target_instance_id",
        "canonical_origin",
        "expires_at",
        "credential_backend",
        "browser_flow",
        "credential_rotated",
    }
    if set(payload) - allowed:
        raise RuntimeError("Enrollment output contained an unexpected field.")
    return payload


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    expected_origin = _exact_origin(args.expected_origin)
    owner_password = _read_password()
    environment = dict(os.environ)
    environment["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        [
            str(args.mcp_executable.expanduser().resolve()),
            "target",
            "--config",
            str(args.config.expanduser().resolve()),
            "login",
            args.target,
            "--no-open-browser",
            "--timeout",
            str(args.timeout),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        creationflags=(
            subprocess.CREATE_NO_WINDOW
            if sys.platform == "win32"
            else 0
        ),
    )
    try:
        authorization_url = _authorization_url(process, expected_origin)
        _approve(
            authorization_url,
            password=owner_password,
            timeout=args.timeout,
        )
        owner_password = ""
        payload = _safe_result(process, timeout=args.timeout)
    finally:
        owner_password = ""
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
