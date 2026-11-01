"""Workspace isolation for local voice-library and linked-reference records."""

from __future__ import annotations

import wave
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from struct import pack
from threading import Barrier
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from pandrator.runtime import DataPaths
from pandrator.web import generation_voice_references, voice_library
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.models import Artifact, Voice, VoiceSample, Workspace
from pandrator.web.projects.identity import initial_personal_workspace
from pandrator.web.voice_library import (
    BUNDLED_SAMPLE_TRANSCRIPT,
    BUNDLED_VOICE_KEY,
    BUNDLED_VOICE_NAME,
    ensure_bundled_voice,
    resolve_audio_cpp_voice_reference,
    validate_profile_evidence,
    voice_payload,
    voice_payloads,
    voice_sample_payload,
)
from pandrator.web.voice_queries import (
    voice_in_workspace,
    voice_name_owner,
    voice_sample_in_workspace,
    voices_in_workspace,
)
from tests.web_test_support import prepare_web_test_data_root


@dataclass
class VoiceHarness:
    database: Database
    paths: DataPaths
    artifacts: ArtifactService
    workspaces: dict[str, str]
    personal_workspace_id: str


def _write_wav(path: Path, *, frames: int = 8000, value: int = 500) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(pack(f"<{frames}h", *([value] * frames)))


def _new_workspace(database: Database, name: str, *, state: str = "active") -> str:
    with database.session() as session:
        workspace = Workspace(kind="team", name=name, state=state)
        session.add(workspace)
        session.flush()
        return workspace.id


@pytest.fixture
def voice_harness(tmp_path: Path) -> Iterator[VoiceHarness]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    artifacts = ArtifactService(database, paths)
    workspaces = {
        "alpha": _new_workspace(database, "Alpha voice team"),
        "beta": _new_workspace(database, "Beta voice team"),
        "archived": _new_workspace(database, "Archived voice team", state="archived"),
    }
    with database.session() as session:
        personal_workspace_id = initial_personal_workspace(session).id
    try:
        yield VoiceHarness(
            database=database,
            paths=paths,
            artifacts=artifacts,
            workspaces=workspaces,
            personal_workspace_id=personal_workspace_id,
        )
    finally:
        database.dispose()


def _new_voice(
    harness: VoiceHarness,
    workspace_id: str,
    name: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> str:
    with harness.database.session() as session:
        voice = Voice(
            workspace_id=workspace_id,
            name=name,
            metadata_json=metadata or {},
        )
        session.add(voice)
        session.flush()
        return voice.id


def _new_sample_artifact(
    harness: VoiceHarness,
    workspace_id: str,
    name: str,
    *,
    value: int = 500,
) -> Artifact:
    path = harness.paths.uploads / f"{name}.wav"
    _write_wav(path, value=value)
    return harness.artifacts.register(
        path,
        kind="audio",
        role="voice_sample",
        workspace_id=workspace_id,
    )


def _new_sample(
    harness: VoiceHarness,
    workspace_id: str,
    voice_id: str,
    artifact_id: str,
    *,
    transcript: str = "Synthetic voice sample.",
) -> str:
    with harness.database.session() as session:
        sample = VoiceSample(
            workspace_id=workspace_id,
            voice_id=voice_id,
            artifact_id=artifact_id,
            transcript=transcript,
            transcript_language="en",
            transcript_reviewed=True,
        )
        session.add(sample)
        session.flush()
        return sample.id


def _table_counts(harness: VoiceHarness) -> tuple[int, int, int]:
    with harness.database.session() as session:
        voices = session.scalar(select(func.count()).select_from(Voice))
        samples = session.scalar(select(func.count()).select_from(VoiceSample))
        artifacts = session.scalar(select(func.count()).select_from(Artifact))
    assert voices is not None and samples is not None and artifacts is not None
    return voices, samples, artifacts


def test_voice_names_are_unique_inside_each_workspace_and_direct_lookups_are_scoped(
    voice_harness: VoiceHarness,
) -> None:
    harness = voice_harness
    alpha_id = _new_voice(harness, harness.workspaces["alpha"], "Shared voice")
    beta_id = _new_voice(harness, harness.workspaces["beta"], "Shared voice")
    alpha_artifact = _new_sample_artifact(
        harness, harness.workspaces["alpha"], "alpha-shared-voice"
    )
    beta_artifact = _new_sample_artifact(harness, harness.workspaces["beta"], "beta-shared-voice")
    alpha_sample_id = _new_sample(harness, harness.workspaces["alpha"], alpha_id, alpha_artifact.id)
    beta_sample_id = _new_sample(harness, harness.workspaces["beta"], beta_id, beta_artifact.id)
    with pytest.raises(IntegrityError):
        with harness.database.session() as session:
            session.add(Voice(workspace_id=harness.workspaces["alpha"], name="Shared voice"))
            session.flush()

    with harness.database.session() as session:
        alpha_voices = voices_in_workspace(session, workspace_id=harness.workspaces["alpha"])
        beta_voices = voices_in_workspace(session, workspace_id=harness.workspaces["beta"])
        assert [voice.id for voice in alpha_voices] == [alpha_id]
        assert [voice.id for voice in beta_voices] == [beta_id]
        alpha_by_id = voice_in_workspace(
            session, alpha_id, workspace_id=harness.workspaces["alpha"]
        )
        assert alpha_by_id is not None and alpha_by_id.id == alpha_id
        assert (
            voice_in_workspace(session, beta_id, workspace_id=harness.workspaces["alpha"]) is None
        )
        alpha_name_owner = voice_name_owner(
            session, "Shared voice", workspace_id=harness.workspaces["alpha"]
        )
        beta_name_owner = voice_name_owner(
            session, "Shared voice", workspace_id=harness.workspaces["beta"]
        )
        assert alpha_name_owner is not None and alpha_name_owner.id == alpha_id
        assert beta_name_owner is not None and beta_name_owner.id == beta_id
        alpha_sample = voice_sample_in_workspace(
            session,
            alpha_sample_id,
            voice_id=alpha_id,
            workspace_id=harness.workspaces["alpha"],
        )
        assert alpha_sample is not None and alpha_sample.id == alpha_sample_id
        assert (
            voice_sample_in_workspace(
                session,
                beta_sample_id,
                voice_id=beta_id,
                workspace_id=harness.workspaces["alpha"],
            )
            is None
        )
        assert voice_in_workspace(session, alpha_id) is None
        assert voice_name_owner(session, "Shared voice") is None
        default_ids = {voice.id for voice in voices_in_workspace(session)}
        assert alpha_id not in default_ids and beta_id not in default_ids
        assert voice_sample_in_workspace(session, alpha_sample_id, voice_id=alpha_id) is None


def test_malformed_voice_sample_backing_artifact_is_rejected(
    voice_harness: VoiceHarness,
) -> None:
    harness = voice_harness
    alpha_workspace = harness.workspaces["alpha"]
    beta_workspace = harness.workspaces["beta"]
    voice_id = _new_voice(harness, alpha_workspace, "Alpha voice")
    foreign_artifact = _new_sample_artifact(harness, beta_workspace, "foreign-voice-sample")
    malformed_sample_id = _new_sample(harness, alpha_workspace, voice_id, foreign_artifact.id)

    with harness.database.session() as session:
        with pytest.raises(ValueError, match="workspace"):
            voice_sample_in_workspace(
                session,
                malformed_sample_id,
                voice_id=voice_id,
                workspace_id=alpha_workspace,
            )


def test_voice_projections_reject_mixed_scope_records_before_file_projection(
    voice_harness: VoiceHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = voice_harness
    alpha_workspace = harness.workspaces["alpha"]
    beta_workspace = harness.workspaces["beta"]
    alpha_id = _new_voice(harness, alpha_workspace, "Alpha voice")
    beta_id = _new_voice(harness, beta_workspace, "Beta voice")
    alpha_artifact = _new_sample_artifact(harness, alpha_workspace, "alpha-projection")
    beta_artifact = _new_sample_artifact(harness, beta_workspace, "beta-projection")
    alpha_sample_id = _new_sample(harness, alpha_workspace, alpha_id, alpha_artifact.id)
    beta_sample_id = _new_sample(harness, beta_workspace, beta_id, beta_artifact.id)

    with harness.database.session() as session:
        alpha_voice = session.get(Voice, alpha_id)
        beta_voice = session.get(Voice, beta_id)
        alpha_sample = session.get(VoiceSample, alpha_sample_id)
        beta_sample = session.get(VoiceSample, beta_sample_id)
        assert alpha_voice is not None and beta_voice is not None
        assert alpha_sample is not None and beta_sample is not None
        payload = voice_payload(
            session,
            harness.paths,
            alpha_voice,
            samples=[alpha_sample],
            workspace_id=alpha_workspace,
        )
        payloads = voice_payloads(
            session, harness.paths, [alpha_voice], workspace_id=alpha_workspace
        )
        sample_payload = voice_sample_payload(
            session,
            harness.paths,
            alpha_sample,
            voice_revision=alpha_voice.revision,
        )
        assert "workspace_id" not in payload
        assert len(payloads) == 1 and "workspace_id" not in payloads[0]
        assert "workspace_id" not in sample_payload

        with monkeypatch.context() as patcher:
            patcher.setattr(
                voice_library,
                "voice_payload",
                lambda *_args, **_kwargs: pytest.fail(
                    "mixed voice owners must be rejected before projecting a row"
                ),
            )
            with pytest.raises(ValueError, match="workspace"):
                voice_payloads(
                    session,
                    harness.paths,
                    [alpha_voice, beta_voice],
                    workspace_id=alpha_workspace,
                )

    alpha_foreign_sample_id = _new_sample(
        harness,
        beta_workspace,
        alpha_id,
        beta_artifact.id,
        transcript="Cross-workspace sample for the alpha voice.",
    )
    with harness.database.session() as session:
        alpha_voice = session.get(Voice, alpha_id)
        alpha_foreign_sample = session.get(VoiceSample, alpha_foreign_sample_id)
        assert alpha_voice is not None and alpha_foreign_sample is not None
        with monkeypatch.context() as patcher:
            patcher.setattr(
                voice_library,
                "sample_file_status",
                lambda *_args, **_kwargs: pytest.fail(
                    "sample ownership must be checked before file projection"
                ),
            )
            with pytest.raises(ValueError, match="workspace"):
                voice_payload(
                    session,
                    harness.paths,
                    alpha_voice,
                    samples=[alpha_foreign_sample],
                    workspace_id=alpha_workspace,
                )


def test_voice_payloads_reject_a_malformed_cross_workspace_sample_before_file_access(
    voice_harness: VoiceHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = voice_harness
    alpha_workspace = harness.workspaces["alpha"]
    beta_workspace = harness.workspaces["beta"]
    alpha_id = _new_voice(harness, alpha_workspace, "Alpha voice")
    foreign_artifact = _new_sample_artifact(harness, beta_workspace, "alpha-voice-foreign-artifact")
    _new_sample(
        harness,
        beta_workspace,
        alpha_id,
        foreign_artifact.id,
        transcript="Corrupt cross-workspace sample.",
    )

    def fail_file_status(*_args: object, **_kwargs: object) -> Any:
        pytest.fail("voice sample owner must be checked before file access")

    with harness.database.session() as session:
        alpha_voice = session.get(Voice, alpha_id)
        assert alpha_voice is not None
        monkeypatch.setattr(voice_library, "sample_file_status", fail_file_status)
        with pytest.raises(ValueError, match="workspace"):
            voice_payloads(
                session,
                harness.paths,
                [alpha_voice],
                workspace_id=alpha_workspace,
            )


def test_foreign_profile_evidence_fails_before_managed_path_probe(
    voice_harness: VoiceHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = voice_harness
    foreign_artifact = _new_sample_artifact(
        harness, harness.workspaces["beta"], "foreign-profile-evidence"
    )

    def fail_managed_path(*_args: object, **_kwargs: object) -> Any:
        pytest.fail("foreign profile evidence must fail before probing its path")

    monkeypatch.setattr(DataPaths, "managed_path", fail_managed_path)
    with harness.database.session() as session:
        with pytest.raises(ValueError, match="another workspace"):
            validate_profile_evidence(
                session,
                harness.paths,
                {"evidence": {"tone": {"artifact_id": foreign_artifact.id}}},
                workspace_id=harness.workspaces["alpha"],
            )


def test_audio_cpp_voice_aliases_are_indexed_and_resolved_only_in_explicit_workspace(
    voice_harness: VoiceHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = voice_harness
    metadata = {
        "providers": {
            "audio_cpp": {
                "resource_kind": "linked_reference",
                "status": "ready",
                "voice_id": "shared-remote-id",
                "provider_voice_id": "shared-provider-id",
            }
        }
    }
    voice_ids: dict[str, str] = {}
    sample_ids: dict[str, str] = {}
    artifacts: dict[str, Artifact] = {}
    for key in ("alpha", "beta"):
        workspace_id = harness.workspaces[key]
        voice_ids[key] = _new_voice(
            harness, workspace_id, "Shared display alias", metadata=metadata
        )
        artifacts[key] = _new_sample_artifact(
            harness, workspace_id, f"{key}-linked-reference", value=900
        )
        sample_ids[key] = _new_sample(harness, workspace_id, voice_ids[key], artifacts[key].id)
    assert artifacts["alpha"].content_hash == artifacts["beta"].content_hash

    def no_openai_endpoint(*_args: object, **_kwargs: object) -> tuple[None, None]:
        return None, None

    monkeypatch.setattr(
        "pandrator.logic.tts_handler.resolve_openai_audio_endpoint",
        no_openai_endpoint,
    )
    with harness.database.session() as session:
        alpha_index = generation_voice_references.voice_reference_index(
            session,
            harness.paths,
            workspace_id=harness.workspaces["alpha"],
        )
        beta_index = generation_voice_references.voice_reference_index(
            session,
            harness.paths,
            workspace_id=harness.workspaces["beta"],
        )
        assert [item["id"] for item in alpha_index["shared display alias"]] == [voice_ids["alpha"]]
        assert [item["id"] for item in beta_index["shared display alias"]] == [voice_ids["beta"]]
        alpha_reference = resolve_audio_cpp_voice_reference(
            session,
            harness.paths,
            {"voice": "shared-remote-id"},
            workspace_id=harness.workspaces["alpha"],
        )
        beta_reference = resolve_audio_cpp_voice_reference(
            session,
            harness.paths,
            {"voice": "shared-remote-id"},
            workspace_id=harness.workspaces["beta"],
        )
        assert alpha_reference is not None and beta_reference is not None
        alpha_hash, alpha_path, alpha_artifact, alpha_sample = alpha_reference
        beta_hash, beta_path, beta_artifact, beta_sample = beta_reference
        assert alpha_hash == beta_hash == artifacts["alpha"].content_hash
        assert alpha_artifact.id == artifacts["alpha"].id
        assert beta_artifact.id == artifacts["beta"].id
        assert alpha_sample.id == sample_ids["alpha"]
        assert beta_sample.id == sample_ids["beta"]
        assert alpha_path == harness.paths.managed_path(artifacts["alpha"].relative_path)
        assert beta_path == harness.paths.managed_path(artifacts["beta"].relative_path)


def _install_synthetic_bundle_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, bytes]:
    source_root = tmp_path / "src"
    source = source_root / "tts_voices" / "sample_male_new.wav"
    _write_wav(source, frames=12000, value=1250)
    module_file = source_root / "pandrator" / "web" / "voice_library.py"
    module_file.parent.mkdir(parents=True, exist_ok=True)
    module_file.touch()
    monkeypatch.setattr(voice_library, "__file__", str(module_file))
    return source, source.read_bytes()


def _bundle_details(
    harness: VoiceHarness,
    voice_id: str,
) -> tuple[Voice, VoiceSample, Artifact, Path]:
    with harness.database.session() as session:
        voice = session.get(Voice, voice_id)
        assert voice is not None
        sample = session.scalar(select(VoiceSample).where(VoiceSample.voice_id == voice_id))
        assert sample is not None
        artifact = session.get(Artifact, sample.artifact_id)
        assert artifact is not None
        path = harness.paths.managed_path(artifact.relative_path)
        return voice, sample, artifact, path


def test_concurrent_first_bundle_reads_publish_one_complete_owned_sample(
    voice_harness: VoiceHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = voice_harness
    _source, source_bytes = _install_synthetic_bundle_source(tmp_path, monkeypatch)
    barrier = Barrier(4)

    def materialize() -> str:
        barrier.wait(timeout=10)
        voice = ensure_bundled_voice(
            harness.database,
            harness.paths,
            harness.artifacts,
            workspace_id=harness.workspaces["alpha"],
        )
        assert voice is not None
        return voice.id

    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = [pool.submit(materialize) for _ in range(4)]
        voice_ids = [future.result(timeout=20) for future in pending]
    assert len(set(voice_ids)) == 1
    assert _table_counts(harness) == (1, 1, 1)
    _voice, sample, artifact, path = _bundle_details(harness, voice_ids[0])
    assert sample.workspace_id == artifact.workspace_id == harness.workspaces["alpha"]
    assert path.read_bytes() == source_bytes
    assert list(path.parent.glob(".bundled-*")) == []


def test_bundled_voice_materializes_idempotently_per_workspace(
    voice_harness: VoiceHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = voice_harness
    source, source_bytes = _install_synthetic_bundle_source(tmp_path, monkeypatch)

    alpha = ensure_bundled_voice(
        harness.database,
        harness.paths,
        harness.artifacts,
        workspace_id=harness.workspaces["alpha"],
    )
    assert alpha is not None
    alpha_voice, alpha_sample, alpha_artifact, alpha_path = _bundle_details(harness, alpha.id)
    after_first_alpha = _table_counts(harness)
    alpha_again = ensure_bundled_voice(
        harness.database,
        harness.paths,
        harness.artifacts,
        workspace_id=harness.workspaces["alpha"],
    )
    assert alpha_again is not None
    alpha_again_voice, alpha_again_sample, alpha_again_artifact, alpha_again_path = _bundle_details(
        harness, alpha_again.id
    )
    assert alpha_again.id == alpha.id
    assert alpha_again_voice.id == alpha_voice.id
    assert alpha_again_sample.id == alpha_sample.id
    assert alpha_again_artifact.id == alpha_artifact.id
    assert alpha_again_path == alpha_path
    assert _table_counts(harness) == after_first_alpha

    beta = ensure_bundled_voice(
        harness.database,
        harness.paths,
        harness.artifacts,
        workspace_id=harness.workspaces["beta"],
    )
    assert beta is not None
    beta_voice, beta_sample, beta_artifact, beta_path = _bundle_details(harness, beta.id)
    solo = ensure_bundled_voice(harness.database, harness.paths, harness.artifacts)
    assert solo is not None
    solo_voice, solo_sample, solo_artifact, solo_path = _bundle_details(harness, solo.id)

    assert alpha_voice.id != beta_voice.id
    assert alpha_voice.id != solo_voice.id
    assert beta_voice.id != solo_voice.id
    assert solo_voice.id == BUNDLED_VOICE_KEY
    assert alpha_voice.name == beta_voice.name == solo_voice.name == BUNDLED_VOICE_NAME
    assert alpha_voice.workspace_id == harness.workspaces["alpha"]
    assert beta_voice.workspace_id == harness.workspaces["beta"]
    assert solo_voice.workspace_id == harness.personal_workspace_id
    assert alpha_sample.workspace_id == alpha_voice.workspace_id
    assert beta_sample.workspace_id == beta_voice.workspace_id
    assert solo_sample.workspace_id == solo_voice.workspace_id
    assert alpha_artifact.workspace_id == alpha_voice.workspace_id
    assert beta_artifact.workspace_id == beta_voice.workspace_id
    assert solo_artifact.workspace_id == solo_voice.workspace_id
    assert alpha_path != beta_path and beta_path != solo_path and alpha_path != solo_path
    assert (
        alpha_path.read_bytes() == beta_path.read_bytes() == solo_path.read_bytes() == source_bytes
    )
    assert source.read_bytes() == source_bytes
    assert all(
        sample.transcript == BUNDLED_SAMPLE_TRANSCRIPT
        for sample in (alpha_sample, beta_sample, solo_sample)
    )

    with harness.database.session() as session:
        default_voices = voices_in_workspace(session)
        assert [voice.id for voice in default_voices] == [BUNDLED_VOICE_KEY]


def test_bundled_voice_rejects_inactive_or_missing_workspace_before_path_mutation(
    voice_harness: VoiceHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = voice_harness
    source, source_bytes = _install_synthetic_bundle_source(tmp_path, monkeypatch)
    voices_before = set(harness.paths.voices.iterdir())
    counts_before = _table_counts(harness)

    with pytest.raises(ValueError, match="workspace"):
        ensure_bundled_voice(
            harness.database,
            harness.paths,
            harness.artifacts,
            workspace_id=harness.workspaces["archived"],
        )
    with pytest.raises(ValueError, match="workspace"):
        ensure_bundled_voice(
            harness.database,
            harness.paths,
            harness.artifacts,
            workspace_id="missing-voice-workspace",
        )
    assert set(harness.paths.voices.iterdir()) == voices_before
    assert _table_counts(harness) == counts_before

    active_voice = ensure_bundled_voice(
        harness.database,
        harness.paths,
        harness.artifacts,
        workspace_id=harness.workspaces["alpha"],
    )
    assert active_voice is not None
    _voice, _sample, _artifact, active_path = _bundle_details(harness, active_voice.id)
    bytes_before_archive = active_path.read_bytes()
    entries_before_archive = set(harness.paths.voices.iterdir())
    with harness.database.session() as session:
        workspace = session.get(Workspace, harness.workspaces["alpha"])
        assert workspace is not None
        workspace.state = "archived"

    with pytest.raises(ValueError, match="workspace"):
        ensure_bundled_voice(
            harness.database,
            harness.paths,
            harness.artifacts,
            workspace_id=harness.workspaces["alpha"],
        )
    assert active_path.read_bytes() == bytes_before_archive == source_bytes
    assert set(harness.paths.voices.iterdir()) == entries_before_archive
