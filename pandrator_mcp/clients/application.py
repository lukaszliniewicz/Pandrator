"""Bounded application API client for one opaque target binding."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import requests

from ..credentials import CredentialResolver
from ..errors import PandratorMcpError
from ..network_policy import TargetMode, normalize_origin
from ..request_context import correlation_headers
from ..targets import ResolvedTarget, TargetBinding
from ..transport import PinnedAddressAdapter

LocalBootstrap = Callable[
    [ResolvedTarget, requests.Session],
    str | None,
]

_IDEMPOTENCY_KEY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"
)
_PASSTHROUGH_ERROR_CODES = frozenset(
    {
        "confirmation_required",
        "duplicate_session",
        "idempotency_conflict",
        "idempotency_in_progress",
        "idempotency_key_required",
        "not_found",
        "plan_consumed",
        "plan_digest_mismatch",
        "plan_expired",
        "plan_invalid",
        "plan_stale",
        "precondition_required",
        "scope_denied",
        "session_busy",
        "source_hash_unavailable",
        "target_identity_mismatch",
        "validation_error",
    }
)


class ApplicationClient:
    """HTTP-only Pandrator client; endpoints always come from TargetRegistry."""

    def __init__(
        self,
        binding: TargetBinding,
        credentials: CredentialResolver,
        *,
        session: requests.Session | None = None,
        local_bootstrap: LocalBootstrap | None = None,
        timeout_seconds: float = 15.0,
        maximum_response_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        self.binding = binding
        self.credentials = credentials
        self.session = session or requests.Session()
        self.session.trust_env = False
        self._session_lock = threading.RLock()
        self._local_bootstrap = local_bootstrap
        self._local_authenticated_origins: set[str] = set()
        self._local_csrf_tokens: dict[str, str] = {}
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 120.0))
        self.maximum_response_bytes = max(
            64 * 1024,
            min(int(maximum_response_bytes), 16 * 1024 * 1024),
        )

    @staticmethod
    def _url(target: ResolvedTarget, path: str) -> str:
        if (
            not path.startswith("/api/v1/")
            or "://" in path
            or "\\" in path
            or "?" in path
            or "#" in path
            or ".." in path.split("/")
        ):
            raise ValueError("Application paths must be fixed /api/v1 resources.")
        return f"{target.application.origin}{path}"

    def _request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        authenticated: bool = True,
        parameters: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        if_match_revision: int | None = None,
        _allow_local_retry: bool = True,
    ) -> dict[str, Any]:
        method = str(method or "GET").upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError("The application method is not allowlisted.")
        if method in {"GET", "DELETE"} and body is not None:
            raise ValueError(f"{method} application requests cannot include JSON.")
        encoded_body: bytes | None = None
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
                    "Application request values must be finite JSON."
                ) from error
            if len(encoded_body) > 512 * 1024:
                raise ValueError(
                    "The application request exceeds the bounded body limit."
                )
        if idempotency_key is not None and not _IDEMPOTENCY_KEY.fullmatch(
            idempotency_key
        ):
            raise ValueError(
                "Idempotency keys must contain 8-200 safe ASCII characters."
            )
        if (
            if_match_revision is not None
            and (
                isinstance(if_match_revision, bool)
                or int(if_match_revision) < 0
            )
        ):
            raise ValueError(
                "If-Match revisions must be non-negative integers."
            )
        target = self.binding.resolve()
        headers = {
            "Accept": "application/json",
            **correlation_headers(),
        }
        if authenticated:
            reference = target.application_credential
            if reference is not None:
                secret = self.credentials.resolve(
                    reference,
                    audience="application",
                )
                headers["Authorization"] = f"Bearer {secret.reveal()}"
            elif target.mode != TargetMode.LOCAL_MANAGED or self._local_bootstrap is None:
                raise PandratorMcpError(
                    "authentication_required",
                    "This target has no application credential enrollment.",
                )
        verify: bool | str = target.application.ca_bundle or True
        proxies = (
            {
                "http": target.application.proxy_origin,
                "https": target.application.proxy_origin,
            }
            if target.application.proxy_origin
            else {}
        )
        try:
            with self._session_lock:
                url = self._url(target, path)
                adapter: PinnedAddressAdapter | None = None
                prior_adapter = None
                if isinstance(self.session, requests.Session):
                    prior_adapter = self.session.get_adapter(url)
                    adapter = PinnedAddressAdapter(
                        target.application.origin,
                        target.application.addresses,
                    )
                    self.session.mount(f"{target.application.origin}/", adapter)
                try:
                    if (
                        authenticated
                        and target.application_credential is None
                        and target.application.origin not in self._local_authenticated_origins
                    ):
                        csrf_token = self._local_bootstrap(
                            target,
                            self.session,
                        )
                        if csrf_token:
                            self._local_csrf_tokens[
                                target.application.origin
                            ] = csrf_token
                        self._local_authenticated_origins.add(target.application.origin)
                    if encoded_body is not None:
                        headers["Content-Type"] = "application/json"
                    if idempotency_key is not None:
                        headers["Idempotency-Key"] = idempotency_key
                    if if_match_revision is not None:
                        headers["If-Match"] = (
                            f'"{int(if_match_revision)}"'
                        )
                    if (
                        method not in {"GET", "HEAD", "OPTIONS"}
                        and target.application_credential is None
                    ):
                        csrf_token = self._local_csrf_tokens.get(
                            target.application.origin
                        )
                        if csrf_token:
                            headers["X-CSRF-Token"] = csrf_token
                    request_arguments = {
                        "headers": headers,
                        "params": parameters,
                        "timeout": self.timeout_seconds,
                        "allow_redirects": False,
                        "stream": True,
                        "verify": verify,
                        "proxies": proxies,
                    }
                    if method == "GET":
                        response = self.session.get(
                            url,
                            **request_arguments,
                        )
                    else:
                        response = self.session.request(
                            method,
                            url,
                            data=encoded_body,
                            **request_arguments,
                        )
                    if 300 <= response.status_code < 400:
                        response.close()
                        raise PandratorMcpError(
                            "network_policy_denied",
                            "Pandrator target redirects are not allowed.",
                        )
                    chunks: list[bytes] = []
                    size = 0
                    try:
                        for chunk in response.iter_content(chunk_size=64 * 1024):
                            size += len(chunk)
                            if size > self.maximum_response_bytes:
                                raise PandratorMcpError(
                                    "downstream_unavailable",
                                    "The Pandrator response exceeded the configured size limit.",
                                )
                            chunks.append(chunk)
                        status_code = response.status_code
                    finally:
                        response.close()
                finally:
                    if adapter is not None and prior_adapter is not None:
                        self.session.mount(
                            f"{target.application.origin}/",
                            prior_adapter,
                        )
                        adapter.close()
        except requests.exceptions.SSLError as error:
            raise PandratorMcpError(
                "tls_validation_failed",
                "The target's TLS identity could not be validated.",
            ) from error
        except requests.RequestException as error:
            raise PandratorMcpError(
                "application_unavailable",
                "The Pandrator application is unavailable.",
                retryable=True,
            ) from error
        raw = b"".join(chunks)
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PandratorMcpError(
                "downstream_unavailable",
                "Pandrator returned an invalid JSON response.",
            ) from error
        if status_code == 401:
            if (
                authenticated
                and target.mode == TargetMode.LOCAL_MANAGED
                and target.application_credential is None
                and _allow_local_retry
            ):
                with self._session_lock:
                    self._local_authenticated_origins.discard(target.application.origin)
                    self._local_csrf_tokens.pop(
                        target.application.origin,
                        None,
                    )
                return self._request_json(
                    path,
                    method=method,
                    authenticated=authenticated,
                    parameters=parameters,
                    body=body,
                    idempotency_key=idempotency_key,
                    if_match_revision=if_match_revision,
                    _allow_local_retry=False,
                )
            raise PandratorMcpError(
                "authentication_required",
                "The Pandrator application credential was rejected.",
            )
        downstream_error = (
            payload.get("error")
            if isinstance(payload.get("error"), dict)
            else {}
        )
        downstream_code = str(
            downstream_error.get("code") or ""
        ).strip()
        if downstream_code in _PASSTHROUGH_ERROR_CODES:
            details = downstream_error.get("details")
            if not isinstance(details, dict):
                details = {}
            raise PandratorMcpError(
                downstream_code,
                str(
                    downstream_error.get("message")
                    or "Pandrator rejected the request."
                )[:2_000],
                details={
                    **{
                        str(key)[:120]: value
                        for key, value in list(details.items())[:20]
                    },
                    "status": status_code,
                },
                retryable=bool(details.get("retryable")),
            )
        if status_code == 403:
            raise PandratorMcpError(
                "scope_denied",
                "The enrolled application principal lacks the required scope.",
            )
        if status_code == 404:
            raise PandratorMcpError("not_found", "The Pandrator resource was not found.")
        if status_code == 409:
            raise PandratorMcpError(
                "revision_conflict",
                "The Pandrator resource changed since it was inspected.",
                details={"status": status_code},
            )
        if status_code == 422:
            raise PandratorMcpError(
                "validation_error",
                "Pandrator rejected one or more request fields.",
                details={"status": status_code},
            )
        if status_code == 429:
            raise PandratorMcpError(
                "rate_limited",
                "Pandrator is rate limiting this principal.",
                details={"status": status_code},
                retryable=True,
            )
        if status_code >= 400:
            raise PandratorMcpError(
                "downstream_unavailable",
                "Pandrator rejected the request.",
                details={"status": status_code},
                retryable=status_code >= 500,
            )
        if not isinstance(payload, dict):
            raise PandratorMcpError(
                "downstream_unavailable",
                "Pandrator returned an unexpected JSON value.",
            )
        return payload

    def health(self) -> dict[str, Any]:
        return self._request_json("/api/v1/health", authenticated=False)

    def identity(self, *, validate_expected: bool = True) -> dict[str, Any]:
        payload = self._request_json("/api/v1/system/identity")
        target = self.binding.resolve()
        expected = target.expected_identity
        if payload.get("schema_version") != "1" or payload.get("service") != "pandrator":
            raise PandratorMcpError(
                "incompatible_downstream",
                "The application identity contract is missing or incompatible.",
            )
        try:
            actual_origin = normalize_origin(str(payload.get("canonical_origin") or ""))
        except PandratorMcpError as error:
            raise PandratorMcpError(
                "target_identity_mismatch",
                "The Pandrator canonical public origin is invalid.",
            ) from error
        if actual_origin != target.application.origin:
            raise PandratorMcpError(
                "target_identity_mismatch",
                "The Pandrator canonical origin differs from the configured target.",
            )
        if not validate_expected:
            return payload
        if (
            expected.application_instance_id
            and payload.get("instance_id") != expected.application_instance_id
        ):
            raise PandratorMcpError(
                "target_identity_mismatch",
                "The Pandrator application instance has changed.",
            )
        if expected.canonical_application_origin:
            if actual_origin != normalize_origin(expected.canonical_application_origin):
                raise PandratorMcpError(
                    "target_identity_mismatch",
                    "The Pandrator canonical public origin has changed.",
                )
        if (
            expected.manager_instance_id
            and payload.get("manager_instance_id") != expected.manager_instance_id
        ):
            raise PandratorMcpError(
                "target_identity_mismatch",
                "The linked Pandrator Manager instance has changed.",
            )
        if (
            target.discovered_manager_instance_id
            and payload.get("manager_instance_id") != target.discovered_manager_instance_id
        ):
            raise PandratorMcpError(
                "target_identity_mismatch",
                "Pandrator is linked to a different local Manager process.",
            )
        return payload

    def openapi(self) -> dict[str, Any]:
        return self._request_json("/api/v1/openapi.json", authenticated=False)

    def capabilities(self) -> dict[str, Any]:
        return self._request_json("/api/v1/capabilities")

    def target_summary(self) -> dict[str, Any]:
        target = self.binding.resolve()
        return {
            "schema_version": "1",
            "name": target.profile_name,
            "mode": target.mode.value,
            "network_zone": target.application.zone.value,
            "tls": target.application.scheme == "https",
            "explicit_ca": bool(target.application.ca_bundle),
            "explicit_proxy": bool(target.application.proxy_origin),
            "application_credential_configured": (target.application_credential is not None),
            "manager_recovery_configured": (
                target.manager_recovery is not None
                and target.manager_recovery_credential is not None
            ),
            "identity_pinned": bool(target.expected_identity.application_instance_id),
        }

    def list_sessions(self, *, limit: int = 50) -> dict[str, Any]:
        return self._request_json(
            "/api/v1/sessions",
            parameters={"limit": max(1, min(int(limit), 100))},
        )

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/sessions/{quote(session_id, safe='')}")

    def get_workflow(self, session_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/sessions/{quote(session_id, safe='')}/workflow")

    def get_session_settings(
        self,
        session_id: str,
        section: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/settings/"
            f"{quote(section, safe='')}"
        )

    def create_session(
        self,
        *,
        name: str,
        workflow_kind: str,
        source_language: str,
        target_language: str | None,
        workflow_preset: str,
        included_stages: tuple[str, ...],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            "/api/v1/sessions",
            method="POST",
            body={
                "name": name,
                "workflow_kind": workflow_kind,
                "source_language": source_language,
                "target_language": target_language,
                "workflow_preset": workflow_preset,
                "included_stages": list(included_stages),
            },
            idempotency_key=idempotency_key,
        )

    def update_session(
        self,
        session_id: str,
        *,
        expected_revision: int,
        changes: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}",
            method="PATCH",
            body=changes,
            idempotency_key=idempotency_key,
            if_match_revision=expected_revision,
        )

    def list_sources(
        self,
        *,
        include_trashed: bool = False,
    ) -> dict[str, Any]:
        return self._request_json(
            "/api/v1/sources",
            parameters=(
                {"include_trashed": "true"}
                if include_trashed
                else None
            ),
        )

    def attach_existing_source(
        self,
        session_id: str,
        *,
        source_asset_id: str,
        role: str,
        expected_session_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/sources",
            method="POST",
            body={
                "source_asset_id": source_asset_id,
                "role": role,
            },
            idempotency_key=idempotency_key,
            if_match_revision=expected_session_revision,
        )

    def update_session_settings(
        self,
        session_id: str,
        *,
        section: str,
        value: dict[str, Any],
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/settings/"
            f"{quote(section, safe='')}",
            method="PUT",
            body={"value": value},
            idempotency_key=idempotency_key,
            if_match_revision=expected_revision,
        )

    def list_artifacts(
        self,
        *,
        session_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        parameters: dict[str, Any] = {
            "limit": max(1, min(int(limit), 100)),
        }
        if session_id:
            parameters["session_id"] = session_id
        return self._request_json("/api/v1/artifacts", parameters=parameters)

    def list_providers(self) -> dict[str, Any]:
        return self._request_json("/api/v1/providers")

    def list_voices(self) -> dict[str, Any]:
        return self._request_json("/api/v1/voices")

    def list_work(
        self,
        *,
        session_id: str | None = None,
        kinds: tuple[str, ...] = (),
        states: tuple[str, ...] = (),
        limit: int = 50,
    ) -> dict[str, Any]:
        parameters: dict[str, Any] = {"limit": max(1, min(int(limit), 100))}
        if session_id:
            parameters["session_id"] = session_id
        if kinds:
            parameters["kind"] = list(kinds)
        if states:
            parameters["state"] = list(states)
        return self._request_json("/api/v1/work", parameters=parameters)

    def get_work(self, work_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/work/{quote(work_id, safe='')}")

    def get_work_events(
        self,
        work_id: str,
        *,
        after: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/work/{quote(work_id, safe='')}/events",
            parameters={
                "after": max(0, int(after)),
                "limit": max(1, min(int(limit), 200)),
            },
        )

    def create_workflow_plan(
        self,
        session_id: str,
        *,
        target_stage: str,
        overrides: dict[str, Any],
        expires_in_minutes: int,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/workflow-plans",
            method="POST",
            body={
                "target_stage": target_stage,
                "overrides": overrides,
                "expires_in_minutes": max(
                    1,
                    min(int(expires_in_minutes), 60),
                ),
            },
        )

    def get_workflow_plan(self, plan_id: str) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/workflow-plans/{quote(plan_id, safe='')}"
        )

    def execute_workflow_plan(
        self,
        plan_id: str,
        *,
        plan_digest: str,
        accepted_confirmations: tuple[str, ...],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/workflow-plans/{quote(plan_id, safe='')}/execute",
            method="POST",
            body={
                "plan_digest": plan_digest,
                "accepted_confirmations": list(
                    accepted_confirmations
                ),
            },
            idempotency_key=idempotency_key,
        )

    def cancel_work(
        self,
        work_id: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/work/{quote(work_id, safe='')}/cancel",
            method="POST",
            body={},
            idempotency_key=idempotency_key,
        )

    def manager_read(
        self,
        resource: str,
        *,
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        allowed = {
            "status": "/api/v1/manager/status",
            "components": "/api/v1/manager/components",
            "doctor": "/api/v1/manager/doctor",
            "services": "/api/v1/manager/services",
            "releases": "/api/v1/manager/releases",
        }
        try:
            path = allowed[resource]
        except KeyError as error:
            raise ValueError("The Manager proxy resource is not allowlisted.") from error
        return self._request_json(path, parameters=parameters)

    def manager_operation(self, operation_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/manager/operations/{quote(operation_id, safe='')}")

    def manager_operation_tasks(
        self,
        operation_id: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/manager/operations/{quote(operation_id, safe='')}/tasks"
        )

    def manager_create_plan(
        self,
        *,
        kind: str,
        desired: dict[str, dict[str, Any]],
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            "/api/v1/manager/plans",
            method="POST",
            body={
                "kind": kind,
                "desired": desired,
                "expected_revision": expected_revision,
            },
            idempotency_key=idempotency_key,
        )

    def manager_execute_plan(
        self,
        *,
        plan_id: str,
        plan_digest: str,
        accepted_confirmations: tuple[str, ...],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            "/api/v1/manager/operations",
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

    def manager_runtime(
        self,
        *,
        action: str,
        service_ids: tuple[str, ...],
        idempotency_key: str,
    ) -> dict[str, Any]:
        if action not in {"start", "stop", "restart"}:
            raise ValueError("The Manager runtime action is invalid.")
        return self._request_json(
            f"/api/v1/manager/runtime/{action}",
            method="POST",
            body={"service_ids": list(service_ids)},
            idempotency_key=idempotency_key,
        )

    def manager_cancel_operation(
        self,
        operation_id: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/manager/operations/{quote(operation_id, safe='')}/cancel",
            method="POST",
            body={},
            idempotency_key=idempotency_key,
        )
