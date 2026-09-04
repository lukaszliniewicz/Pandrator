"""Bounded application API client for one opaque target binding."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import requests

from ..credentials import CredentialResolver
from ..errors import FailureCode, PandratorMcpError
from ..network_policy import TargetMode, normalize_origin
from ..request_context import correlation_headers
from ..targets import ResolvedTarget, TargetBinding
from ..transport import PinnedAddressAdapter

LocalBootstrap = Callable[
    [ResolvedTarget, requests.Session],
    str | None,
]

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$")
_PASSTHROUGH_ERROR_CODES = frozenset(
    {
        "batch_completed",
        "batch_not_ready",
        "confirmation_required",
        "dispatch_busy",
        "dispatch_sequential",
        "duplicate_session",
        "finalization_conflict",
        "finalization_incomplete",
        "ineligible_source",
        "ineligible_session",
        "invalid_kind",
        "invalid_model_response",
        "invalid_cleanup_result",
        "invalid_output_role",
        "idempotency_conflict",
        "idempotency_in_progress",
        "idempotency_key_required",
        "lease_conflict",
        "lease_expired",
        "materialization_failed",
        "not_found",
        "plan_consumed",
        "plan_digest_mismatch",
        "plan_expired",
        "plan_invalid",
        "plan_stale",
        "precondition_required",
        "preparation_conflict",
        "response_too_large",
        "result_kind_mismatch",
        "result_phase_mismatch",
        "run_not_claimable",
        "run_preparing",
        "run_busy",
        "run_completed",
        "run_failed",
        "run_finalizing",
        "run_not_preparable",
        "scope_denied",
        "session_busy",
        "source_changed",
        "source_empty",
        "source_deleted",
        "source_hash_missing",
        "source_hash_unavailable",
        "source_invalid",
        "source_language_mismatch",
        "source_language_missing",
        "source_not_found",
        "source_revision_missing",
        "source_revision_mismatch",
        "source_segments_invalid",
        "source_session_mismatch",
        "source_unavailable",
        "source_unmaterialized",
        "index_changed",
        "index_unavailable",
        "target_identity_mismatch",
        "target_language_required",
        "unsupported_source_format",
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
        maximum_response_bytes: int = 8 * 1024 * 1024,
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
        parameters: dict[str, Any] | list[tuple[str, Any]] | None = None,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        if_match_revision: int | None = None,
        maximum_body_bytes: int = 512 * 1024,
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
                raise ValueError("Application request values must be finite JSON.") from error
            body_limit = max(
                64 * 1024,
                min(int(maximum_body_bytes), 16 * 1024 * 1024),
            )
            if len(encoded_body) > body_limit:
                raise ValueError("The application request exceeds the bounded body limit.")
        if idempotency_key is not None and not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise ValueError("Idempotency keys must contain 8-200 safe ASCII characters.")
        if if_match_revision is not None and (
            isinstance(if_match_revision, bool) or int(if_match_revision) < 0
        ):
            raise ValueError("If-Match revisions must be non-negative integers.")
        target = self.binding.resolve()
        local_bootstrap = self._local_bootstrap
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
            elif target.mode != TargetMode.LOCAL_MANAGED or local_bootstrap is None:
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
                        if local_bootstrap is None:
                            raise PandratorMcpError(
                                "authentication_required",
                                "This local target cannot bootstrap authentication.",
                            )
                        csrf_token = local_bootstrap(
                            target,
                            self.session,
                        )
                        if csrf_token:
                            self._local_csrf_tokens[target.application.origin] = csrf_token
                        self._local_authenticated_origins.add(target.application.origin)
                    if encoded_body is not None:
                        headers["Content-Type"] = "application/json"
                    if idempotency_key is not None:
                        headers["Idempotency-Key"] = idempotency_key
                    if if_match_revision is not None:
                        headers["If-Match"] = f'"{int(if_match_revision)}"'
                    if (
                        method not in {"GET", "HEAD", "OPTIONS"}
                        and target.application_credential is None
                    ):
                        csrf_token = self._local_csrf_tokens.get(target.application.origin)
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
                                    "response_too_large",
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
                    maximum_body_bytes=maximum_body_bytes,
                    _allow_local_retry=False,
                )
            raise PandratorMcpError(
                "authentication_required",
                "The Pandrator application credential was rejected.",
            )
        raw_downstream_error = payload.get("error")
        downstream_error: dict[str, Any] = (
            raw_downstream_error if isinstance(raw_downstream_error, dict) else {}
        )
        downstream_code = str(downstream_error.get("code") or "").strip()
        if downstream_code in _PASSTHROUGH_ERROR_CODES:
            details = downstream_error.get("details")
            if not isinstance(details, dict):
                details = {}
            raise PandratorMcpError(
                cast(FailureCode, downstream_code),
                str(downstream_error.get("message") or "Pandrator rejected the request.")[:2_000],
                details={
                    **{str(key)[:120]: value for key, value in list(details.items())[:20]},
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

    def _request_binary_json(
        self,
        path: str,
        *,
        method: str,
        body: bytes,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
        _allow_local_retry: bool = True,
    ) -> dict[str, Any]:
        """Send one bounded binary body through the normal target boundary."""

        if method.upper() != "PUT":
            raise ValueError("Binary application requests are limited to PUT.")
        if len(body) > 16 * 1024 * 1024:
            raise ValueError("Binary application request exceeds 16 MiB.")
        target = self.binding.resolve()
        headers = {
            "Accept": "application/json",
            "Content-Type": content_type,
            **correlation_headers(),
            **(extra_headers or {}),
        }
        reference = target.application_credential
        if reference is not None:
            secret = self.credentials.resolve(reference, audience="application")
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
                        target.application_credential is None
                        and target.application.origin not in self._local_authenticated_origins
                    ):
                        assert self._local_bootstrap is not None
                        csrf_token = self._local_bootstrap(target, self.session)
                        if csrf_token:
                            self._local_csrf_tokens[target.application.origin] = csrf_token
                        self._local_authenticated_origins.add(target.application.origin)
                    csrf_token = self._local_csrf_tokens.get(target.application.origin)
                    if target.application_credential is None and csrf_token:
                        headers["X-CSRF-Token"] = csrf_token
                    response = self.session.put(
                        url,
                        data=body,
                        headers=headers,
                        timeout=max(self.timeout_seconds, 120.0),
                        allow_redirects=False,
                        stream=True,
                        verify=verify,
                        proxies=proxies,
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
                                    "response_too_large",
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
        if status_code == 401 and target.mode == TargetMode.LOCAL_MANAGED and _allow_local_retry:
            with self._session_lock:
                self._local_authenticated_origins.discard(target.application.origin)
                self._local_csrf_tokens.pop(target.application.origin, None)
            return self._request_binary_json(
                path,
                method=method,
                body=body,
                content_type=content_type,
                extra_headers=extra_headers,
                _allow_local_retry=False,
            )
        if status_code == 401:
            raise PandratorMcpError(
                "authentication_required",
                "The Pandrator application credential was rejected.",
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
        if status_code >= 400:
            downstream = payload.get("error") if isinstance(payload, dict) else None
            downstream = downstream if isinstance(downstream, dict) else {}
            code = str(downstream.get("code") or "")
            if code in _PASSTHROUGH_ERROR_CODES:
                details = downstream.get("details")
                details = details if isinstance(details, dict) else {}
                raise PandratorMcpError(
                    cast(FailureCode, code),
                    str(downstream.get("message") or "Pandrator rejected the request.")[:2_000],
                    details={**details, "status": status_code},
                    retryable=bool(details.get("retryable")),
                )
            raise PandratorMcpError(
                "downstream_unavailable",
                "Pandrator rejected the binary request.",
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
        if target.mode == TargetMode.LOCAL_MANAGED:
            if not payload.get("managed") or not payload.get("manager_instance_id"):
                raise PandratorMcpError(
                    "target_identity_mismatch",
                    "The local Pandrator process is not running under Manager control.",
                )
        elif actual_origin != target.application.origin:
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

    def auth_status(self) -> dict[str, Any]:
        return self._request_json("/api/v1/auth/status")

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

    def list_sessions(
        self,
        *,
        limit: int = 50,
        query: str | None = None,
    ) -> dict[str, Any]:
        parameters: dict[str, Any] = {"limit": max(1, min(int(limit), 100))}
        if query and query.strip():
            parameters["q"] = query.strip()
        return self._request_json(
            "/api/v1/sessions",
            parameters=parameters,
        )

    def create_dispatch_run(
        self,
        session_id: str,
        *,
        kind: str,
        source_artifact_id: str | None,
        source_language: str | None,
        target_language: str | None,
        instructions: str,
        char_limit: int,
        max_segments_per_batch: int,
        no_remove_subtitles: bool,
        context_before: int = 8,
        context_after: int = 2,
        timing_context_mode: str | None = None,
        include_timing_context: bool | None = None,
        substantial_gap_ms: int,
        glossary: dict[str, str],
        execution_mode: str = "serial",
        max_parallel_batches: int = 1,
        context_capsule: dict[str, Any] | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        resolved_timing_context_mode = timing_context_mode
        if resolved_timing_context_mode is None:
            resolved_timing_context_mode = "full" if include_timing_context is not False else "none"
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/dispatch-runs",
            method="POST",
            body={
                "kind": kind,
                "source_artifact_id": source_artifact_id,
                "source_language": source_language,
                "target_language": target_language,
                "instructions": instructions,
                "char_limit": int(char_limit),
                "max_segments_per_batch": int(max_segments_per_batch),
                "no_remove_subtitles": bool(no_remove_subtitles),
                "context_before": int(context_before),
                "context_after": int(context_after),
                "timing_context_mode": resolved_timing_context_mode,
                "substantial_gap_ms": int(substantial_gap_ms),
                "glossary": glossary,
                "execution_mode": execution_mode,
                "max_parallel_batches": int(max_parallel_batches),
                "context_capsule": dict(context_capsule or {}),
            },
            idempotency_key=idempotency_key,
        )

    def list_dispatch_runs(
        self,
        session_id: str,
        *,
        limit: int = 50,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/dispatch-runs",
            parameters={"limit": max(1, min(int(limit), 100))},
        )

    def get_dispatch_run(self, run_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/dispatch-runs/{quote(run_id, safe='')}")

    def claim_dispatch_batch(
        self,
        run_id: str,
        *,
        lease_seconds: int = 900,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/dispatch-runs/{quote(run_id, safe='')}/claim",
            method="POST",
            body={"lease_seconds": int(lease_seconds)},
            idempotency_key=idempotency_key,
        )

    def renew_dispatch_batch(
        self,
        batch_id: str,
        *,
        lease_token: str,
        lease_seconds: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/dispatch-batches/{quote(batch_id, safe='')}/renew",
            method="POST",
            body={
                "lease_token": lease_token,
                "lease_seconds": int(lease_seconds),
            },
            idempotency_key=idempotency_key,
        )

    def release_dispatch_batch(
        self,
        batch_id: str,
        *,
        lease_token: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/dispatch-batches/{quote(batch_id, safe='')}/release",
            method="POST",
            body={"lease_token": lease_token},
            idempotency_key=idempotency_key,
        )

    def submit_dispatch_batch(
        self,
        batch_id: str,
        *,
        lease_token: str,
        result: dict[str, Any] | None = None,
        response_text: str | None = None,
        context_delta: dict[str, Any] | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"lease_token": lease_token}
        if result is not None:
            body["result"] = result
        if response_text is not None:
            body["response_text"] = response_text
        body["context_delta"] = dict(context_delta or {})
        return self._request_json(
            f"/api/v1/dispatch-batches/{quote(batch_id, safe='')}/submit",
            method="POST",
            body=body,
            idempotency_key=idempotency_key,
            # JSON string escaping can make a valid 512 KiB UTF-8 response
            # larger on the wire while the backend still enforces the exact
            # decoded response limit.
            maximum_body_bytes=4 * 1024 * 1024,
        )

    def create_source_cleaning_dispatch_run(
        self,
        session_id: str,
        *,
        source_artifact_id: str | None,
        instructions: str,
        evidence_limit: int,
        remove_footnotes: bool | None,
        filter_citations: bool | None,
        pdf_ocr_mode: str | None,
        pdf_ocr_language: str | None,
        pdf_ocr_dpi: int | None,
        pdf_remove_toc: bool | None,
        pdf_remove_repeated_marginals: bool | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "source_artifact_id": source_artifact_id,
            "instructions": instructions,
            "evidence_limit": int(evidence_limit),
        }
        optional = {
            "remove_footnotes": remove_footnotes,
            "filter_citations": filter_citations,
            "pdf_ocr_mode": pdf_ocr_mode,
            "pdf_ocr_language": pdf_ocr_language,
            "pdf_ocr_dpi": pdf_ocr_dpi,
            "pdf_remove_toc": pdf_remove_toc,
            "pdf_remove_repeated_marginals": pdf_remove_repeated_marginals,
        }
        body.update({key: value for key, value in optional.items() if value is not None})
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/source-cleaning-dispatch-runs",
            method="POST",
            body=body,
            idempotency_key=idempotency_key,
        )

    def list_source_cleaning_dispatch_runs(
        self,
        session_id: str,
        *,
        limit: int = 50,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/source-cleaning-dispatch-runs",
            parameters={"limit": max(1, min(int(limit), 100))},
        )

    def get_source_cleaning_dispatch_run(self, run_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/source-cleaning-dispatch-runs/{quote(run_id, safe='')}")

    def claim_source_cleaning_dispatch_batch(
        self,
        run_id: str,
        *,
        lease_seconds: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/source-cleaning-dispatch-runs/{quote(run_id, safe='')}/claim",
            method="POST",
            body={"lease_seconds": int(lease_seconds)},
            idempotency_key=idempotency_key,
        )

    def renew_source_cleaning_dispatch_batch(
        self,
        batch_id: str,
        *,
        lease_token: str,
        lease_seconds: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/source-cleaning-dispatch-batches/{quote(batch_id, safe='')}/renew",
            method="POST",
            body={
                "lease_token": lease_token,
                "lease_seconds": int(lease_seconds),
            },
            idempotency_key=idempotency_key,
        )

    def release_source_cleaning_dispatch_batch(
        self,
        batch_id: str,
        *,
        lease_token: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/source-cleaning-dispatch-batches/{quote(batch_id, safe='')}/release",
            method="POST",
            body={"lease_token": lease_token},
            idempotency_key=idempotency_key,
        )

    def inspect_source_cleaning_dispatch_extraction(
        self,
        batch_id: str,
        *,
        lease_token: str,
        action: str,
        arguments: dict[str, Any],
        view: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/source-cleaning-dispatch-batches/{quote(batch_id, safe='')}/inspect",
            method="POST",
            body={
                "lease_token": lease_token,
                "action": action,
                "arguments": arguments,
                "view": view,
            },
            idempotency_key=idempotency_key,
            maximum_body_bytes=16 * 1024 * 1024,
        )

    def submit_source_cleaning_dispatch_batch(
        self,
        batch_id: str,
        *,
        lease_token: str,
        result: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/source-cleaning-dispatch-batches/{quote(batch_id, safe='')}/submit",
            method="POST",
            body={"lease_token": lease_token, "result": result},
            idempotency_key=idempotency_key,
            maximum_body_bytes=16 * 1024 * 1024,
        )

    def create_speech_optimization_dispatch_run(
        self,
        session_id: str,
        *,
        source_artifact_id: str | None,
        language: str | None,
        voice_language: str | None,
        tts_service: str | None,
        instructions: str,
        char_limit: int,
        max_units_per_batch: int,
        context_before: int,
        context_after: int,
        include_timing: bool,
        execution_mode: str = "serial",
        max_parallel_batches: int = 1,
        context_capsule: dict[str, Any] | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "instructions": instructions,
            "char_limit": int(char_limit),
            "max_units_per_batch": int(max_units_per_batch),
            "context_before": int(context_before),
            "context_after": int(context_after),
            "include_timing": bool(include_timing),
            "execution_mode": execution_mode,
            "max_parallel_batches": int(max_parallel_batches),
            "context_capsule": dict(context_capsule or {}),
        }
        optional = {
            "source_artifact_id": source_artifact_id,
            "language": language,
            "voice_language": voice_language,
            "tts_service": tts_service,
        }
        body.update({key: value for key, value in optional.items() if value is not None})
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/speech-optimization-dispatch-runs",
            method="POST",
            body=body,
            idempotency_key=idempotency_key,
        )

    def list_speech_optimization_dispatch_runs(
        self,
        session_id: str,
        *,
        limit: int = 50,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/speech-optimization-dispatch-runs",
            parameters={"limit": max(1, min(int(limit), 100))},
        )

    def get_speech_optimization_dispatch_run(self, run_id: str) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/speech-optimization-dispatch-runs/{quote(run_id, safe='')}"
        )

    def claim_speech_optimization_dispatch_batch(
        self,
        run_id: str,
        *,
        lease_seconds: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/speech-optimization-dispatch-runs/{quote(run_id, safe='')}/claim",
            method="POST",
            body={"lease_seconds": int(lease_seconds)},
            idempotency_key=idempotency_key,
        )

    def renew_speech_optimization_dispatch_batch(
        self,
        batch_id: str,
        *,
        lease_token: str,
        lease_seconds: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/speech-optimization-dispatch-batches/{quote(batch_id, safe='')}/renew",
            method="POST",
            body={
                "lease_token": lease_token,
                "lease_seconds": int(lease_seconds),
            },
            idempotency_key=idempotency_key,
        )

    def release_speech_optimization_dispatch_batch(
        self,
        batch_id: str,
        *,
        lease_token: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/speech-optimization-dispatch-batches/{quote(batch_id, safe='')}/release",
            method="POST",
            body={"lease_token": lease_token},
            idempotency_key=idempotency_key,
        )

    def submit_speech_optimization_dispatch_batch(
        self,
        batch_id: str,
        *,
        lease_token: str,
        result: dict[str, Any],
        context_delta: dict[str, Any] | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/speech-optimization-dispatch-batches/{quote(batch_id, safe='')}/submit",
            method="POST",
            body={
                "lease_token": lease_token,
                "result": result,
                "context_delta": dict(context_delta or {}),
            },
            idempotency_key=idempotency_key,
            maximum_body_bytes=4 * 1024 * 1024,
        )

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/sessions/{quote(session_id, safe='')}")

    def get_workflow(self, session_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/sessions/{quote(session_id, safe='')}/workflow")

    def get_subtitles(self, session_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/sessions/{quote(session_id, safe='')}/subtitles")

    def review_subtitles(
        self,
        session_id: str,
        *,
        artifact_ids: list[str] | tuple[str, ...],
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/subtitles/review",
            parameters=[("artifact_id", item) for item in artifact_ids],
        )

    def save_subtitle_review(
        self,
        session_id: str,
        stage: str,
        *,
        expected_revision: int,
        segments: list[dict[str, Any]],
        source_artifact_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "expected_revision": expected_revision,
            "segments": segments,
        }
        if source_artifact_id:
            payload["source_artifact_id"] = source_artifact_id
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/subtitles/{quote(stage, safe='')}/review",
            method="POST",
            body=payload,
            idempotency_key=idempotency_key,
        )

    def get_session_settings(
        self,
        session_id: str,
        section: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/settings/{quote(section, safe='')}"
        )

    def describe_parameters(
        self,
        *,
        sections: tuple[str, ...] = (),
        names: tuple[str, ...] = (),
        workflow_kind: str | None = None,
        query: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return filtered parameter definitions from the application."""

        parameters: list[tuple[str, Any]] = [
            *(("section", section) for section in sections),
            *(("name", name) for name in names),
        ]
        if workflow_kind:
            parameters.append(("workflow_kind", workflow_kind))
        if query:
            parameters.append(("query", query))
        parameters.append(("limit", max(1, min(int(limit), 300))))
        return self._request_json(
            "/api/v1/parameter-definitions",
            parameters=parameters,
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
            parameters=({"include_trashed": "true"} if include_trashed else None),
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
            f"/api/v1/sessions/{quote(session_id, safe='')}/settings/{quote(section, safe='')}",
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

    def artifact_context(self, artifact_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/artifacts/{quote(artifact_id, safe='')}/context")

    def initialize_upload(
        self,
        *,
        filename: str,
        size_bytes: int,
        mime_type: str | None,
        sha256: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            "/api/v1/uploads/init",
            method="POST",
            body={
                "filename": filename,
                "size_bytes": int(size_bytes),
                "mime_type": mime_type,
                "sha256": sha256,
            },
            idempotency_key=idempotency_key,
        )

    def upload_status(self, upload_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/uploads/{quote(upload_id, safe='')}")

    def upload_chunk(
        self,
        upload_id: str,
        index: int,
        body: bytes,
        *,
        sha256: str,
    ) -> dict[str, Any]:
        return self._request_binary_json(
            f"/api/v1/uploads/{quote(upload_id, safe='')}/chunks/{int(index)}",
            method="PUT",
            body=body,
            content_type="application/octet-stream",
            extra_headers={"X-Chunk-SHA256": sha256},
        )

    def complete_upload(self, upload_id: str) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/uploads/{quote(upload_id, safe='')}/complete",
            method="POST",
            body={},
        )

    def tts_catalog(self, *, refresh: bool = False) -> dict[str, Any]:
        return self._request_json(
            "/api/v1/services/tts",
            parameters={"refresh": "true"} if refresh else None,
        )

    def list_generation_runs(self, session_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/sessions/{quote(session_id, safe='')}/generation-runs")

    def list_generation_segments(
        self,
        session_id: str,
        *,
        cursor: int = 0,
        limit: int = 50,
        generation_run_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "cursor": max(0, int(cursor)),
            "limit": max(1, min(int(limit), 100)),
        }
        if generation_run_id:
            params["generation_run_id"] = generation_run_id
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/generation-segments",
            parameters=params,
        )

    def update_generation_segment(
        self,
        segment_id: str,
        *,
        changes: dict[str, Any],
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/generation-segments/{quote(segment_id, safe='')}",
            method="PATCH",
            body=changes,
            if_match_revision=expected_revision,
            idempotency_key=idempotency_key,
        )

    def select_generation_take(
        self,
        segment_id: str,
        take_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/generation-segments/{quote(segment_id, safe='')}/takes/{quote(take_id, safe='')}/select",
            method="POST",
            if_match_revision=expected_revision,
            idempotency_key=idempotency_key,
        )

    def start_generation_run(
        self,
        session_id: str,
        *,
        segment_ids: list[str] | tuple[str, ...] | None = None,
        operation: str = "generate",
        idempotency_key: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"operation": operation}
        if segment_ids:
            body["segment_ids"] = list(segment_ids)
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/generation-runs",
            method="POST",
            body=body,
            idempotency_key=idempotency_key,
        )

    def create_output_assembly(
        self,
        session_id: str,
        *,
        generation_run_id: str | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if generation_run_id:
            body["generation_run_id"] = generation_run_id
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/output-assemblies",
            method="POST",
            body=body,
            idempotency_key=idempotency_key,
        )

    def download_artifact(
        self,
        artifact_id: str,
        destination: Path,
        *,
        expected_size: int,
        expected_hash: str | None,
        _allow_local_retry: bool = True,
    ) -> dict[str, Any]:
        """Resume one immutable artifact into a caller-approved local path."""

        partial = destination.with_name(f".{destination.name}.part")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink() or partial.is_symlink():
            raise PandratorMcpError(
                "network_policy_denied",
                "Local artifact destinations may not be symbolic links.",
            )
        if destination.is_file():
            size = destination.stat().st_size
            digest = self._sha256_path(destination)
            if size == expected_size and (not expected_hash or digest == expected_hash):
                return {
                    "path": str(destination),
                    "size_bytes": size,
                    "sha256": digest,
                    "resumed": False,
                    "reused": True,
                }
            raise PandratorMcpError(
                "revision_conflict",
                "The local output path already contains different content.",
            )
        if destination.exists() and not destination.is_file():
            raise PandratorMcpError(
                "revision_conflict",
                "The local output path is not a regular file destination.",
            )
        if partial.exists() and not partial.is_file():
            raise PandratorMcpError(
                "revision_conflict",
                "The resumable local output path is not a regular file.",
            )
        offset = partial.stat().st_size if partial.is_file() else 0
        if offset > expected_size:
            partial.unlink(missing_ok=True)
            offset = 0
        target = self.binding.resolve()
        headers = {"Accept": "application/octet-stream", **correlation_headers()}
        if offset:
            headers["Range"] = f"bytes={offset}-"
            if expected_hash:
                headers["If-Range"] = f'"{expected_hash}"'
        reference = target.application_credential
        if reference is not None:
            secret = self.credentials.resolve(reference, audience="application")
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
        url = self._url(
            target,
            f"/api/v1/artifacts/{quote(artifact_id, safe='')}/content",
        )
        try:
            with self._session_lock:
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
                        target.application_credential is None
                        and target.application.origin not in self._local_authenticated_origins
                    ):
                        assert self._local_bootstrap is not None
                        csrf_token = self._local_bootstrap(target, self.session)
                        if csrf_token:
                            self._local_csrf_tokens[target.application.origin] = csrf_token
                        self._local_authenticated_origins.add(target.application.origin)
                    response = self.session.get(
                        url,
                        headers=headers,
                        timeout=max(self.timeout_seconds, 120.0),
                        allow_redirects=False,
                        stream=True,
                        verify=verify,
                        proxies=proxies,
                    )
                    if 300 <= response.status_code < 400:
                        response.close()
                        raise PandratorMcpError(
                            "network_policy_denied",
                            "Pandrator target redirects are not allowed.",
                        )
                    if (
                        response.status_code == 401
                        and target.mode == TargetMode.LOCAL_MANAGED
                        and target.application_credential is None
                        and _allow_local_retry
                    ):
                        response.close()
                        self._local_authenticated_origins.discard(target.application.origin)
                        self._local_csrf_tokens.pop(
                            target.application.origin,
                            None,
                        )
                        return self.download_artifact(
                            artifact_id,
                            destination,
                            expected_size=expected_size,
                            expected_hash=expected_hash,
                            _allow_local_retry=False,
                        )
                    if response.status_code not in {200, 206}:
                        status = response.status_code
                        response.close()
                        if status == 401:
                            raise PandratorMcpError(
                                "authentication_required",
                                "The Pandrator application credential was rejected.",
                            )
                        if status == 403:
                            raise PandratorMcpError(
                                "scope_denied",
                                "The enrolled application principal lacks the required scope.",
                            )
                        if status == 404:
                            raise PandratorMcpError(
                                "not_found",
                                "The requested artifact was not found.",
                            )
                        raise PandratorMcpError(
                            "downstream_unavailable",
                            "Pandrator could not stream the requested artifact.",
                            details={"status": status},
                            retryable=status >= 500,
                        )
                    append = response.status_code == 206 and offset > 0
                    if not append:
                        offset = 0
                    flags = (
                        os.O_WRONLY
                        | os.O_CREAT
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0)
                        | (os.O_APPEND if append else os.O_TRUNC)
                    )
                    try:
                        descriptor = os.open(partial, flags, 0o600)
                        with os.fdopen(descriptor, "ab" if append else "wb") as output:
                            for chunk in response.iter_content(chunk_size=1024 * 1024):
                                if chunk:
                                    output.write(chunk)
                            output.flush()
                            os.fsync(output.fileno())
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
                "The artifact download was interrupted and can be resumed.",
                retryable=True,
            ) from error
        size = partial.stat().st_size
        digest = self._sha256_path(partial)
        if size != expected_size or (expected_hash and digest != expected_hash):
            raise PandratorMcpError(
                "source_changed",
                "The downloaded artifact did not match its immutable metadata.",
                details={
                    "expected_size": expected_size,
                    "actual_size": size,
                },
                retryable=size < expected_size,
            )
        os.replace(partial, destination)
        return {
            "path": str(destination),
            "size_bytes": size,
            "sha256": digest,
            "resumed": offset > 0,
            "reused": False,
        }

    @staticmethod
    def _sha256_path(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

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
        continuation: bool = True,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/workflow-plans",
            method="POST",
            body={
                "target_stage": target_stage,
                "overrides": overrides,
                "continuation": bool(continuation),
                "expires_in_minutes": max(
                    1,
                    min(int(expires_in_minutes), 60),
                ),
            },
        )

    def get_workflow_plan(self, plan_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/workflow-plans/{quote(plan_id, safe='')}")

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
                "accepted_confirmations": list(accepted_confirmations),
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
                "accepted_confirmations": list(accepted_confirmations),
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
