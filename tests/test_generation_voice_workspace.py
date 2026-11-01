"""Persisted generation ownership using migrated scratch data and fake providers."""

from __future__ import annotations

import base64
import json
import threading
import wave
from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydub import AudioSegment

from pandrator.logic import tts_handler
from pandrator.runtime import DataPaths
from pandrator.web.accounts.execution_actor import HostActor, execution_actor_scope
from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.accounts.models import Workspace
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.generation_audio_identity import AudioIdentityContext, _hash, _material_settings
from pandrator.web.generation_cast_runtime import resolve_binding
from pandrator.web.generation_controls import get_generation_controls, save_generation_controls
from pandrator.web.generation_recast import _effective
from pandrator.web.generation_start_preparation import snapshot_guard
from pandrator.web.models import (
    Artifact,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    Voice,
    VoiceSample,
)
from pandrator.web.sessions import SessionService
from pandrator.web.workflow_handlers import WorkflowHandlers
from tests.web_test_support import prepare_web_test_data_root


@dataclass
class Case:
    database: Database
    paths: DataPaths
    handlers: WorkflowHandlers
    owners: list[str]
    sessions: list[str]
    revisions: list[str]
    segments: list[str]
    voices: list[str]
    artifacts: list[Artifact]


@pytest.fixture
def case(tmp_path):
    paths = prepare_web_test_data_root(tmp_path / "generation")
    database = Database(paths.database)
    handlers = WorkflowHandlers(database, paths)
    artifacts = ArtifactService(database, paths)
    owners, sessions, revisions, segments, voices, samples = [], [], [], [], [], []
    with database.session() as session:
        for name in ("alpha", "beta"):
            owner = Workspace(kind="team", name=name, state="active")
            session.add(owner)
            session.flush()
            owners.append(owner.id)
        owners.append(initial_personal_workspace(session).id)
    for index, owner in enumerate(owners):
        record = SessionService(database).create(f"Session {index}", workspace_id=owner)
        sessions.append(record.id)
        path = paths.voices / f"sample-{index}.wav"
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(8000)
            stream.writeframes(bytes([index, 0]) * 800)
        artifact = artifacts.register(path, kind="audio", role="voice_sample", workspace_id=owner)
        samples.append(artifact)
        with database.session() as session:
            voice = Voice(
                workspace_id=owner,
                name="Shared",
                metadata_json={
                    "providers": {
                        "audio_cpp": {
                            "status": "ready",
                            "resource_kind": "linked_reference",
                            "voice_id": "shared",
                        },
                        "kobold_qwen": {"status": "ready", "voice_id": "shared"},
                        "silero": {"status": "ready", "voice_id": "shared"},
                    }
                },
            )
            session.add(voice)
            plan = GenerationPlan(session_id=record.id)
            session.add(plan)
            session.flush()
            session.add(
                VoiceSample(
                    workspace_id=owner,
                    voice_id=voice.id,
                    artifact_id=artifact.id,
                    transcript=f"Owner {index}",
                    transcript_reviewed=True,
                )
            )
            revision = GenerationPlanRevision(
                plan_id=plan.id, revision_number=1, content_hash="text"
            )
            session.add(revision)
            session.flush()
            plan.active_revision_id = revision.id
            segment = GenerationSegment(plan_revision_id=revision.id, ordinal=1, text="Test text.")
            session.add(segment)
            session.flush()
            voices.append(voice.id)
            revisions.append(revision.id)
            segments.append(segment.id)
    try:
        yield Case(
            database, paths, handlers, owners, sessions, revisions, segments, voices, samples
        )
    finally:
        database.dispose()


def test_controls_reject_foreign_binding_without_mutation_after_commit(case):
    with case.database.session() as session:
        before = save_generation_controls(
            session,
            case.sessions[0],
            expected_revision=0,
            cast={"narrator": {"voice_id": case.voices[0]}},
        )
    with case.database.session() as session:
        with pytest.raises(ValueError, match="Managed voice does not exist"):
            save_generation_controls(
                session,
                case.sessions[0],
                expected_revision=1,
                characters=[{"id": "new", "display_name": "New"}],
                cast={"narrator": {"voice_id": case.voices[1]}},
            )
    with case.database.session() as session:
        assert get_generation_controls(session, case.sessions[0]) == before
        with pytest.raises(ValueError, match="Managed cast voice no longer exists"):
            resolve_binding(
                session,
                {"voice_id": case.voices[1]},
                {"service": "Silero"},
                workspace_id=case.owners[0],
            )
        assert (
            resolve_binding(
                session,
                {"voice_id": case.voices[0]},
                {"service": "Silero"},
                workspace_id=case.owners[0],
            )["voice"]
            == "shared"
        )
        # A legacy direct call defaults to the solo owner, never a global inventory.
        with pytest.raises(ValueError, match="Managed cast voice no longer exists"):
            resolve_binding(session, {"voice_id": case.voices[0]}, {"service": "Silero"})
        assert resolve_binding(session, {"voice_id": case.voices[2]}, {"service": "Silero"})


def test_identity_initializes_before_cache_and_keeps_legacy_hash_shape(case):
    settings = {"service": "Silero", "model": "v3_en", "voice": "en_0", "language": "en"}
    with case.database.session() as session:
        context = AudioIdentityContext(session, {"tts": settings})
        assert context.workspace_id is None and context.voices == {}
        first = session.get(GenerationSegment, case.segments[0])
        foreign = session.get(GenerationSegment, case.segments[1])
        identity = context.for_segment(first)
        assert identity == {
            "schema_version": 1,
            "settings_hash": _hash({"tts": _material_settings({"tts": settings}), "rvc": {}}),
            "voice_reference_hash": _hash([]),
        }
        assert context.cache
        with pytest.raises(ValueError, match="workspace"):
            context.for_segment(foreign)
        assert context.for_segment(first) == identity
        owner = session.get(Workspace, case.owners[0])
        owner.state = "archived"
        with pytest.raises(ValueError, match="inactive"):
            context.for_segment(first)


def test_recast_inventory_can_initialize_without_performance_compilation(case, monkeypatch):
    with case.database.session() as session:
        context = AudioIdentityContext(session, {"tts": {"service": "Silero"}})
        monkeypatch.setattr(context, "_with_performance", Mock(side_effect=AssertionError))
        context._ensure_segment_workspace(session.get(GenerationSegment, case.segments[0]))
        assert context.workspace_id == case.owners[0]
        assert context.voices == {}
        context._ensure_voice_references()
        assert case.voices[0].casefold() in context.voices
        assert case.voices[1].casefold() not in context.voices
        assert _effective(session, context, {"voice_id": case.voices[0]}, {"service": "Silero"})
        with pytest.raises(ValueError, match="Managed cast voice no longer exists"):
            _effective(session, context, {"voice_id": case.voices[1]}, {"service": "Silero"})


def test_snapshot_guard_excludes_foreign_inventory_and_rejects_wrong_revision(case):
    def guard():
        with case.database.session() as session:
            return snapshot_guard(session, case.sessions[0], case.revisions[0])

    initial = guard()
    with case.database.session() as session:
        session.get(Voice, case.voices[1]).revision += 1
        session.get(Artifact, case.artifacts[1].id).content_hash = "changed-foreign"
    assert guard() == initial
    with case.database.session() as session:
        session.get(Voice, case.voices[0]).revision += 1
    assert guard() != initial
    with case.database.session() as session:
        with pytest.raises(ValueError, match="session"):
            snapshot_guard(session, case.sessions[0], case.revisions[1])


@pytest.mark.parametrize("selected", ["Shared", "shared"])
def test_audio_cpp_callback_scopes_identical_aliases_and_byte_cache(case, selected):
    settings = {"service": "audio_cpp", "voice": selected}
    for index in (0, 1, 2):
        result = case.handlers.prepare_audio_cpp_voice_reference(
            settings, workspace_id=case.owners[index]
        )
        assert result["audio_cpp_voice_ref_hash"] == case.artifacts[index].content_hash
        assert result["audio_cpp_reference_text"] == f"Owner {index}"
        assert (
            base64.b64decode(result["audio_cpp_voice_ref"]["data"].split(",")[1])
            == (case.paths.voices / f"sample-{index}.wav").read_bytes()
        )
    assert (
        case.handlers.prepare_audio_cpp_voice_reference(settings)["audio_cpp_reference_text"]
        == "Owner 2"
    )
    with case.database.session() as session:
        session.get(Workspace, case.owners[0]).state = "archived"
    with pytest.raises(ValueError, match="inactive"):
        case.handlers.prepare_audio_cpp_voice_reference(settings, workspace_id=case.owners[0])


def _qwen_settings(voice="shared"):
    return {"service": "Qwen3 TTS", "model": "Voice Cloning", "speaker": voice}


def test_qwen_shared_alias_restores_selected_owner_and_namespaces_cache(case, monkeypatch):
    catalogue = Mock(return_value=[])
    monkeypatch.setattr(tts_handler, "get_kobold_qwen_voice_catalog", catalogue)
    publish = Mock()
    monkeypatch.setattr(case.handlers, "publish_voice", publish)
    verified = set()
    for index in (0, 1, 2):
        kwargs = {} if index == 2 else {"workspace_id": case.owners[index]}
        case.handlers._ensure_qwen_cloned_voice(
            _qwen_settings(),
            base_url="fake://qwen",
            verified=verified,
            cancel_event=threading.Event(),
            **kwargs,
        )
        assert publish.call_args.args[0]["voice_id"] == case.voices[index]
        assert publish.call_args.args[0]["workspace_id"] == case.owners[index]
    assert catalogue.call_count == 3 and publish.call_count == 3
    case.handlers._ensure_qwen_cloned_voice(
        _qwen_settings(),
        base_url="fake://qwen",
        verified=verified,
        cancel_event=threading.Event(),
        workspace_id=case.owners[0],
    )
    assert catalogue.call_count == 3
    assert len(verified) == 3
    with case.database.session() as session:
        session.get(Workspace, case.owners[0]).state = "archived"
    with pytest.raises(ValueError, match="inactive"):
        case.handlers._ensure_qwen_cloned_voice(
            _qwen_settings(),
            base_url="fake://qwen",
            verified=verified,
            cancel_event=threading.Event(),
            workspace_id=case.owners[0],
        )


def test_qwen_remote_and_cache_cannot_authorize_known_foreign_alias(case, monkeypatch):
    with case.database.session() as session:
        voice = session.get(Voice, case.voices[1])
        voice.metadata_json = {"providers": {"kobold_qwen": {"voice_id": "foreign-only"}}}
    catalogue = Mock(return_value=[{"id": "foreign-only", "type": "cloned"}])
    monkeypatch.setattr(tts_handler, "get_kobold_qwen_voice_catalog", catalogue)
    publish = Mock()
    monkeypatch.setattr(case.handlers, "publish_voice", publish)
    for verified in (
        set(),
        {"foreign-only", json.dumps([case.owners[0], "foreign-only"], separators=(",", ":"))},
    ):
        with pytest.raises(ValueError, match="unavailable in this workspace"):
            case.handlers._ensure_qwen_cloned_voice(
                _qwen_settings("foreign-only"),
                base_url="fake://qwen",
                verified=verified,
                cancel_event=threading.Event(),
                workspace_id=case.owners[0],
            )
    catalogue.assert_not_called()
    publish.assert_not_called()
    # An untracked externally configured voice retains its existing catalogue behavior.
    catalogue.return_value = [{"id": "external", "type": "cloned"}]
    case.handlers._ensure_qwen_cloned_voice(
        _qwen_settings("external"),
        base_url="fake://qwen",
        verified=set(),
        cancel_event=threading.Event(),
        workspace_id=case.owners[0],
    )
    catalogue.assert_called_once()
    publish.assert_not_called()


@execution_actor_scope(HostActor("pandrator-cli"))
def test_preview_forwards_owner_and_publishes_owned_artifact(case, monkeypatch):
    import pandrator.web.workflow_handlers as module

    monkeypatch.setattr(
        module, "hydrate_tts_settings", lambda _db, _paths, settings, **_kwargs: settings
    )
    prepare = Mock(side_effect=lambda settings, **_kwargs: settings)
    ensure = Mock()
    monkeypatch.setattr(case.handlers, "prepare_audio_cpp_voice_reference", prepare)
    monkeypatch.setattr(case.handlers, "_ensure_qwen_cloned_voice", ensure)
    monkeypatch.setattr(
        case.handlers.tts_providers, "synthesize", Mock(return_value=AudioSegment.silent(40))
    )
    monkeypatch.setattr(case.handlers, "_record_tts_usage", Mock())
    payload = {
        "text": "Preview.",
        "settings": {"service": "Silero"},
        "workspace_id": case.owners[1],
        "_job_id": "same-untrusted-preview-id",
    }
    result = case.handlers.preview_tts_voice(payload, lambda *_args: None, threading.Event())
    assert prepare.call_args.kwargs["workspace_id"] == case.owners[1]
    assert ensure.call_args.kwargs["workspace_id"] == case.owners[1]
    with case.database.session() as session:
        first = session.get(Artifact, result["artifact_id"])
        assert first.workspace_id == case.owners[1]
        first_path = case.paths.managed_path(first.relative_path)
        first_bytes = first_path.read_bytes()

    # A caller-controlled/replayed ID cannot overwrite another owner's preview.
    other = case.handlers.preview_tts_voice(
        {**payload, "workspace_id": case.owners[0]},
        lambda *_args: None,
        threading.Event(),
    )
    with case.database.session() as session:
        second = session.get(Artifact, other["artifact_id"])
        assert second.workspace_id == case.owners[0]
        assert case.paths.managed_path(second.relative_path) != first_path
    assert first_path.read_bytes() == first_bytes

    def archive(*_args, **_kwargs):
        with case.database.session() as session:
            session.get(Workspace, case.owners[1]).state = "archived"
        return AudioSegment.silent(40)

    monkeypatch.setattr(case.handlers.tts_providers, "synthesize", archive)
    with pytest.raises(ValueError, match="inactive"):
        case.handlers.preview_tts_voice(payload, lambda *_args: None, threading.Event())


@pytest.mark.parametrize("runner", ["automatic", "resumable"])
@pytest.mark.parametrize("batch_size", [1, 2])
@execution_actor_scope(HostActor("pandrator-cli"))
def test_generation_callbacks_receive_persisted_owner(case, monkeypatch, runner, batch_size):
    from pandrator.web import speech_plan_workspace
    from pandrator.web.workflow_generation_automatic import generate_audio
    from pandrator.web.workflow_generation_execution import run_generation

    class ReachedScopedCallback(Exception):
        pass

    prepare = Mock(side_effect=lambda settings, **_kwargs: settings)
    ensure = Mock(side_effect=ReachedScopedCallback)
    settings = {"service": "Silero", "voice": "en_0", "model": "v3_en"}
    common = {
        "_hydrate_tts_settings": lambda _database, _paths, values, **_kwargs: values,
        "_negotiated_tts_batch_size": lambda *_args, **_kwargs: batch_size,
        "prepare_audio_cpp_voice_reference": prepare,
        "_ensure_qwen_cloned_voice": ensure,
    }
    monkeypatch.setattr(
        speech_plan_workspace, "freeze_speech_snapshot", lambda *_args, **_kwargs: None
    )
    if runner == "automatic":
        context = replace(
            case.handlers._automatic_generation_context(),
            **common,
            _optimize_generation_texts=lambda _sid, _ids, texts, *_args, **_kwargs: (texts, ""),
        )
        source = case.paths.uploads / "prepared.json"
        source.write_text(json.dumps([{"text": "Test text."}]))
        artifact = Artifact(id=case.artifacts[1].id, role="prepared_text", metadata_json={})

        def invoke():
            return generate_audio(
                context,
                case.sessions[1],
                artifact,
                source,
                settings,
                lambda *_args: None,
                threading.Event(),
                role="audiobook_audio",
            )
    else:
        with case.database.session() as session:
            run = GenerationRun(
                session_id=case.sessions[1],
                plan_revision_id=case.revisions[1],
                settings_snapshot_json={"tts": settings, "speech_plan_frozen": True},
            )
            session.add(run)
            session.flush()
            run_id = run.id
        context = replace(
            case.handlers._generation_execution_context(),
            **common,
            _voiceover_second_pass=lambda *_args, **_kwargs: None,
        )

        def invoke():
            return run_generation(
                context, {"generation_run_id": run_id}, lambda *_args: None, threading.Event()
            )

    with pytest.raises(ReachedScopedCallback):
        invoke()
    assert prepare.call_args.kwargs["workspace_id"] == case.owners[1]
    assert ensure.call_args.kwargs["workspace_id"] == case.owners[1]


def test_invalid_roots_fail_before_identity_cache_and_generation_hydration(case, monkeypatch):
    from pandrator.web.workflow_generation_automatic import generate_audio

    with case.database.session() as session:
        context = AudioIdentityContext(session, {"tts": {"service": "Silero"}})
        context.cache[("", "", "")] = {"cached": True}
        segment = GenerationSegment(plan_revision_id="missing", ordinal=1, text="Missing root.")
        with pytest.raises(ValueError, match="speech plan"):
            context.for_segment(segment)
        session.get(Workspace, case.owners[0]).state = "archived"
    hydrate = Mock(side_effect=AssertionError("Provider hydration must not run"))
    context = replace(case.handlers._automatic_generation_context(), _hydrate_tts_settings=hydrate)
    with pytest.raises(ValueError, match="inactive"):
        generate_audio(
            context,
            case.sessions[0],
            case.artifacts[0],
            Path("unused.json"),
            {},
            lambda *_args: None,
            threading.Event(),
            role="audiobook_audio",
        )
    hydrate.assert_not_called()


def test_freeze_and_render_preview_pass_the_persisted_session_owner(case, monkeypatch):
    from pandrator.web import generation_voice_references
    from pandrator.web.generation_cast_runtime import freeze_cast_snapshot, preview_render_parts

    original_index = generation_voice_references.voice_reference_index
    index = Mock(wraps=original_index)
    monkeypatch.setattr(generation_voice_references, "voice_reference_index", index)
    settings = {"service": "Silero", "casting_enabled": True, "voice": "en_0"}
    with case.database.session() as session:
        segment = session.get(GenerationSegment, case.segments[1])
        segment.voice_id = case.voices[1]
        snapshot = {}
        freeze_cast_snapshot(session, case.revisions[1], snapshot, settings)
        assert index.call_args.kwargs["workspace_id"] == case.owners[1]
        assert snapshot["generation_control_snapshot"]["resolved_bindings"]
        assert preview_render_parts(
            session, case.sessions[1], segment.text, segment.id, settings, None
        )
        segment.voice_id = case.voices[0]
        with pytest.raises(ValueError, match="Managed cast voice no longer exists"):
            freeze_cast_snapshot(session, case.revisions[1], {}, settings)
        with pytest.raises(ValueError, match="Managed cast voice no longer exists"):
            preview_render_parts(
                session, case.sessions[1], segment.text, segment.id, settings, None
            )


def test_audio_cpp_identity_resolves_own_sample_for_identical_alias(case):
    snapshot = {"tts": {"service": "audio_cpp", "voice": "Shared"}}
    with case.database.session() as session:
        contexts = [AudioIdentityContext(session, snapshot) for _ in (0, 1)]
        identities = [
            context.for_segment(session.get(GenerationSegment, case.segments[index]))
            for index, context in enumerate(contexts)
        ]
        assert identities[0]["voice_reference_hash"] != identities[1]["voice_reference_hash"]
        assert identities[0]["settings_hash"] == identities[1]["settings_hash"]
        assert all(
            set(identity) == {"schema_version", "settings_hash", "voice_reference_hash"}
            for identity in identities
        )
