"""Exact-origin HTTPS client for direct app-down Manager recovery."""

from __future__ import annotations

import hmac
import json
import re
import threading
from typing import Any
from urllib.parse import quote

import requests

from ..credentials import CredentialResolver
from ..errors import PandratorMcpError
from ..request_context import correlation_headers
from ..targets import TargetBinding
from ..transport import PinnedAddressAdapter

_IDEMPOTENCY_KEY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"
)


class RecoveryManagerGateway:
    """Use only the scoped recovery credential and fixed target binding."""

    def __init__(
        self,
        binding: TargetBinding,
        credentials: CredentialResolver,
        *,
        session: requests.Session | None = None,
        timeout_seconds: float = 30.0,
        maximum_response_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        self.binding = binding
        self.credentials = credentials
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.timeout_seconds = max(
            1.0,
            min(float(timeout_seconds), 10 * 60),
        )
        self.maximum_response_bytes = max(
            64 * 1024,
            min(int(maximum_response_bytes), 16 * 1024 * 1024),
        )
        self._session_lock = threading.RLock()

    @staticmethod
    def _path(value: str) -> str:
        if (
            not value.startswith("/v1/")
            or "://" in value
            or "\\"
            in value
            or "?"
            in value
            or "#"
            in value
            or ".." in value.split("/")
        ):
            raise ValueError(
                "Manager recovery paths must be fixed v1 resources."
            )
        return value

    def _request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        target = self.binding.resolve()
        endpoint = target.manager_recovery
        if endpoint is None:
            raise PandratorMcpError(
                "recovery_enrollment_required",
                "This target has no Manager recovery origin.",
            )
        if endpoint.scheme != "https":
            raise PandratorMcpError(
                "network_policy_denied",
                "Direct Manager recovery requires authenticated HTTPS.",
            )
        selected_method = str(method).upper()
        if selected_method not in {"GET", "POST"}:
            raise ValueError("The Manager recovery method is not allowlisted.")
        if selected_method == "GET" and body is not None:
            raise ValueError("Manager recovery reads cannot include JSON.")
        if idempotency_key is not None and not _IDEMPOTENCY_KEY.fullmatch(
            idempotency_key
        ):
            raise ValueError(
                "Idempotency keys must contain 8-200 safe ASCII characters."
            )
        headers = {
            "Accept": "application/json",
            **correlation_headers(),
        }
        if authenticated:
            reference = target.manager_recovery_credential
            if reference is None:
                raise PandratorMcpError(
                    "recovery_enrollment_required",
                    "Direct Manager recovery is configured but not enrolled.",
                )
            secret = self.credentials.resolve(
                reference,
                audience="manager_recovery",
            )
            headers["Authorization"] = f"Bearer {secret.reveal()}"
        encoded_body = None
        if body is not None:
            try:
                encoded_body = json.dumps(
                    body,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "Manager recovery values must be finite JSON."
                ) from error
            if len(encoded_body) > 512 * 1024:
                raise ValueError(
                    "The Manager recovery request exceeds its body limit."
                )
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        verify: bool | str = endpoint.ca_bundle or True
        proxies = (
            {
                "http": endpoint.proxy_origin,
                "https": endpoint.proxy_origin,
            }
            if endpoint.proxy_origin
            else {}
        )
        url = f"{endpoint.origin}{self._path(path)}"
        try:
            with self._session_lock:
                prior_adapter = self.session.get_adapter(url)
                adapter = PinnedAddressAdapter(
                    endpoint.origin,
                    endpoint.addresses,
                )
                self.session.mount(f"{endpoint.origin}/", adapter)
                try:
                    response = self.session.request(
                        selected_method,
                        url,
                        headers=headers,
                        data=encoded_body,
                        timeout=self.timeout_seconds,
                        allow_redirects=False,
                        stream=True,
                        verify=verify,
                        proxies=proxies,
                    )
                    if 300 <= response.status_code < 400:
                        response.close()
                        raise PandratorMcpError(
                            "network_policy_denied",
                            "Manager recovery redirects are not allowed.",
                        )
                    chunks: list[bytes] = []
                    size = 0
                    try:
                        for chunk in response.iter_content(
                            chunk_size=64 * 1024
                        ):
                            size += len(chunk)
                            if size > self.maximum_response_bytes:
                                raise PandratorMcpError(
                                    "downstream_unavailable",
                                    "The Manager response exceeded the configured size limit.",
                                )
                            chunks.append(chunk)
                        status_code = response.status_code
                        response_instance = str(
                            response.headers.get(
                                "X-Pandrator-Manager-Instance"
                            )
                            or ""
                        )
                    finally:
                        response.close()
                finally:
                    self.session.mount(
                        f"{endpoint.origin}/",
                        prior_adapter,
                    )
                    adapter.close()
        except requests.exceptions.SSLError as error:
            raise PandratorMcpError(
                "tls_validation_failed",
                "The Manager recovery TLS identity could not be validated.",
            ) from error
        except requests.RequestException as error:
            raise PandratorMcpError(
                "manager_unavailable",
                "The Pandrator Manager recovery endpoint is unavailable.",
                retryable=True,
            ) from error
        raw = b"".join(chunks)
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PandratorMcpError(
                "downstream_unavailable",
                "Pandrator Manager returned invalid JSON.",
            ) from error
        if not isinstance(payload, dict):
            raise PandratorMcpError(
                "downstream_unavailable",
                "Pandrator Manager returned an unexpected JSON value.",
            )
        if status_code == 401:
            raise PandratorMcpError(
                "recovery_enrollment_required",
                "The Manager recovery credential is missing, expired, or revoked.",
            )
        if status_code == 403:
            raise PandratorMcpError(
                "scope_denied",
                "The Manager recovery principal lacks the required scope.",
            )
        if status_code == 404:
            raise PandratorMcpError(
                "not_found",
                "The Manager recovery resource was not found.",
            )
        if status_code == 409:
            error = (
                payload.get("error")
                if isinstance(payload.get("error"), dict)
                else {}
            )
            downstream_code = str(error.get("code") or "")
            code = (
                "confirmation_required"
                if downstream_code == "confirmation_required"
                else "revision_conflict"
            )
            raise PandratorMcpError(
                code,
                str(
                    error.get("message")
                    or "The Manager plan or revision changed."
                )[:2_000],
                details={
                    "downstream_code": downstream_code[:160],
                    "status": status_code,
                },
            )
        if status_code == 400:
            raise PandratorMcpError(
                "validation_error",
                "Pandrator Manager rejected one or more request fields.",
                details={"status": status_code},
            )
        if status_code >= 400:
            raise PandratorMcpError(
                "manager_unavailable",
                "Pandrator Manager rejected the recovery request.",
                details={"status": status_code},
                retryable=status_code >= 500,
            )
        expected = target.expected_identity.manager_instance_id
        payload_instance = str(
            payload.get("manager_instance_id")
            or payload.get("instance_id")
            or response_instance
            or ""
        )
        if expected and not hmac.compare_digest(
            payload_instance,
            expected,
        ):
            raise PandratorMcpError(
                "target_identity_mismatch",
                "The Manager recovery instance identity changed.",
            )
        return payload

    def identity(self) -> dict[str, Any]:
        payload = self._request_json(
            "/v1/automation/identity",
            authenticated=False,
        )
        target = self.binding.resolve()
        endpoint = target.manager_recovery
        if (
            payload.get("schema_version") != "1"
            or payload.get("service") != "pandrator-manager"
            or not payload.get("automation_enabled")
            or endpoint is None
            or payload.get("canonical_recovery_origin")
            != endpoint.origin
        ):
            raise PandratorMcpError(
                "target_identity_mismatch",
                "The configured endpoint is not the enrolled Manager recovery service.",
            )
        expected = target.expected_identity.manager_instance_id
        if expected and payload.get("manager_instance_id") != expected:
            raise PandratorMcpError(
                "target_identity_mismatch",
                "The Manager recovery instance identity changed.",
            )
        return payload

    def status(self) -> dict[str, Any]:
        return {
            "available": True,
            "gateway": "direct_recovery",
            "identity": self.identity(),
            "status": self._request_json("/v1/status"),
        }

    def components(self) -> dict[str, Any]:
        return self._request_json("/v1/components")

    def doctor(self) -> dict[str, Any]:
        return self._request_json("/v1/doctor")

    def services(self) -> dict[str, Any]:
        return self._request_json("/v1/services")

    def releases(self) -> dict[str, Any]:
        return self._request_json("/v1/releases")

    def operation(self, operation_id: str) -> dict[str, Any]:
        return self._request_json(
            f"/v1/operations/{quote(operation_id, safe='')}"
        )

    def operation_tasks(self, operation_id: str) -> dict[str, Any]:
        return self._request_json(
            f"/v1/operations/{quote(operation_id, safe='')}/tasks"
        )

    def create_plan(
        self,
        *,
        kind: str,
        desired: dict[str, dict[str, Any]],
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            "/v1/plans",
            method="POST",
            body={
                "kind": kind,
                "desired": desired,
                "expected_revision": expected_revision,
            },
            idempotency_key=idempotency_key,
        )

    def execute_plan(
        self,
        *,
        plan_id: str,
        plan_digest: str,
        accepted_confirmations: tuple[str, ...],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            "/v1/operations",
            method="POST",
            body={
                "plan_id": plan_id,
                "plan_digest": plan_digest,
                "accepted_confirmations": list(
                    accepted_confirmations
                ),
            },
            idempotency_key=idempotency_key,
        )

    def control_runtime(
        self,
        *,
        action: str,
        target: str,
        service_ids: tuple[str, ...],
        idempotency_key: str,
    ) -> dict[str, Any]:
        if action not in {"start", "stop", "restart"}:
            raise ValueError("The Manager runtime action is invalid.")
        if target == "application":
            path = f"/v1/application/{action}"
            body: dict[str, Any] = {}
        elif target == "managed_services":
            path = f"/v1/runtime/{action}"
            body = {"service_ids": list(service_ids)}
        else:
            raise ValueError("The Manager runtime target is invalid.")
        return self._request_json(
            path,
            method="POST",
            body=body,
            idempotency_key=idempotency_key,
        )

    def cancel_operation(
        self,
        operation_id: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/v1/operations/{quote(operation_id, safe='')}/cancel",
            method="POST",
            body={},
            idempotency_key=idempotency_key,
        )
