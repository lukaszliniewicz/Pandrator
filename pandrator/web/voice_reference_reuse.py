"""Exact managed reference validation and normalized design-reference reuse."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .artifacts import sha256_file
from .models import Artifact, Voice, VoiceSample
from .voice_library import sample_file_status

REFERENCE_PREPARATION_PROFILE = "mono-pcm16-24000-none/v1"


def verified_sample_artifact(
    session: Session, paths: DataPaths, sample: VoiceSample, *, expected_sha256: str | None = None
) -> tuple[Artifact, str]:
    """Require a readable, hash-identical registered normalized sample."""
    status, path = sample_file_status(session, paths, sample)
    artifact = session.get(Artifact, sample.artifact_id)
    if status != "ready" or artifact is None or path is None or path.suffix.lower() != ".wav":
        raise ValueError("The selected normalized voice sample is unavailable.")
    registered_hash = str(artifact.content_hash or "").strip().casefold()
    try:
        actual_hash = sha256_file(path)
    except OSError as error:
        raise ValueError("The selected voice sample could not be read.") from error
    if not registered_hash or actual_hash != registered_hash:
        raise ValueError("The selected voice sample changed after it was registered.")
    if expected_sha256 is not None and actual_hash != expected_sha256.casefold():
        raise ValueError("The selected voice sample does not match the requested SHA256.")
    return artifact, actual_hash


def reusable_design_reference(
    session: Session, paths: DataPaths, voice: Voice, *, source_artifact_id: str,
    source_sha256: str, transcript: str, language: str | None,
) -> dict[str, Any] | None:
    """Reuse an equivalent current reference, as resolved by native synthesis."""
    samples = session.scalars(
        select(VoiceSample).where(VoiceSample.voice_id == voice.id)
        .order_by(VoiceSample.created_at.desc())
    )
    for sample in samples:
        if sample_file_status(session, paths, sample)[0] != "ready":
            continue
        # Native synthesis resolves the newest ready sample. Reusing an older
        # match would leave that resolver on a different reference.
        if not sample.transcript_reviewed or sample.transcript != transcript or sample.transcript_language != language:
            return None
        artifact = session.get(Artifact, sample.artifact_id)
        metadata = dict(artifact.metadata_json or {}) if artifact else {}
        provenance = metadata.get("sample_provenance") or {}
        if not isinstance(provenance, dict) or (
            provenance.get("source_preview_artifact_id") != source_artifact_id
            or provenance.get("source_preview_sha256") != source_sha256
            or provenance.get("reference_preparation_profile") != REFERENCE_PREPARATION_PROFILE
            or metadata.get("reference_preparation_profile") != REFERENCE_PREPARATION_PROFILE
        ):
            return None
        try:
            artifact, digest = verified_sample_artifact(session, paths, sample)
        except ValueError:
            return None
        return {
            "status": "ready", "sample_id": sample.id, "artifact_id": artifact.id,
            "sample_sha256": digest, "voice_revision": voice.revision,
            "reused_reference": True,
        }
    return None
