"""Bounded, redacted support bundles for installation troubleshooting."""

from __future__ import annotations

import io
import json
import os
import platform
import re
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from . import __version__
from .launcher import launcher_metadata_path
from .network import load_network_configuration
from .tls import select_ca_bundle

if TYPE_CHECKING:
    from .application import ManagerApplication
    from .supervisor import ProcessSupervisor

_MAX_MANAGER_LOG_BYTES = 1024 * 1024
_MAX_SERVICE_LOG_BYTES = 256 * 1024
_MAX_LOG_BYTES_TOTAL = 4 * 1024 * 1024
_MAX_JSON_FILE_BYTES = 128 * 1024
_URL_PATTERN = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)
    (
      ["']?
      (?:authorization|proxy-authorization|cookie|set-cookie|password|
         passwd|secret|csrf(?:_token)?|access[_-]?token|refresh[_-]?token|
         api[_-]?key|client[_-]?secret)
      ["']?
      \s*[:=]\s*
    )
    (["']?)
    ([^,\s"'}\]]+)
    (["']?)
    """
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_KEY_MARKERS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
    "csrf",
    "api_key",
    "apikey",
)


@dataclass(frozen=True, slots=True)
class DiagnosticBundle:
    filename: str
    payload: bytes


class DiagnosticRedactor:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.expanduser().resolve(strict=False)
        self.home = Path.home().expanduser().resolve(strict=False)
        replacements: list[tuple[str, str]] = [
            (str(self.workspace), "$WORKSPACE"),
            (str(self.home), "$HOME"),
        ]
        for path, replacement in tuple(replacements):
            alternative = path.replace("\\", "/")
            if alternative != path:
                replacements.append((alternative, replacement))
        self.path_replacements = sorted(
            set(replacements),
            key=lambda item: len(item[0]),
            reverse=True,
        )

    @staticmethod
    def _redact_url(match: re.Match[str]) -> str:
        value = match.group(0)
        trailing = ""
        while value and value[-1] in ".,;)]}":
            trailing = value[-1] + trailing
            value = value[:-1]
        try:
            parsed = urlsplit(value)
            host = parsed.hostname or ""
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            port = parsed.port
            netloc = f"{host}:{port}" if port is not None else host
            value = urlunsplit(
                (parsed.scheme, netloc, parsed.path, "", "")
            )
        except ValueError:
            value = "<redacted-url>"
        return value + trailing

    def text(self, value: str) -> str:
        redacted = str(value)
        for path, replacement in self.path_replacements:
            redacted = re.sub(
                re.escape(path),
                lambda _match, selected=replacement: selected,
                redacted,
                flags=re.IGNORECASE if os.name == "nt" else 0,
            )
        redacted = _URL_PATTERN.sub(self._redact_url, redacted)
        redacted = _BEARER.sub("Bearer <redacted>", redacted)
        redacted = _SECRET_ASSIGNMENT.sub(
            lambda match: (
                f"{match.group(1)}{match.group(2)}"
                f"<redacted>{match.group(4)}"
            ),
            redacted,
        )
        return redacted

    def value(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): (
                    "<redacted>"
                    if any(
                        marker in str(key).casefold()
                        for marker in _SECRET_KEY_MARKERS
                    )
                    else self.value(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.value(item) for item in value]
        if isinstance(value, str):
            return self.text(value)
        return value


def _tail(path: Path, maximum_bytes: int) -> str:
    try:
        if path.is_symlink():
            return ""
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - maximum_bytes))
            payload = handle.read(maximum_bytes)
    except (FileNotFoundError, OSError):
        return ""
    return payload.decode("utf-8", errors="replace")


def _bounded_json(path: Path) -> dict[str, Any] | None:
    try:
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size > _MAX_JSON_FILE_BYTES
        ):
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _operation_payload(application: ManagerApplication) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for operation in application.store.list_operations(limit=25):
        tasks = application.store.operation_tasks(operation.id)
        operations.append(
            {
                "id": operation.id,
                "plan_id": operation.plan_id,
                "kind": operation.kind.value,
                "state": operation.state.value,
                "progress": operation.progress,
                "current_task_id": operation.current_task_id,
                "error_code": operation.error_code,
                "error_message": operation.error_message,
                "recovery": operation.recovery,
                "created_at": operation.created_at.isoformat(),
                "updated_at": operation.updated_at.isoformat(),
                "finished_at": (
                    operation.finished_at.isoformat()
                    if operation.finished_at
                    else None
                ),
                "tasks": [
                    {
                        "id": task.task.id,
                        "kind": task.task.kind,
                        "label": task.task.label,
                        "component_id": task.task.component_id,
                        "state": task.state.value,
                        "attempt": task.attempt,
                        "error": task.error,
                        "started_at": (
                            task.started_at.isoformat()
                            if task.started_at
                            else None
                        ),
                        "finished_at": (
                            task.finished_at.isoformat()
                            if task.finished_at
                            else None
                        ),
                    }
                    for task in tasks
                ],
            }
        )
    return operations


def _component_payload(application: ManagerApplication) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for component_id, (desired, inspection) in sorted(
        application.store.component_records().items()
    ):
        result.append(
            {
                "id": component_id,
                "desired": (
                    desired.model_dump(mode="json")
                    if desired is not None
                    else None
                ),
                "inspection": inspection.model_dump(mode="json"),
            }
        )
    return result


def _release_payload(application: ManagerApplication) -> dict[str, Any]:
    layout = application.context.layout
    return {
        "accepted": {
            product: application.store.accepted_release(product)
            for product in ("pandrator", "pandrator-manager")
        },
        "active_pointers": {
            "pandrator": _bounded_json(
                layout.root / "app" / "current.json"
            ),
            "pandrator-manager": _bounded_json(
                layout.root / "manager" / "current.json"
            ),
        },
        "launcher": _bounded_json(launcher_metadata_path(layout)),
    }


def _log_files(layout) -> list[tuple[str, Path, int]]:
    selected: list[tuple[str, Path, int]] = []
    for name in (
        "manager.log",
        "manager.log.1",
        "manager.log.2",
        "manager.log.3",
        "manager-launch.log",
        "manager-launch.log.1",
    ):
        selected.append(
            (f"logs/{name}", layout.logs / name, _MAX_MANAGER_LOG_BYTES)
        )
    service_root = layout.logs / "services"
    if service_root.is_symlink():
        return selected
    try:
        service_logs = sorted(service_root.glob("*.log"))[:20]
    except OSError:
        service_logs = []
    for path in service_logs:
        if path.is_symlink():
            continue
        selected.append(
            (
                f"logs/services/{path.name}",
                path,
                _MAX_SERVICE_LOG_BYTES,
            )
        )
    return selected


def build_diagnostic_bundle(
    application: ManagerApplication,
    supervisor: ProcessSupervisor,
) -> DiagnosticBundle:
    """Build a support ZIP in memory; no diagnostic copy is retained on disk."""

    generated_at = datetime.now(timezone.utc)
    redactor = DiagnosticRedactor(application.context.layout.workspace)
    ca_bundle = select_ca_bundle(application.context.environment)
    summary = {
        "schema_version": 1,
        "generated_at": generated_at.isoformat(),
        "manager": {
            "version": __version__,
            "instance_id": application.instance_id,
            "frozen": bool(getattr(sys, "frozen", False)),
            "python": platform.python_version(),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
        },
        "workspace": "$WORKSPACE",
        "tls": ca_bundle.diagnostic_payload(),
        "network": load_network_configuration(
            application.context.layout
        ).model_dump(mode="json"),
        "doctor": application.doctor(
            supervisor=supervisor
        ).model_dump(mode="json"),
        "components": _component_payload(application),
        "services": [
            service.model_dump(mode="json")
            for service in supervisor.snapshot()
        ],
        "operations": _operation_payload(application),
        "releases": _release_payload(application),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(
        output,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        archive.writestr(
            "README.txt",
            (
                "Pandrator Manager diagnostic bundle\n\n"
                "This bundle is generated on demand for issue reports. It "
                "contains bounded log tails and a redacted system summary. "
                "Manager databases, browser sessions, credential files, and "
                "raw environment variables are not included. Known sensitive "
                "fields are redacted. Review the files before sharing them.\n"
            ),
        )
        archive.writestr(
            "summary.json",
            json.dumps(
                redactor.value(summary),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        remaining = _MAX_LOG_BYTES_TOTAL
        for archive_name, path, maximum in _log_files(
            application.context.layout
        ):
            if remaining <= 0:
                break
            maximum = min(maximum, remaining)
            content = _tail(path, maximum)
            if not content:
                continue
            encoded = redactor.text(content).encode(
                "utf-8",
                errors="replace",
            )
            remaining -= min(maximum, len(encoded))
            archive.writestr(archive_name, encoded)
    filename = (
        "pandrator-diagnostics-"
        f"{generated_at.strftime('%Y%m%dT%H%M%SZ')}.zip"
    )
    return DiagnosticBundle(filename=filename, payload=output.getvalue())
