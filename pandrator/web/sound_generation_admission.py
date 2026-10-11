"""Generation request validation, deduplication, and durable job admission."""

from __future__ import annotations

import math
import os
import secrets
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from numbers import Real
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from pandrator.logic import audio_cpp_sfx
from pandrator.runtime import DataPaths

from .jobs import JobQueue
from .models import Artifact, Job, SoundVariant
from .sound_schemas import DEFAULT_SOUND_MODEL_ID, SoundGenerate
from .stt_resources import audio_cpp_resource_keys

SettingsProvider = Callable[[], Mapping[str, Any]]
_SETTINGS_FIELDS = frozenset(
    {"audio_cpp_executable", "audio_cpp_cache_dir", "audio_cpp_timeout_seconds"}
)
_ACTIVE_JOB_STATUSES = frozenset({"queued", "running", "cancel_requested"})


@dataclass(frozen=True, slots=True)
class PreparedSoundGeneration:
    request_template: dict[str, Any]
    runtime_identity: dict[str, Any]
    candidate_count: int = 1


def runtime_settings(settings_provider: SettingsProvider | None) -> dict[str, Any]:
    """Read only the three operator-controlled native runtime settings."""
    if settings_provider is None:
        return {}
    supplied = settings_provider()
    if not isinstance(supplied, Mapping):
        raise TypeError("The sound runtime settings provider must return a mapping.")
    settings: dict[str, Any] = {}
    for key in _SETTINGS_FIELDS:
        value = supplied.get(key)
        if value is None:
            continue
        if key == "audio_cpp_timeout_seconds":
            if isinstance(value, bool) or not isinstance(value, Real):
                raise ValueError("audio_cpp_timeout_seconds must be a positive finite number.")
            timeout = float(value)
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError("audio_cpp_timeout_seconds must be a positive finite number.")
            settings[key] = timeout
        else:
            if not isinstance(value, (str, os.PathLike)):
                raise ValueError(f"{key} must be a path string.")
            path = os.fspath(value)
            if not path:
                raise ValueError(f"{key} must be a non-empty path.")
            settings[key] = path
    return settings


def prepare_generation(
    payload: dict[str, Any],
    settings: dict[str, Any],
) -> PreparedSoundGeneration:
    """Validate public request fields and identify the runtime before writing."""
    request = SoundGenerate.model_validate(payload)
    raw_request = _public_request_payload(request)
    canonical = audio_cpp_sfx.validate_request(raw_request, resolved_seed=0)
    if request.seed is None:
        canonical.pop("seed")
    audio_cpp_sfx.require_backend(settings, canonical["backend"])
    identity = _runtime_identity(settings, canonical["model_id"])
    return PreparedSoundGeneration(
        request_template=deepcopy(canonical),
        runtime_identity=deepcopy(identity),
        candidate_count=request.candidate_count,
    )


def _runtime_identity(settings: dict[str, Any], model_id: str) -> dict[str, Any]:
    if model_id == DEFAULT_SOUND_MODEL_ID:
        return audio_cpp_sfx.runtime_identity(settings)
    return audio_cpp_sfx.runtime_identity(settings, model_id=model_id)


def _public_request_payload(request: SoundGenerate) -> dict[str, Any]:
    payload = request.model_dump(exclude_none=True)
    payload.pop("candidate_count", None)
    if "backend" not in request.model_fields_set:
        payload.pop("backend", None)
    return payload


def _same_prepared_request(payload: dict[str, Any], prepared: PreparedSoundGeneration) -> None:
    request = SoundGenerate.model_validate(payload)
    if request.candidate_count != prepared.candidate_count:
        raise ValueError("Prepared sound generation count does not match the submitted request.")
    canonical = audio_cpp_sfx.validate_request(
        _public_request_payload(request), resolved_seed=0
    )
    if request.seed is None:
        canonical.pop("seed")
    if canonical != prepared.request_template:
        raise ValueError("Prepared sound generation does not match the submitted request.")


def _resolved_request(
    payload: dict[str, Any],
    prepared: PreparedSoundGeneration,
    *,
    seed_maximum: int | None = None,
) -> dict[str, Any]:
    _same_prepared_request(payload, prepared)
    request = deepcopy(prepared.request_template)
    if "seed" not in request:
        maximum = (
            audio_cpp_sfx.seed_maximum(request["model_id"])
            if seed_maximum is None
            else seed_maximum
        )
        if type(maximum) is not int or not 0 <= maximum <= 2**32 - 1:
            raise ValueError("The sound model returned an invalid seed maximum.")
        request["seed"] = (
            secrets.randbits(32)
            if maximum == 2**32 - 1
            else secrets.randbelow(maximum + 1)
        )
    return audio_cpp_sfx.validate_request(request)


def _resolved_settings(request: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(request)


def _valid_cached_artifact(
    session: Session,
    paths: DataPaths,
    variant: SoundVariant,
) -> bool:
    if variant.state != "ready" or not variant.artifact_id:
        return False
    artifact = session.get(Artifact, variant.artifact_id)
    if (
        artifact is None
        or artifact.kind != "audio"
        or artifact.role != "sound.original"
        or artifact.session_id is not None
        or artifact.state != "current"
        or not artifact.content_hash
    ):
        return False
    try:
        return paths.managed_path(artifact.relative_path).is_file()
    except (OSError, ValueError):
        return False


def _cache_hit(
    session: Session,
    paths: DataPaths,
    fingerprint: str,
    request: dict[str, Any],
) -> dict[str, Any] | None:
    statement = (
        select(SoundVariant, Job.status)
        .join(Job, Job.id == SoundVariant.job_id)
        .where(
            SoundVariant.fingerprint == fingerprint,
            or_(
                SoundVariant.state == "ready",
                and_(
                    SoundVariant.state == "queued",
                    Job.status.in_(_ACTIVE_JOB_STATUSES),
                ),
            ),
        )
        .order_by(SoundVariant.created_at.desc(), SoundVariant.id.desc())
        .limit(50)
    )
    for variant, job_status in session.execute(statement).all():
        if variant.state == "ready":
            if not _valid_cached_artifact(session, paths, variant):
                continue
            status = "ready"
        else:
            if job_status not in _ACTIVE_JOB_STATUSES:
                continue
            status = "queued" if job_status == "queued" else "running"
        return {
            "candidate_id": variant.id,
            "job_id": variant.job_id,
            "item_id": variant.item_id,
            "status": status,
            "cache_hit": True,
            "resolved_settings": _resolved_settings(request),
        }
    return None


def admit_generation_in_session(
    session: Session,
    jobs: JobQueue,
    paths: DataPaths,
    payload: dict[str, Any],
    prepared: PreparedSoundGeneration,
) -> dict[str, Any]:
    """Reuse valid candidates or atomically enqueue one or more generation jobs."""
    candidate_count = prepared.candidate_count
    if type(candidate_count) is not int or not 1 <= candidate_count <= 8:
        raise ValueError("Prepared sound generation count must be from 1 to 8.")

    if candidate_count == 1:
        request = _resolved_request(payload, prepared)
        return _admit_resolved_generation_in_session(
            session, jobs, paths, request, prepared
        )

    maximum = audio_cpp_sfx.seed_maximum(prepared.request_template["model_id"])
    if type(maximum) is not int or not 0 <= maximum <= 2**32 - 1:
        raise ValueError("The sound model returned an invalid seed maximum.")
    if candidate_count > maximum + 1:
        raise ValueError("candidate_count exceeds this model's available seed space.")

    request = _resolved_request(payload, prepared, seed_maximum=maximum)
    base_seed = request["seed"]
    receipts: list[dict[str, Any]] = []
    for index in range(candidate_count):
        child_request = deepcopy(request)
        child_request["seed"] = (base_seed + index) % (maximum + 1)
        child_request = audio_cpp_sfx.validate_request(child_request)
        receipts.append(
            _admit_resolved_generation_in_session(
                session, jobs, paths, child_request, prepared
            )
        )

    first = dict(receipts[0])
    first["cache_hit"] = all(receipt["cache_hit"] for receipt in receipts)
    first["candidate_count"] = candidate_count
    first["candidates"] = [
        {
            "candidate_id": receipt["candidate_id"],
            "job_id": receipt["job_id"],
            "item_id": receipt["item_id"],
            "status": receipt["status"],
            "cache_hit": receipt["cache_hit"],
            "seed": receipt["resolved_settings"]["seed"],
        }
        for receipt in receipts
    ]
    return first


def _admit_resolved_generation_in_session(
    session: Session,
    jobs: JobQueue,
    paths: DataPaths,
    request: dict[str, Any],
    prepared: PreparedSoundGeneration,
) -> dict[str, Any]:
    """Reuse one valid candidate or enqueue one sound.generate job."""
    fingerprint = audio_cpp_sfx.generation_fingerprint(
        request, prepared.runtime_identity
    )
    if cached := _cache_hit(session, paths, fingerprint, request):
        return cached

    provenance = deepcopy(request)
    if "kind" not in provenance and provenance.get("model_id") == DEFAULT_SOUND_MODEL_ID:
        provenance["kind"] = "sound"
    variant = SoundVariant(
        state="queued",
        fingerprint=fingerprint,
        provenance_json={
            "provider": "audio_cpp",
            "operation": "gen",
            **provenance,
            "runtime_identity": deepcopy(prepared.runtime_identity),
        },
    )
    session.add(variant)
    session.flush()
    job = jobs.enqueue_in_session(
        session,
        "sound.generate",
        {
            "candidate_id": variant.id,
            "request": request,
            "runtime_identity": deepcopy(prepared.runtime_identity),
        },
        session_id=None,
        max_attempts=1,
        resource_keys=audio_cpp_resource_keys(request["backend"]),
    )
    variant.job_id = job.id
    session.flush()
    return {
        "candidate_id": variant.id,
        "job_id": job.id,
        "item_id": None,
        "status": "queued",
        "cache_hit": False,
        "resolved_settings": _resolved_settings(request),
    }
