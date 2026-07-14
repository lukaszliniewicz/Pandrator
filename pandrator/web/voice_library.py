"""Shared voice-library seeding helpers for the web workspace."""

from __future__ import annotations

import shutil
from pathlib import Path

from sqlalchemy import select

from pandrator.runtime import DataPaths

from .artifacts import ArtifactService
from .database import Database
from .models import Voice, VoiceSample


BUNDLED_VOICE_KEY = "pandrator-sample-male-v1"
BUNDLED_VOICE_NAME = "Pandrator sample voice"
BUNDLED_SAMPLE_TRANSCRIPT = (
    "The window was open, granted, but the room is on the second floor. Anyway, "
    "you may dismiss the window. I remember the old lady saying there was a bar "
    "across it, and that nobody could have squeezed through."
)


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
            if name_owner is not None and (name_owner.metadata_json or {}).get("bundled_voice") == BUNDLED_VOICE_KEY:
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
