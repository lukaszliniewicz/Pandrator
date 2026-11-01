"""Private, single-operation receipts for voice provider jobs.

Only response recording may outlive execution authority. It cannot publish a
binding or change canonical job completion/lease state.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import Database
from .job_execution import current_job_lease
from .models import Job

_IDENTITIES = ("job_id", "workspace_id", "voice_id", "service_id")
_STATES = {"prepared", "dispatching", "uncertain", "completed", "rejected"}
_BINDINGS = {"not_applied", "ready", "stale", "removed"}
_PUBLIC = (
    "operation",
    "state",
    "service_id",
    "provider_voice_id",
    "remote_deleted",
    "binding_state",
)
_RESULT_KEYS = {
    "voice_id",
    "service_id",
    "provider_voice_id",
    "voice_revision",
    "sample_id",
    "artifact_id",
    "sample_sha256",
    "reused_registration",
    "remote_deleted",
    "cancellation_requested_after_upload",
    "cancellation_requested_after_delete",
}
_GOOGLE_RESULT_KEYS = {
    "voice_id",
    "provider_voice_id",
    "service_id",
    "model",
    "voice_revision",
    "artifact_id",
    "warning",
    "usage",
}
_BASE_KEYS = {
    "version",
    "operation",
    *_IDENTITIES,
    "token",
    "lease_generation",
    "request_sha256",
    "baseline_sha256",
    "state",
    "binding_state",
}


class VoiceProviderEffectError(ValueError):
    """A stable failure code for an operation requiring explicit reconciliation."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _identifier(value: object) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= 1024
        and not any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value)
    )


def _digest(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def binding_fingerprint(voice: Any, service_id: str) -> str:
    providers = (voice.metadata_json or {}).get("providers") or {}
    return fingerprint({"present": service_id in providers, "value": providers.get(service_id)})


def _validated(value: object) -> dict[str, Any]:
    if type(value) is not dict:
        raise VoiceProviderEffectError(
            "provider_effect_invalid", "Malformed retained provider receipt."
        )
    extra = {"provider_voice_id", "remote_deleted", "handler_result"}
    if (
        not _BASE_KEYS <= value.keys()
        or not value.keys() <= _BASE_KEYS | extra
        or type(value["version"]) is not int
        or value["version"] != 1
        or type(value["operation"]) is not str
        or value["operation"] not in {"voice.publish", "voice.unpublish", "voice.google_design"}
        or any(not _identifier(value[key]) for key in _IDENTITIES)
        or not _digest(value["token"])
        or not _digest(value["request_sha256"])
        or not _digest(value["baseline_sha256"])
        or type(value["lease_generation"]) is not int
        or value["lease_generation"] < 1
        or type(value["state"]) is not str
        or value["state"] not in _STATES
        or type(value["binding_state"]) is not str
        or value["binding_state"] not in _BINDINGS
        or ("provider_voice_id" in value and not _identifier(value["provider_voice_id"]))
        or ("remote_deleted" in value and type(value["remote_deleted"]) is not bool)
    ):
        raise VoiceProviderEffectError(
            "provider_effect_invalid", "Malformed retained provider receipt."
        )
    completed = value["state"] == "completed"
    applied = value["binding_state"] != "not_applied"
    google = value["operation"] == "voice.google_design"
    if (
        (not completed and (applied or extra & value.keys()))
        or (value["state"] == "rejected" and not google)
        or (google and value["binding_state"] not in {"not_applied", "ready"})
        or (google and "remote_deleted" in value)
        or (completed and not _identifier(value.get("provider_voice_id")))
        or (completed and value["operation"] == "voice.unpublish" and "remote_deleted" not in value)
        or (value["operation"] == "voice.publish" and "remote_deleted" in value)
        or (value["operation"] == "voice.publish" and value["binding_state"] == "removed")
        or (
            value["operation"] == "voice.unpublish" and value["binding_state"] in {"ready", "stale"}
        )
        or (applied != ("handler_result" in value))
    ):
        raise VoiceProviderEffectError(
            "provider_effect_invalid", "Inconsistent retained provider receipt."
        )
    if applied:
        result = value["handler_result"]
        allowed = _GOOGLE_RESULT_KEYS if google else _RESULT_KEYS
        if (
            type(result) is not dict
            or not result.keys() <= allowed
            or any(
                type(v) not in {str, bool, int, type(None)}
                for k, v in result.items()
                if not (google and k == "usage")
            )
            or any(type(v) is str and not _identifier(v) for v in result.values())
            or any(
                key not in result
                or key not in value
                or type(result[key]) is not type(value[key])
                or result[key] != value[key]
                for key in {"voice_id", "service_id", "provider_voice_id", "remote_deleted"}
                & (result.keys() | value.keys())
            )
        ):
            raise VoiceProviderEffectError(
                "provider_effect_invalid", "Invalid retained handler result."
            )
        if google and (
            not _identifier(result.get("model"))
            or type(result.get("voice_revision")) is not int
            or result["voice_revision"] < 1
            or ("artifact_id" in result and not _identifier(result["artifact_id"]))
            or ("warning" in result and not _identifier(result["warning"]))
            or (
                "usage" in result
                and (
                    type(result["usage"]) is not dict
                    or len(result["usage"]) > 20
                    or any(
                        not _identifier(k) or type(v) is not int or not 0 <= v <= 2**63 - 1
                        for k, v in result["usage"].items()
                    )
                )
            )
        ):
            raise VoiceProviderEffectError(
                "provider_effect_invalid", "Invalid Google handler result."
            )
    return deepcopy(value)


def voice_provider_effect_summary(value: object) -> dict[str, Any] | None:
    """Project only validated, bounded receipt fields; never expose private data."""
    if value is None:
        return None
    try:
        receipt = _validated(value)
    except (VoiceProviderEffectError, TypeError, ValueError):
        return None
    return {key: receipt[key] for key in _PUBLIC if key in receipt}


@dataclass(frozen=True)
class VoiceProviderEffect:
    database: Database
    operation: str
    workspace_id: str
    voice_id: str
    service_id: str
    job_id: str | None
    generation: int | None

    @classmethod
    def current(
        cls, database: Database, operation: str, workspace_id: str, voice_id: str, service_id: str
    ) -> VoiceProviderEffect:
        lease = current_job_lease.get()
        return cls(
            database,
            operation,
            workspace_id,
            voice_id,
            service_id,
            lease["job_id"] if lease else None,
            lease["lease_generation"] if lease else None,
        )

    def _job(self, session: Session) -> Job | None:
        if self.job_id is None:
            return None
        job = session.scalar(
            select(Job).where(Job.id == self.job_id).execution_options(populate_existing=True)
        )
        if (
            job is None
            or job.kind != self.operation
            or job.workspace_id != self.workspace_id
            or job.project_id is not None
            or job.session_id is not None
            or job.workflow_run_id is not None
            or type(job.payload_json) is not dict
            or job.payload_json.get("voice_id") != self.voice_id
            or job.payload_json.get("service_id") != self.service_id
            or (
                "workspace_id" in job.payload_json
                and job.payload_json["workspace_id"] != self.workspace_id
            )
        ):
            raise VoiceProviderEffectError(
                "provider_effect_identity", "The original provider job identity changed."
            )
        return job

    def _load(self, job: Job) -> dict[str, Any] | None:
        if job.external_effect_json is None:
            return None
        value = _validated(job.external_effect_json)
        if any(value[key] != getattr(self, key) for key in ("operation", *_IDENTITIES)):
            raise VoiceProviderEffectError(
                "provider_effect_identity",
                "Retained provider receipt belongs to another operation.",
            )
        if (
            self.operation == "voice.google_design"
            and value["binding_state"] == "ready"
            and value["handler_result"]["model"] != job.payload_json.get("model")
        ):
            raise VoiceProviderEffectError(
                "provider_effect_identity", "Google receipt model differs from its job."
            )
        return value

    def previous_result(self, session: Session) -> dict[str, Any] | None:
        job = self._job(session)
        value = self._load(job) if job else None
        if value is None or value["state"] == "prepared":
            return None
        if value["state"] == "rejected":
            raise VoiceProviderEffectError(
                "provider_effect_rejected",
                "Google rejected this creation. Automatic repeat is stopped.",
            )
        if value["state"] != "completed":
            raise VoiceProviderEffectError(
                "provider_effect_uncertain",
                "A provider request may already have happened. Automatic repeat is stopped.",
            )
        if value["binding_state"] == "not_applied":
            raise VoiceProviderEffectError(
                "provider_effect_local_not_applied",
                "A completed provider receipt was retained; no local binding was applied. Reconcile it explicitly.",
            )
        return deepcopy(value["handler_result"])

    def reserve(
        self,
        request: object,
        baseline: str,
        prove: Callable[[Session], object],
        *,
        session: Session | None = None,
    ) -> str | None:
        if session is None:
            with self.database.immediate_session() as current:
                return self.reserve(request, baseline, prove, session=current)
        else:
            prove(session)
            job = self._job(session)
            if job is None:
                return None
            old = self._load(job)
            if old is not None and old["state"] != "prepared":
                raise VoiceProviderEffectError(
                    "provider_effect_uncertain",
                    "A provider operation already exists; automatic repeat is stopped.",
                )
            token = secrets.token_hex(32)
            value = {
                "version": 1,
                "operation": self.operation,
                "job_id": self.job_id,
                "workspace_id": self.workspace_id,
                "voice_id": self.voice_id,
                "service_id": self.service_id,
                "token": token,
                "lease_generation": self.generation,
                "request_sha256": fingerprint(request),
                "baseline_sha256": baseline,
                "state": "prepared",
                "binding_state": "not_applied",
            }
            job.external_effect_json = _validated(value)
            return token

    def dispatch(self, token: str | None, prove: Callable[[Session], object]) -> None:
        with self.database.immediate_session() as session:
            prove(session)
            job = self._job(session)
            if job is None:
                return
            value = self._load(job)
            if (
                value is None
                or value["token"] != token
                or value["state"] not in {"prepared", "dispatching"}
            ):
                raise VoiceProviderEffectError(
                    "provider_effect_superseded",
                    "This provider attempt no longer owns its dispatch intent.",
                )
            value["state"] = "dispatching"
            job.external_effect_json = value

    def failed(self, token: str | None) -> None:
        with self.database.immediate_session() as session:
            job = self._job(session)
            if job is None:
                return
            value = self._load(job)
            if (
                value is None
                or value["token"] != token
                or value["state"] in {"completed", "rejected"}
            ):
                return
            if value["state"] == "prepared":
                job.external_effect_json = None
            else:
                value["state"] = "uncertain"
                job.external_effect_json = value

    def completed(
        self, token: str | None, provider_voice_id: str, *, remote_deleted: bool | None = None
    ) -> None:
        # Recording-only CAS: authority/lease/Voice may have disappeared during IO.
        # The immutable job/root and the original secret token remain mandatory.
        if not _identifier(provider_voice_id) or (
            remote_deleted is not None and type(remote_deleted) is not bool
        ):
            raise VoiceProviderEffectError(
                "provider_effect_invalid", "Provider returned an invalid voice receipt."
            )
        with self.database.immediate_session() as session:
            job = self._job(session)
            if job is None:
                return
            value = self._load(job)
            if (
                value is None
                or value["token"] != token
                or value["state"] not in {"dispatching", "uncertain", "completed"}
            ):
                raise VoiceProviderEffectError(
                    "provider_effect_superseded",
                    "This provider response no longer owns its dispatch intent.",
                )
            if value["state"] == "completed":
                if (
                    value["provider_voice_id"] != provider_voice_id
                    or value.get("remote_deleted") != remote_deleted
                ):
                    raise VoiceProviderEffectError(
                        "provider_effect_identity", "Conflicting completed provider receipt."
                    )
                return
            value.update(state="completed", provider_voice_id=provider_voice_id)
            if remote_deleted is not None:
                value["remote_deleted"] = remote_deleted
            job.external_effect_json = _validated(value)

    def rejected(self, token: str | None, status_code: int) -> None:
        """Record only a known terminal Google rejection under its dispatch token."""
        if (
            self.operation != "voice.google_design"
            or type(status_code) is not int
            or not 400 <= status_code < 500
            or status_code == 408
        ):
            raise VoiceProviderEffectError(
                "provider_effect_invalid", "Not a terminal Google rejection."
            )
        with self.database.immediate_session() as session:
            job = self._job(session)
            if job is None:
                return
            value = self._load(job)
            if (
                value is None
                or value["token"] != token
                or value["state"] not in {"dispatching", "uncertain", "rejected"}
            ):
                raise VoiceProviderEffectError(
                    "provider_effect_superseded", "This rejection does not own its dispatch intent."
                )
            value["state"] = "rejected"
            job.external_effect_json = _validated(value)

    def applied(
        self, session: Session, token: str | None, state: str, result: dict[str, Any]
    ) -> None:
        """Called in the proved binding writer; transaction commits both together."""
        job = self._job(session)
        if job is None:
            return
        value = self._load(job)
        if (
            value is None
            or value["token"] != token
            or value["state"] != "completed"
            or value["binding_state"] != "not_applied"
        ):
            raise VoiceProviderEffectError(
                "provider_effect_superseded", "Provider receipt cannot apply this local binding."
            )
        value.update(binding_state=state, handler_result=deepcopy(result))
        job.external_effect_json = _validated(value)

    def update_result(self, session: Session, token: str | None, result: dict[str, Any]) -> None:
        """Refresh only a currently proved Google receipt's audition result."""
        if self.operation != "voice.google_design":
            raise VoiceProviderEffectError(
                "provider_effect_invalid", "Only Google audition results may be updated."
            )
        job = self._job(session)
        if job is None:
            return
        from .access.voice_sample_execution_policy import require_voice_sample_execution_workspace
        from .voice_provider_settings import check_voice_provider_settings

        payload = {**job.payload_json, "_job_id": job.id, "_lease_generation": self.generation}
        require_voice_sample_execution_workspace(
            session,
            payload,
            "voice.google_design",
            voice_id=self.voice_id,
            expected_workspace_id=self.workspace_id,
        )
        check_voice_provider_settings(session, payload, self.workspace_id)
        value = self._load(job)
        if (
            value is None
            or value["token"] != token
            or value["state"] != "completed"
            or value["binding_state"] != "ready"
        ):
            raise VoiceProviderEffectError(
                "provider_effect_superseded", "Google audition no longer owns the applied receipt."
            )
        if result.get("model") != value["handler_result"].get("model"):
            raise VoiceProviderEffectError(
                "provider_effect_identity", "Google audition model changed."
            )
        value["handler_result"] = deepcopy(result)
        job.external_effect_json = _validated(value)
