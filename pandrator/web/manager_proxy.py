"""Authenticated, allowlisted bridge from Pandrator to its local manager.

The browser never receives the manager bearer credential or connection
descriptor.  Pandrator validates the descriptor and manager process identity,
then forwards only the operations exposed explicitly below.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Callable
from urllib.parse import urlsplit

import psutil
import requests
from flask import g, has_request_context, jsonify, request

from .schemas import (
    ManagerLegacyImportRequest,
    ManagerOperationRequest,
    ManagerPlanRequest,
    ManagerReleasePlanRequest,
    ManagerRuntimeRequest,
    ManagerUninstallPlanRequest,
)


def _loopback(value: object) -> bool:
    candidate = str(value or "").split("%", 1)[0]
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(address.is_loopback or (mapped and mapped.is_loopback))


def _same_path(first: str | os.PathLike[str], second: str | os.PathLike[str]) -> bool:
    return os.path.normcase(
        str(Path(first).expanduser().resolve(strict=False))
    ) == os.path.normcase(
        str(Path(second).expanduser().resolve(strict=False))
    )


class ManagerProxyError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 503,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


@dataclass(frozen=True, slots=True)
class ManagerConnection:
    base_url: str
    instance_id: str
    secret: str


class LocalManagerProxy:
    """Discover and validate the manager without importing its Python package."""

    def __init__(
        self,
        *,
        descriptor_path: str | os.PathLike[str] | None = None,
        credential_path: str | os.PathLike[str] | None = None,
        session: requests.Session | None = None,
    ) -> None:
        configured_descriptor = descriptor_path or os.environ.get(
            "PANDRATOR_MANAGER_DESCRIPTOR"
        )
        configured_credential = credential_path or os.environ.get(
            "PANDRATOR_MANAGER_CREDENTIAL"
        )
        self.descriptor_path = (
            Path(configured_descriptor).expanduser().resolve(strict=False)
            if configured_descriptor
            else None
        )
        self.credential_path = (
            Path(configured_credential).expanduser().resolve(strict=False)
            if configured_credential
            else None
        )
        self.session = session or requests.Session()
        self.session.trust_env = False
        self._session_lock = RLock()

    @property
    def configured(self) -> bool:
        return self.descriptor_path is not None

    def discover(self) -> ManagerConnection:
        if self.descriptor_path is None:
            raise ManagerProxyError(
                "manager_not_configured",
                "This Pandrator process was not started by Pandrator Manager.",
            )
        try:
            payload = json.loads(self.descriptor_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            raise ManagerProxyError(
                "manager_unavailable",
                "The manager connection descriptor is unavailable or invalid.",
            ) from error

        required = {
            "base_url",
            "instance_id",
            "pid",
            "process_create_time",
            "executable",
        }
        if not required.issubset(payload):
            raise ManagerProxyError(
                "manager_descriptor_invalid",
                "The manager connection descriptor is incomplete.",
            )
        parsed = urlsplit(str(payload["base_url"]))
        if (
            parsed.scheme != "http"
            or not _loopback(parsed.hostname)
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or parsed.port is None
        ):
            raise ManagerProxyError(
                "manager_descriptor_unsafe",
                "The manager descriptor does not contain a safe loopback endpoint.",
            )

        try:
            process = psutil.Process(int(payload["pid"]))
            create_time = float(payload["process_create_time"])
            if abs(process.create_time() - create_time) > 0.01:
                raise ManagerProxyError(
                    "manager_identity_mismatch",
                    "The manager PID has been reused by another process.",
                )
            if not _same_path(process.exe(), str(payload["executable"])):
                raise ManagerProxyError(
                    "manager_identity_mismatch",
                    "The manager executable does not match its descriptor.",
                )
        except ManagerProxyError:
            raise
        except (psutil.Error, OSError, TypeError, ValueError) as error:
            raise ManagerProxyError(
                "manager_unavailable",
                "The manager process cannot be validated.",
            ) from error

        credential = self.credential_path or (
            self.descriptor_path.parent / "client.secret"
        )
        # The manager credential is always co-located with its protected
        # descriptor.  Refuse an environment-injected path outside that state
        # directory so a compromised app configuration cannot read arbitrary
        # files through this bridge.
        if credential.parent.resolve(strict=False) != self.descriptor_path.parent:
            raise ManagerProxyError(
                "manager_credential_unsafe",
                "The manager credential path is outside manager state.",
            )
        try:
            secret = credential.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise ManagerProxyError(
                "manager_unavailable",
                "The manager client credential is unavailable.",
            ) from error
        if len(secret) < 32:
            raise ManagerProxyError(
                "manager_credential_invalid",
                "The manager client credential is invalid.",
            )
        return ManagerConnection(
            base_url=str(payload["base_url"]).rstrip("/"),
            instance_id=str(payload["instance_id"]),
            secret=secret,
        )

    def request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        timeout: float = 30,
    ) -> tuple[dict[str, Any], int]:
        if not path.startswith("/v1/") or "://" in path:
            raise ValueError("Manager proxy paths must be allowlisted v1 resources.")
        connection = self.discover()
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {connection.secret}",
        }
        if has_request_context():
            request_id = str(
                getattr(g, "request_id", "") or ""
            )
            traceparent = str(
                getattr(g, "traceparent", "") or ""
            )
            if request_id:
                headers["X-Request-ID"] = request_id[:120]
            if traceparent:
                headers["traceparent"] = traceparent[:160]
        if method.upper() not in {"GET", "HEAD"}:
            headers["Idempotency-Key"] = (
                idempotency_key or str(uuid.uuid4())
            )
        try:
            with self._session_lock:
                response = self.session.request(
                    method.upper(),
                    f"{connection.base_url}{path}",
                    headers=headers,
                    json=body,
                    timeout=timeout,
                )
        except requests.RequestException as error:
            raise ManagerProxyError(
                "manager_unavailable",
                "Pandrator Manager is not responding.",
            ) from error
        try:
            payload = response.json()
        except ValueError as error:
            raise ManagerProxyError(
                "manager_invalid_response",
                "Pandrator Manager returned an invalid response.",
            ) from error
        if not isinstance(payload, dict):
            raise ManagerProxyError(
                "manager_invalid_response",
                "Pandrator Manager returned an unexpected response.",
            )
        if response.status_code >= 400:
            envelope = payload.get("error")
            if isinstance(envelope, dict):
                raise ManagerProxyError(
                    str(envelope.get("code") or "manager_request_failed"),
                    str(envelope.get("message") or "Manager request failed."),
                    status=response.status_code,
                    details=envelope.get("details"),
                )
            raise ManagerProxyError(
                "manager_request_failed",
                f"Manager request failed ({response.status_code}).",
                status=response.status_code,
            )
        response_instance = str(
            response.headers.get("X-Pandrator-Manager-Instance") or ""
        )
        payload_instance = (
            str(payload.get("instance_id") or "")
            if path == "/v1/health"
            else response_instance
        )
        if not hmac.compare_digest(payload_instance, connection.instance_id):
            raise ManagerProxyError(
                "manager_identity_mismatch",
                "The process answering on the manager port has the wrong identity.",
            )
        return payload, response.status_code

    def inventory(self) -> dict[str, Any]:
        """Return one bounded manager projection for application services."""

        health, _ = self.request_json("GET", "/v1/health", timeout=3)
        status, _ = self.request_json("GET", "/v1/status", timeout=3)
        components, _ = self.request_json("GET", "/v1/components", timeout=10)
        services, _ = self.request_json("GET", "/v1/services", timeout=10)
        return {
            "health": health,
            "status": status,
            "components": list(components.get("items") or []),
            "services": list(services.get("items") or []),
        }

    def managed_service(self, service_id: str) -> dict[str, Any]:
        payload, _ = self.request_json(
            "GET",
            f"/v1/services/{service_id}",
            timeout=5,
        )
        return payload


def register_manager_routes(
    app,
    *,
    require_auth: Callable,
    error_response: Callable,
    proxy: LocalManagerProxy | None = None,
    plan_response_transform: (
        Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None
    ) = None,
) -> None:
    """Register the deliberately narrow browser-to-manager contract."""

    manager = proxy or LocalManagerProxy()

    def failure(error: ManagerProxyError):
        return error_response(
            error.code,
            str(error),
            error.status,
            error.details,
        )

    def mutation_allowed() -> bool:
        return _loopback(request.remote_addr) or os.environ.get(
            "PANDRATOR_ALLOW_REMOTE_MANAGER_MUTATIONS",
            "",
        ).strip().lower() in {"1", "true", "yes", "on"}

    def forward(
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        timeout: float = 30,
        response_transform: (
            Callable[[dict[str, Any]], dict[str, Any]] | None
        ) = None,
    ):
        if method.upper() not in {"GET", "HEAD"} and not mutation_allowed():
            return error_response(
                "local_manager_access_required",
                "Installation and host-management changes are restricted to "
                "a browser on this computer.",
                403,
            )
        try:
            payload, status = manager.request_json(
                method,
                path,
                body=body,
                idempotency_key=request.headers.get("Idempotency-Key"),
                timeout=timeout,
            )
        except ManagerProxyError as error:
            return failure(error)
        if response_transform is not None:
            payload = response_transform(payload)
        return jsonify(payload), status

    @app.get("/api/v1/manager/status")
    @require_auth
    def manager_status():
        try:
            health, _ = manager.request_json("GET", "/v1/health", timeout=3)
            status, _ = manager.request_json("GET", "/v1/status", timeout=3)
        except ManagerProxyError as error:
            return jsonify(
                {
                    "available": False,
                    "configured": manager.configured,
                    "error": {
                        "code": error.code,
                        "message": str(error),
                    },
                }
            )
        return jsonify({"available": True, "health": health, "status": status})

    @app.get("/api/v1/manager/components")
    @require_auth
    def manager_components():
        return forward("GET", "/v1/components")

    @app.get("/api/v1/manager/doctor")
    @require_auth
    def manager_doctor():
        return forward("GET", "/v1/doctor", timeout=120)

    @app.get("/api/v1/manager/legacy")
    @require_auth
    def manager_legacy():
        return forward("GET", "/v1/legacy", timeout=120)

    @app.post("/api/v1/manager/legacy/import")
    @require_auth
    def manager_legacy_import():
        payload = ManagerLegacyImportRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        return forward(
            "POST",
            "/v1/legacy/import",
            body=payload.model_dump(mode="json"),
            timeout=120,
        )

    @app.get("/api/v1/manager/services")
    @require_auth
    def manager_services():
        return forward("GET", "/v1/services")

    @app.get("/api/v1/manager/releases")
    @require_auth
    def manager_releases():
        return forward("GET", "/v1/releases")

    @app.post("/api/v1/manager/releases/plans")
    @require_auth
    def manager_release_plans():
        payload = ManagerReleasePlanRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        return forward(
            "POST",
            "/v1/releases/plans",
            body=payload.model_dump(mode="json", exclude_none=True),
        )

    @app.post("/api/v1/manager/uninstall/plans")
    @require_auth
    def manager_uninstall_plans():
        payload = ManagerUninstallPlanRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        return forward(
            "POST",
            "/v1/uninstall/plans",
            body=payload.model_dump(mode="json", exclude_none=True),
        )

    @app.post("/api/v1/manager/plans")
    @require_auth
    def manager_plans():
        payload = ManagerPlanRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        body = payload.model_dump(mode="json", exclude_none=True)
        return forward(
            "POST",
            "/v1/plans",
            body=body,
            response_transform=(
                (
                    lambda response: plan_response_transform(response, body)
                )
                if plan_response_transform is not None
                else None
            ),
        )

    @app.route("/api/v1/manager/operations", methods=["GET", "POST"])
    @require_auth
    def manager_operations():
        body = None
        if request.method == "POST":
            payload = ManagerOperationRequest.model_validate(
                request.get_json(silent=True) or {}
            )
            body = payload.model_dump(mode="json")
        return forward(
            request.method,
            "/v1/operations",
            body=body,
        )

    @app.get("/api/v1/manager/operations/<operation_id>")
    @require_auth
    def manager_operation(operation_id: str):
        return forward("GET", f"/v1/operations/{operation_id}")

    @app.get("/api/v1/manager/operations/<operation_id>/tasks")
    @require_auth
    def manager_operation_tasks(operation_id: str):
        return forward("GET", f"/v1/operations/{operation_id}/tasks")

    @app.post("/api/v1/manager/operations/<operation_id>/cancel")
    @require_auth
    def manager_operation_cancel(operation_id: str):
        return forward(
            "POST",
            f"/v1/operations/{operation_id}/cancel",
            body={},
        )

    @app.post("/api/v1/manager/runtime/<action>")
    @require_auth
    def manager_runtime(action: str):
        if action not in {"start", "stop", "restart"}:
            return error_response(
                "validation_error",
                "Runtime action must be start, stop, or restart.",
                422,
            )
        payload = ManagerRuntimeRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        return forward(
            "POST",
            f"/v1/runtime/{action}",
            body=payload.model_dump(mode="json"),
            timeout=10 * 60,
        )

    @app.get("/api/v1/manager/logs")
    @require_auth
    def manager_logs():
        service_id = request.args.get("service_id", "")
        maximum = request.args.get("bytes", "65536")
        if not service_id:
            return error_response(
                "validation_error",
                "service_id is required.",
                422,
            )
        from urllib.parse import urlencode

        return forward(
            "GET",
            f"/v1/logs?{urlencode({'service_id': service_id, 'bytes': maximum})}",
        )
