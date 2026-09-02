"""Shared voice-library seeding helpers for the web workspace."""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from copy import deepcopy
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .artifacts import ArtifactService
from .database import Database
from .models import Artifact, Voice, VoiceSample, utcnow

BUNDLED_VOICE_KEY = "pandrator-sample-male-v1"
BUNDLED_VOICE_NAME = "Pandrator sample voice"
BUNDLED_SAMPLE_TRANSCRIPT = (
    "The window was open, granted, but the room is on the second floor. Anyway, "
    "you may dismiss the window. I remember the old lady saying there was a bar "
    "across it, and that nobody could have squeezed through."
)


def is_bundled_voice(voice: Voice) -> bool:
    return bool(
        voice.id == BUNDLED_VOICE_KEY
        or (voice.metadata_json or {}).get("bundled_voice")
    )


def mark_provider_registrations_stale(
    voice: Voice,
    reason: str,
    *,
    sample_id: str | None = None,
    reference_text_only: bool = False,
) -> None:
    """Keep remote IDs while making a changed local reference explicit."""

    metadata = deepcopy(voice.metadata_json or {})
    providers = dict(metadata.get("providers") or {})
    changed = False
    for service_id, raw in list(providers.items()):
        registration = dict(raw or {})
        if registration.get("resource_kind") == "linked_reference":
            # A linked registration follows the local voice.  It is not a
            # provider-side copy that becomes stale when a newer sample or
            # reviewed transcript is added.
            continue
        if (
            sample_id
            and registration.get("sample_id")
            and registration.get("sample_id") != sample_id
        ):
            continue
        if reference_text_only:
            reference_text_mode = registration.get("reference_text_mode")
            if reference_text_mode is None:
                reference_text_mode = (
                    "required" if service_id == "fishs2" else "ignored"
                )
            if reference_text_mode == "ignored":
                continue
        if registration.get("status") != "stale":
            registration["status"] = "stale"
            changed = True
        registration["stale_reason"] = reason
        registration["stale_at"] = utcnow().isoformat()
        providers[service_id] = registration
    if changed or providers:
        metadata["providers"] = providers
        voice.metadata_json = metadata


def sample_file_status(
    session: Session,
    paths: DataPaths,
    sample: VoiceSample,
) -> tuple[str, Path | None]:
    artifact = session.get(Artifact, sample.artifact_id)
    if artifact is None or artifact.state == "deleted":
        return "missing", None
    try:
        path = paths.managed_path(artifact.relative_path)
    except (OSError, ValueError):
        return "unsafe", None
    return ("ready", path) if path.is_file() else ("missing", path)


def voice_sample_payload(
    session: Session,
    paths: DataPaths,
    sample: VoiceSample,
    *,
    voice_revision: int,
) -> dict[str, Any]:
    status, _path = sample_file_status(session, paths, sample)
    return {
        "id": sample.id,
        "voice_id": sample.voice_id,
        "artifact_id": sample.artifact_id,
        "transcript": sample.transcript,
        "transcript_language": sample.transcript_language,
        "transcript_reviewed": sample.transcript_reviewed,
        "file_status": status,
        "available": status == "ready",
        "voice_revision": voice_revision,
        "created_at": sample.created_at.isoformat(),
    }


def voice_payload(
    session: Session,
    paths: DataPaths,
    voice: Voice,
    *,
    samples: list[VoiceSample] | None = None,
) -> dict[str, Any]:
    """Return a voice while deriving stale provider state from local files."""

    if samples is None:
        samples = list(
            session.scalars(
                select(VoiceSample)
                .where(VoiceSample.voice_id == voice.id)
                .order_by(VoiceSample.created_at.desc())
            ).all()
        )
    available_ids = {
        sample.id
        for sample in samples
        if sample_file_status(session, paths, sample)[0] == "ready"
    }
    newest_available = next(
        (sample.id for sample in samples if sample.id in available_ids),
        None,
    )
    metadata = deepcopy(voice.metadata_json or {})
    providers = dict(metadata.get("providers") or {})
    for service_id, raw in list(providers.items()):
        registration = dict(raw or {})
        if registration.get("resource_kind") == "linked_reference":
            providers[service_id] = registration
            continue
        registered_sample = str(registration.get("sample_id") or "")
        if registration.get("status") == "ready" and (
            not registered_sample
            or registered_sample not in available_ids
            or registered_sample != newest_available
        ):
            registration["status"] = "stale"
            registration["stale_reason"] = (
                "The uploaded sample is missing."
                if registered_sample not in available_ids
                else "A newer local sample is available."
            )
        providers[service_id] = registration
    metadata["providers"] = providers
    return {
        "id": voice.id,
        "name": voice.name,
        "language": voice.language,
        "description": voice.description,
        "rvc_model_ref": voice.rvc_model_ref,
        "metadata_json": metadata,
        "revision": voice.revision,
        "bundled": is_bundled_voice(voice),
        "sample_count": len(samples),
        "available_sample_count": len(available_ids),
        "preferred_sample_id": newest_available,
        "preferred_sample_transcript_reviewed": bool(
            newest_available
            and next(
                (
                    sample.transcript_reviewed
                    for sample in samples
                    if sample.id == newest_available
                ),
                False,
            )
        ),
    }


def voice_payloads(
    session: Session,
    paths: DataPaths,
    voices: Iterable[Voice],
) -> list[dict[str, Any]]:
    """Serialize a voice collection with one sample query instead of N+1."""

    records = list(voices)
    if not records:
        return []
    samples_by_voice: dict[str, list[VoiceSample]] = {voice.id: [] for voice in records}
    samples = session.scalars(
        select(VoiceSample)
        .where(VoiceSample.voice_id.in_(samples_by_voice))
        .order_by(VoiceSample.voice_id, VoiceSample.created_at.desc())
    ).all()
    for sample in samples:
        samples_by_voice.setdefault(sample.voice_id, []).append(sample)
    return [
        voice_payload(
            session,
            paths,
            voice,
            samples=samples_by_voice.get(voice.id, []),
        )
        for voice in records
    ]


def retire_sample_artifact(
    session: Session,
    paths: DataPaths,
    sample: VoiceSample,
) -> Path | None:
    """Soft-delete one artifact and return a safely removable managed path."""

    artifact = session.get(Artifact, sample.artifact_id)
    if artifact is None:
        return None
    ArtifactService._mark_descendants_stale(session, artifact.id)
    artifact.state = "deleted"
    artifact.metadata_json = {
        **dict(artifact.metadata_json or {}),
        "deleted_at": utcnow().isoformat(),
    }
    artifact.updated_at = utcnow()
    try:
        path = paths.managed_path(artifact.relative_path).resolve(strict=False)
        voice_root = (paths.voices / sample.voice_id).resolve(strict=False)
    except (OSError, ValueError):
        return None
    if not path.is_relative_to(voice_root):
        return None
    references = int(
        session.scalar(
            select(func.count())
            .select_from(VoiceSample)
            .where(
                VoiceSample.artifact_id == artifact.id,
                VoiceSample.id != sample.id,
            )
        )
        or 0
    )
    return path if references == 0 else None


def remove_managed_files(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # The database mutation is authoritative; reconciliation reports a
            # file that could not be cleaned up without making deletion fail.
            continue


def ensure_bundled_voice(
    database: Database,
    paths: DataPaths,
    artifacts: ArtifactService,
) -> Voice | None:
    """Idempotently expose the bundled cloning reference in the web library."""

    source = Path(__file__).resolve().parents[2] / "tts_voices" / "sample_male_new.wav"
    if not source.is_file():
        return None

    target_dir = paths.voices / BUNDLED_VOICE_KEY
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "sample.wav"
    if not target.is_file():
        shutil.copy2(source, target)
    artifact = artifacts.register(
        target,
        kind="audio",
        role="voice_sample",
        metadata={"bundled_voice": BUNDLED_VOICE_KEY},
    )

    with database.session() as session:
        voice = session.get(Voice, BUNDLED_VOICE_KEY)
        if voice is None:
            display_name = BUNDLED_VOICE_NAME
            name_owner = session.scalar(select(Voice).where(Voice.name == display_name))
            if (
                name_owner is not None
                and (name_owner.metadata_json or {}).get("bundled_voice")
                == BUNDLED_VOICE_KEY
            ):
                voice = name_owner
            elif name_owner is not None:
                # Never attach the bundled sample to a user-created voice that
                # happens to share the display name.
                display_name = f"{BUNDLED_VOICE_NAME} (bundled)"
            if voice is None:
                voice = Voice(
                    id=BUNDLED_VOICE_KEY,
                    name=display_name,
                    language="en",
                    description="Bundled reference sample for local voice-cloning backends.",
                    metadata_json={"bundled_voice": BUNDLED_VOICE_KEY, "providers": {}},
                )
                session.add(voice)
                session.flush()
        sample = session.scalar(
            select(VoiceSample).where(
                VoiceSample.voice_id == voice.id,
                VoiceSample.artifact_id == artifact.id,
            )
        )
        if sample is None:
            session.add(
                VoiceSample(
                    voice_id=voice.id,
                    artifact_id=artifact.id,
                    transcript=BUNDLED_SAMPLE_TRANSCRIPT,
                    transcript_language="en",
                    transcript_reviewed=True,
                )
            )
        session.flush()
        session.expunge(voice)
        return voice
