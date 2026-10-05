"""Bounded application API client for one opaque target binding."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
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
from .application_dispatch import ApplicationDispatchMethods
from .application_generation import ApplicationGenerationMethods
from .application_media_edit import ApplicationMediaEditMethods
from .application_projects import ApplicationProjectMethods
from .application_sessions import ApplicationSessionMethods
from .application_sources import ApplicationSourceMethods
from .application_voices import ApplicationVoiceMethods
from .application_work_manager import ApplicationWorkManagerMethods

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
        "structured_speech_source_required",
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
        "materialization_rejected",
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
        "transcription_expired",
        "transcription_limit",
        "invalid_chunk",
        "upload_closed",
        "chunk_conflict",
        "chunk_out_of_order",
        "invalid_chunk_size",
        "upload_incomplete",
        "source_hash_mismatch",
        "invalid_format",
        "invalid_page",
        "result_not_ready",
        "result_unavailable",
        "source_too_large",
        "index_changed",
        "index_unavailable",
        "target_identity_mismatch",
        "target_language_required",
        "unsupported_source_format",
        "validation_error",
    }
)


class ApplicationClient(
    ApplicationDispatchMethods,
    ApplicationVoiceMethods,
    ApplicationSessionMethods,
    ApplicationSourceMethods,
    ApplicationGenerationMethods,
    ApplicationProjectMethods,
    ApplicationMediaEditMethods,
    ApplicationWorkManagerMethods,
):
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
        if_match_revision: int | str | None = None,
        maximum_body_bytes: int = 512 * 1024,
        request_timeout_seconds: float | None = None,
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
        request_timeout = self.timeout_seconds
        if request_timeout_seconds is not None:
            try:
                request_timeout = float(request_timeout_seconds)
            except (TypeError, ValueError) as error:
                raise ValueError("Application request timeouts must be finite numbers.") from error
            if not math.isfinite(request_timeout):
                raise ValueError("Application request timeouts must be finite numbers.")
            request_timeout = max(1.0, min(request_timeout, 120.0))
        if if_match_revision is not None:
            if isinstance(if_match_revision, bool):
                raise ValueError("If-Match revisions must be non-negative integers.")
            if isinstance(if_match_revision, int) and if_match_revision < 0:
                raise ValueError("If-Match revisions must be non-negative integers.")
            if isinstance(if_match_revision, str) and not if_match_revision.strip():
                raise ValueError("If-Match revision IDs must not be blank.")
            if not isinstance(if_match_revision, (int, str)):
                raise ValueError("If-Match must contain a revision number or ID.")
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
                        headers["If-Match"] = f'"{if_match_revision}"'
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
                        "timeout": request_timeout,
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
        except requests.exceptions.Timeout as error:
            is_read = method in {"GET", "HEAD"}
            retry_policy = (
                "safe_to_retry_read"
                if is_read
                else (
                    "same_request_and_idempotency_key"
                    if idempotency_key is not None
                    else "inspect_state"
                )
            )
            raise PandratorMcpError(
                "application_response_timeout",
                (
                    "The Pandrator read request timed out."
                    if is_read
                    else "The Pandrator mutation timed out before a response; its outcome is unknown."
                ),
                details={
                    "timeout_seconds": request_timeout,
                    "operation_outcome": "not_applicable" if is_read else "unknown",
                    "retry_policy": retry_policy,
                },
                retryable=retry_policy != "inspect_state",
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
                    request_timeout_seconds=request_timeout_seconds,
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
        downstream = payload.get("error") if isinstance(payload, dict) else None
        downstream = downstream if isinstance(downstream, dict) else {}
        code = str(downstream.get("code") or "").strip()
        if (
            path.startswith("/api/v1/transcriptions/")
            and code in _PASSTHROUGH_ERROR_CODES
        ):
            details = downstream.get("details")
            details = details if isinstance(details, dict) else {}
            raise PandratorMcpError(
                cast(FailureCode, code),
                str(downstream.get("message") or "Pandrator rejected the request.")[:2_000],
                details={**details, "status": status_code},
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
        if status_code >= 400:
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

    def wait_for_job(
        self,
        work_id: str,
        *,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        """Poll one application job to terminal, preserving its response shape."""

        timeout = max(0.0, min(float(timeout_seconds), 3_600.0))
        started = time.monotonic()
        deadline = started + timeout
        result = self.get_work(work_id)
        while timeout and not self._job_is_terminal(result):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            poll_after_ms = result.get("poll_after_ms", 0)
            try:
                poll_delay = float(poll_after_ms) / 1_000.0
            except (TypeError, ValueError):
                poll_delay = 0.0
            delay = min(max(0.25, min(poll_delay, 10.0)), remaining)
            time.sleep(delay)
            result = self.get_work(work_id)
        return result

    @staticmethod
    def _job_is_terminal(payload: dict[str, Any]) -> bool:
        state = str(payload.get("state") or payload.get("status") or "").strip().lower()
        return state in {"succeeded", "failed", "cancelled", "canceled"}
