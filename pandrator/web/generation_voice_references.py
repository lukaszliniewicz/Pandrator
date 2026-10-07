"""Material managed-reference inventory shared by casting and audio identity."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .models import Artifact, Voice, VoiceSample
from .voice_library import sample_file_status


def _voice_key(value: str) -> str:
    return value.strip().casefold().removesuffix(".wav")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def voice_reference_index(
    session: Session,
    paths: DataPaths | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Index each voice's latest usable sample and material provider aliases."""
    if paths is None:
        paths = DataPaths(Path(str(session.get_bind().engine.url.database)).parent)
    voices: dict[str, list[dict[str, Any]]] = {}
    samples: dict[str, list[dict[str, Any]]] = {}
    for sample, artifact in session.execute(
        select(VoiceSample, Artifact)
        .join(Artifact, Artifact.id == VoiceSample.artifact_id)
        .order_by(VoiceSample.created_at.desc(), VoiceSample.id)
    ):
        if sample.voice_id in samples or sample_file_status(session, paths, sample)[0] != "ready":
            continue
        samples.setdefault(sample.voice_id, []).append(
            {
                "id": sample.id,
                "artifact_id": artifact.id,
                "content_hash": artifact.content_hash,
                "state": artifact.state,
                "transcript_hash": _hash(sample.transcript if sample.transcript_reviewed else ""),
            }
        )
    for voice in session.scalars(select(Voice)):
        providers = dict((voice.metadata_json or {}).get("providers") or {})
        # Voice.revision also changes for descriptions and publication
        # status. Reference samples and provider content hashes carry the
        # audio version without invalidating a harmless rename/republish.
        reference_providers = {
            provider_id: {
                key: value
                for key, value in registration.items()
                if key
                in {
                    "voice_id",
                    "provider_voice_id",
                    "sample_hash",
                    "source_audio_hash",
                    "reference_text_hash",
                    "model",
                }
            }
            for provider_id, registration in providers.items()
            if isinstance(registration, dict)
        }
        identity = {
            "id": voice.id,
            "samples": samples.get(voice.id, []),
            "providers": reference_providers,
        }
        names = {voice.id, voice.name}
        for registration in providers.values():
            if isinstance(registration, dict):
                names.update(
                    str(registration.get(key) or "") for key in ("voice_id", "provider_voice_id")
                )
        for name in names:
            if name.strip():
                voices.setdefault(_voice_key(name), []).append(identity)
    return voices
