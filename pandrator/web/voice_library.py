"""Shared voice-library seeding helpers for the web workspace."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .artifacts import ArtifactService, sha256_file
from .database import Database
from .models import Artifact, User, Voice, VoiceSample, utcnow
from .projects.artifact_identity import resolve_artifact_ownership
from .voice_identity import voice_creation_workspace, voice_sample_workspace, voice_workspace

BUNDLED_VOICE_KEY = "pandrator-sample-male-v1"
BUNDLED_VOICE_NAME = "Pandrator sample voice"
BUNDLED_SAMPLE_TRANSCRIPT = (
    "The window was open, granted, but the room is on the second floor. Anyway, "
    "you may dismiss the window. I remember the old lady saying there was a bar "
    "across it, and that nobody could have squeezed through."
)


def resolve_voice_sample_transcription_settings(
    settings: Mapping[str, Any],
    voice_language: str | None,
) -> dict[str, Any]:
    """Freeze voice-reference transcription language and engine defaults.

    Voice samples use their voice language when the request does not select a
    language. The shared automatic policy selects a supported runtime after
    resolving that source language. Explicit engine/backend choices remain
    untouched.
    """

    resolved = deepcopy(dict(settings))
    requested_language = resolved.get("stt_language")
    if str(requested_language or "").strip():
        selected_language = requested_language
    elif str(voice_language or "").strip():
        selected_language = voice_language
    else:
        selected_language = "auto"
    resolved["stt_language"] = selected_language

    has_explicit_engine = bool(str(resolved.get("stt_engine") or "").strip())
    has_explicit_backend = bool(str(resolved.get("stt_backend") or "").strip())
    if not has_explicit_engine and not has_explicit_backend:
        resolved["stt_engine"] = "auto"
        resolved["stt_backend"] = "auto"
    return resolved


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
        if registration.get("resource_kind") in {"linked_reference", "designed_voice"}:
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
    voice_sample_workspace(session, sample)
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
    artifact = session.get(Artifact, sample.artifact_id)
    return {
        "id": sample.id,
        "voice_id": sample.voice_id,
        "artifact_id": sample.artifact_id,
        "sample_sha256": artifact.content_hash if artifact else None,
        "transcript": sample.transcript,
        "transcript_language": sample.transcript_language,
        "transcript_reviewed": sample.transcript_reviewed,
        "file_status": status,
        "available": status == "ready",
        "voice_revision": voice_revision,
        "created_at": sample.created_at.isoformat(),
    }


def validate_profile_evidence(
    session: Session, paths: DataPaths, profile: dict[str, Any] | None,
    *, workspace_id: str | None = None,
) -> None:
    if not profile:
        return
    workspace = voice_creation_workspace(session, workspace_id=workspace_id)
    evidence = [
        *(profile.get("evidence") or {}).values(),
        *(lang.get("evidence") for lang in profile.get("languages", [])),
    ]
    for item in evidence:
        if not item or not item.get("artifact_id"):
            continue
        artifact = session.get(Artifact, item["artifact_id"])
        if artifact is None or artifact.state == "deleted" or artifact.kind != "audio":
            raise ValueError(
                "Voice trait evidence must reference an available audio artifact."
            )
        owner = resolve_artifact_ownership(session, artifact=artifact)
        if owner.workspace_id != workspace.id:
            raise ValueError("Voice trait evidence belongs to another workspace.")
        try:
            exists = paths.managed_path(artifact.relative_path).is_file()
        except (OSError, ValueError):
            exists = False
        if not exists:
            raise ValueError("Voice trait evidence audio is unavailable.")


def voice_payload(
    session: Session,
    paths: DataPaths,
    voice: Voice,
    *,
    samples: list[VoiceSample] | None = None,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    """Return a voice while deriving stale provider state from local files."""

    workspace = voice_workspace(session, voice, workspace_id=workspace_id)
    if samples is None:
        sample_rows = session.execute(
            select(VoiceSample, Artifact)
            .outerjoin(Artifact, Artifact.id == VoiceSample.artifact_id)
            .where(VoiceSample.voice_id == voice.id)
            .order_by(VoiceSample.created_at.desc())
        ).all()
        samples = [row[0] for row in sample_rows]
    for sample in samples:
        if sample.voice_id != voice.id:
            raise ValueError("A voice sample belongs to another voice.")
        voice_sample_workspace(session, sample, workspace_id=workspace.id)
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
    voice_category = str(metadata.get("voice_category") or "").strip().casefold()
    if voice_category not in {"male", "female", "androgynous", "unspecified"}:
        voice_category = "unspecified"
    metadata["voice_category"] = voice_category
    providers = dict(metadata.get("providers") or {})
    for service_id, raw in list(providers.items()):
        registration = dict(raw or {})
        if registration.get("resource_kind") in {"linked_reference", "designed_voice"}:
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
    from .voice_catalog import normalized_profile

    preferred_artifact = next(
        (sample.artifact_id for sample in samples if sample.id == newest_available),
        None,
    )
    source = session.get(Artifact, preferred_artifact) if preferred_artifact else None
    provenance = (
        (source.metadata_json or {}).get("sample_provenance", {}) if source else {}
    )
    origin = (
        "builtin"
        if is_bundled_voice(voice)
        else "designed"
        if provenance.get("source_kind") == "generated_voice_design" or metadata.get("google_voice_design")
        else "imported"
        if samples
        else "unknown"
    )
    return {
        "origin": origin,
        "id": voice.id,
        "name": voice.name,
        "language": voice.language,
        "description": voice.description,
        "rvc_model_ref": voice.rvc_model_ref,
        "voice_category": voice_category,
        "profile": normalized_profile(metadata.get("profile")),
        "metadata_json": metadata,
        "revision": voice.revision,
        "bundled": is_bundled_voice(voice),
        "sample_count": len(samples),
        "available_sample_count": len(available_ids),
        "preferred_sample_id": newest_available,
        "preview_artifact_id": preferred_artifact or (metadata.get("google_voice_design") or {}).get("preview_artifact_id"),
        "created_at": voice.created_at.isoformat(),
        "updated_at": voice.updated_at.isoformat(),
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
    *, workspace_id: str | None = None,
) -> list[dict[str, Any]]:
    """Serialize a voice collection with one sample query instead of N+1."""

    records = list(voices)
    if not records:
        return []
    workspace = voice_workspace(session, records[0], workspace_id=workspace_id)
    for voice in records:
        voice_workspace(session, voice, workspace_id=workspace.id)
    samples_by_voice: dict[str, list[VoiceSample]] = {voice.id: [] for voice in records}
    sample_rows = session.execute(
        select(VoiceSample, Artifact)
        .outerjoin(Artifact, Artifact.id == VoiceSample.artifact_id)
        .where(VoiceSample.voice_id.in_(samples_by_voice))
        .order_by(VoiceSample.voice_id, VoiceSample.created_at.desc())
    ).all()
    for sample, _artifact in sample_rows:
        voice_sample_workspace(session, sample, workspace_id=workspace.id)
        samples_by_voice.setdefault(sample.voice_id, []).append(sample)
    return [
        voice_payload(
            session,
            paths,
            voice,
            samples=samples_by_voice.get(voice.id, []),
            workspace_id=workspace.id,
        )
        for voice in records
    ]


def retire_sample_artifact(
    session: Session,
    paths: DataPaths,
    sample: VoiceSample,
) -> Path | None:
    """Retire an unshared sample artifact and return its removable managed path."""

    voice_sample_workspace(session, sample)
    artifact = session.get(Artifact, sample.artifact_id)
    if artifact is None:
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
    if references:
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
    return path


def remove_managed_files(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # The database mutation is authoritative; reconciliation reports a
            # file that could not be cleaned up without making deletion fail.
            continue


def _materialize_bundled_sample(source: Path, target: Path) -> None:
    """Publish a complete bundled file without replacing an existing sample."""
    if target.exists():
        return
    staged: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".bundled-", delete=False) as output:
            staged = Path(output.name)
            with source.open("rb") as incoming:
                shutil.copyfileobj(incoming, output)
        try:
            os.link(staged, target)
        except FileExistsError:
            pass
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def ensure_bundled_voice(
    database: Database,
    paths: DataPaths,
    artifacts: ArtifactService,
    *,
    workspace_id: str | None = None,
) -> Voice | None:
    """Idempotently materialize the bundled reference in its owning workspace."""

    source = Path(__file__).resolve().parents[2] / "tts_voices" / "sample_male_new.wav"
    if not source.is_file():
        return None

    with database.snapshot_session() as session:
        workspace = voice_creation_workspace(session, workspace_id=workspace_id)
        owner_id = session.scalar(select(User.id).where(User.legacy_subject == "owner"))
        legacy_workspace = workspace.kind == "personal" and workspace.personal_owner_user_id == owner_id
        workspace_id = workspace.id
        bundled_id = BUNDLED_VOICE_KEY if legacy_workspace else str(uuid5(
            NAMESPACE_URL, f"pandrator:workspace:{workspace_id}:bundled-voice:{BUNDLED_VOICE_KEY}"
        ))
        existing = session.get(Voice, bundled_id)
        if existing is not None:
            voice_workspace(session, existing, workspace_id=workspace_id)

    target_dir = paths.voices / bundled_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "sample.wav"
    _materialize_bundled_sample(source, target)

    with database.snapshot_session() as session:
        workspace = voice_creation_workspace(session, workspace_id=workspace_id)
        voice = session.get(Voice, bundled_id)
        if voice is not None:
            voice_workspace(session, voice, workspace_id=workspace.id)
        if voice is None:
            name_owner = session.scalar(
                select(Voice).where(Voice.name == BUNDLED_VOICE_NAME, Voice.workspace_id == workspace.id)
            )
            if (
                name_owner is not None
                and (name_owner.metadata_json or {}).get("bundled_voice")
                == BUNDLED_VOICE_KEY
            ):
                voice = name_owner
        if voice is not None:
            try:
                canonical_target = target.resolve(strict=True)
                voices_root = paths.voices.resolve(strict=True)
                if not canonical_target.is_relative_to(voices_root):
                    raise ValueError("Bundled voice sample is outside its managed directory.")
                target_hash = sha256_file(target)
            except (OSError, RuntimeError, ValueError):
                target_hash = ""
                canonical_target = None
            if target_hash and canonical_target is not None:
                samples = session.scalars(
                    select(VoiceSample).where(VoiceSample.voice_id == voice.id)
                ).all()
                for sample in samples:
                    artifact = session.get(Artifact, sample.artifact_id)
                    if (
                        artifact is None
                        or artifact.state != "current"
                        or artifact.kind != "audio"
                        or artifact.role != "voice_sample"
                        or artifact.content_hash != target_hash
                        or (artifact.metadata_json or {}).get("bundled_voice")
                        != BUNDLED_VOICE_KEY
                    ):
                        continue
                    status, artifact_path = sample_file_status(session, paths, sample)
                    if status != "ready" or artifact_path is None:
                        continue
                    try:
                        canonical_artifact_path = artifact_path.resolve(strict=True)
                    except (OSError, RuntimeError):
                        continue
                    if canonical_artifact_path != canonical_target:
                        continue
                    session.expunge(voice)
                    return voice

    prepared = artifacts.prepare_registration(target)
    with database.immediate_session() as session:
        workspace = voice_creation_workspace(session, workspace_id=workspace_id)
        artifact = artifacts.register_in_session(
            session, target, kind="audio", role="voice_sample",
            workspace_id=workspace.id,
            metadata={"bundled_voice": BUNDLED_VOICE_KEY}, _prepared=prepared,
        )
        voice = session.get(Voice, bundled_id)
        if voice is not None:
            voice_workspace(session, voice, workspace_id=workspace.id)
        if voice is None:
            display_name = BUNDLED_VOICE_NAME
            name_owner = session.scalar(select(Voice).where(Voice.name == display_name, Voice.workspace_id == workspace.id))
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
                    id=bundled_id,
                    workspace_id=workspace.id,
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
                    workspace_id=workspace.id,
                    voice_id=voice.id,
                    artifact_id=artifact.id,
                    transcript=BUNDLED_SAMPLE_TRANSCRIPT,
                    transcript_language="en",
                    transcript_reviewed=True,
                )
            )
        session.flush()
        voice_workspace(session, voice, workspace_id=workspace.id)
        session.expunge(voice)
        return voice


def _reference_registration_id(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().casefold()).strip("_")


def resolve_audio_cpp_voice_reference(
    session: Session, paths: DataPaths, settings: dict[str, Any],
    _service_config_cache=None,
    *, workspace_id: str | None = None,
    voice_id: str | None = None, strict_selection: bool = False,
    _metadata_only: bool = False,
) -> tuple[str, Path, Artifact, VoiceSample] | None:
    """Select exactly the linked reference that audio.cpp synthesis will use."""
    from pandrator.logic import tts_handler

    registration_ids = {"audio_cpp"}
    endpoint, _error = tts_handler.resolve_openai_audio_endpoint(settings, _service_config_cache)
    if endpoint is not None and (
        str(endpoint.get("adapter") or "").strip().casefold().replace("-", "_")
        == "audio_cpp"
    ):
        registration_ids.add(_reference_registration_id(endpoint.get("name")))

    selected = str(settings.get("voice") or settings.get("speaker") or "").strip()
    if not selected:
        return None
    workspace = voice_creation_workspace(session, workspace_id=workspace_id)
    selected_key = selected.casefold()
    match: tuple[str, Path, Artifact, VoiceSample] | None = None
    voices = select(Voice).where(Voice.workspace_id == workspace.id)
    if voice_id is not None:
        voices = voices.where(Voice.id == voice_id)
    matched_voice_id: str | None = None
    for voice in session.scalars(voices).all():
        providers = dict((voice.metadata_json or {}).get("providers") or {})
        registration = None
        for provider_id, raw_registration in providers.items():
            normalized_provider = _reference_registration_id(provider_id)
            if normalized_provider not in registration_ids or not isinstance(
                raw_registration, dict
            ):
                continue
            if (
                str(raw_registration.get("resource_kind") or "") != "linked_reference"
                or str(raw_registration.get("status") or "") != "ready"
            ):
                continue
            candidate_names = {
                voice.name,
                str(raw_registration.get("voice_id") or ""),
                str(raw_registration.get("provider_voice_id") or ""),
            }
            if selected_key not in {
                name.strip().casefold() for name in candidate_names if name.strip()
            }:
                continue
            registration = raw_registration
            break
        if registration is None:
            continue
        if strict_selection and matched_voice_id is not None:
            raise ValueError("The selected audio.cpp voice alias is ambiguous.")
        matched_voice_id = voice.id

        samples = list(
            session.scalars(
                select(VoiceSample)
                .where(VoiceSample.voice_id == voice.id)
                .order_by(VoiceSample.created_at.desc(), VoiceSample.id.desc())
            ).all()
        )
        sample = next(
            (
                item
                for item in samples
                if sample_file_status(session, paths, item)[0] == "ready"
            ),
            None,
        )
        if sample is None:
            raise ValueError(
                f"Linked audio.cpp voice '{voice.name}' has no readable sample."
            )
        artifact = session.get(Artifact, sample.artifact_id)
        status, path = sample_file_status(session, paths, sample)
        if artifact is None or status != "ready" or path is None:
            raise ValueError(
                f"Linked audio.cpp voice '{voice.name}' has no readable sample."
            )
        if path.suffix.lower() != ".wav":
            raise ValueError(
                "audio.cpp linked voice references require a normalized WAV sample."
            )
        try:
            size_bytes = path.stat().st_size
        except OSError as error:
            raise ValueError(
                "The audio.cpp linked voice sample could not be read."
            ) from error
        if size_bytes > 5 * 1024 * 1024:
            raise ValueError("audio.cpp linked voice references must be at most 5 MiB.")
        content_hash = str(artifact.content_hash or "").strip()
        if not content_hash:
            if _metadata_only:
                raise ValueError("The audio.cpp reference has no published content hash.")
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            content_hash = digest.hexdigest()
        match = (content_hash, path, artifact, sample)
        if not strict_selection:
            break

    return match
