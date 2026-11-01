"""Keep a pinned caption master independent of speech-only exclusions."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from mcp import Client
from pydub import AudioSegment
from sqlalchemy import event, select

from pandrator.logic.dubbing.srt_utils import parse_srt, srt_to_vtt
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.generation_subtitles import capture_display_subtitle_snapshot
from pandrator.web.jobs import JobQueue
from pandrator.web.models import (
    ArtifactEdge,
    AudioTake,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationSegment,
)
from pandrator.web.sessions import SessionService
from pandrator.web.settings_policy import validate_output_settings
from pandrator.web.tts_providers import TtsCapabilities
from pandrator.web.workflow_handlers import WorkflowHandlers
from pandrator.web.workflows import WorkflowService
from pandrator.web.workspace import GenerationService
from pandrator.web.workspace_settings import WorkspaceSettingsService
from pandrator_mcp.context import build_runtime
from pandrator_mcp.schemas.e2e import PlanExportVariantInput
from pandrator_mcp.schemas.workflow import PlanOrchestratedWorkflowInput
from pandrator_mcp.server import build_server
from pandrator_mcp.settings import McpSettings
from tests.web_test_support import prepare_web_test_data_root

MASTER = (
    "1\n00:00:01,000 --> 00:00:03,000\n[Song: Spirit of Life]\n\n"
    "2\n00:00:04,000 --> 00:00:04,100\nAmen.\n"
)
TRANSLATION = MASTER.replace("[Song: Spirit of Life]", "[Lied: Geist des Lebens]")


@pytest.fixture
def case(tmp_path):
    paths = prepare_web_test_data_root(str(tmp_path))
    database = Database(paths.database)
    artifacts = ArtifactService(database, paths)
    queue = JobQueue(database)
    settings = WorkspaceSettingsService(database)
    record = SessionService(database).create("Selected caption master", workflow_kind="subtitles")
    directory = paths.sessions / record.storage_key
    directory.mkdir()

    def artifact(name, text, role):
        path = directory / name
        path.write_text(text, encoding="utf-8")
        return artifacts.register(path, kind="srt", role=role, session_id=record.id)

    master = artifact("master.srt", MASTER, "correction")
    translation = artifact("translation.srt", TRANSLATION, "translation")
    with database.session() as session:
        plan = GenerationPlan(session_id=record.id)
        session.add(plan)
        session.flush()
        revision = GenerationPlanRevision(
            plan_id=plan.id, revision_number=1, content_hash="song-amen-master",
            settings_json={"_source_artifact_id": master.id},
        )
        session.add(revision)
        session.flush()
        plan.active_revision_id = revision.id
        session.add_all([
            GenerationSegment(
                plan_revision_id=revision.id, ordinal=0, node_kind="subtitle_cue",
                text="[Song: Spirit of Life]", optimized_text="", removed=True,
                source_segment_ids_json=[1],
            ),
            GenerationSegment(
                plan_revision_id=revision.id, ordinal=1, node_kind="subtitle_cue",
                text="Amen.", optimized_text="", removed=False,
                source_segment_ids_json=[2],
            ),
        ])
        revision_id = revision.id
    try:
        yield SimpleNamespace(
            database=database, artifacts=artifacts, queue=queue, settings=settings,
            record=record, master=master, translation=translation, artifact=artifact,
            revision_id=revision_id, workflow=WorkflowService(database, queue),
            handlers=WorkflowHandlers(database, paths),
            generation=GenerationService(database, queue, settings, artifacts),
        )
    finally:
        database.dispose()


def test_selected_artifact_settings_and_mcp_acceptance(case):
    validate_output_settings({"subtitle_text_source": "selected_artifact"})
    updated = case.settings.update(
        case.record.id, "output", 0, {"subtitle_text_source": "selected_artifact"}
    )
    assert updated["effective"]["subtitle_text_source"] == "selected_artifact"
    assert PlanExportVariantInput(
        session_id=case.record.id, subtitle_text_source="selected_artifact"
    ).subtitle_text_source == "selected_artifact"
    assert PlanOrchestratedWorkflowInput(
        session_id=case.record.id, goal="Export captions", subtitle_text_source="selected_artifact"
    ).subtitle_text_source == "selected_artifact"


def test_registered_mcp_tools_advertise_selected_artifact(tmp_path):
    async def probe():
        runtime = build_runtime(McpSettings(
            target_name="unconfigured", configuration_path=tmp_path / "missing-targets.json"
        ))
        async with Client(build_server(runtime), mode="auto", raise_exceptions=True) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            for name in ("pandrator_plan_export_variant", "pandrator_plan_orchestrated_workflow"):
                assert tools[name].input_schema["properties"]["subtitle_text_source"]["enum"] == [
                    "current", "generation_run", "selected_artifact",
                ]

    asyncio.run(probe())


@pytest.mark.parametrize("invalid", [None, "", "selected", "artifact", 1, True])
def test_invalid_source_rejected_before_database_lookup(invalid):
    session = Mock()
    with pytest.raises(ValueError, match="subtitle_text_source"):
        validate_output_settings({"subtitle_text_source": invalid})
    with pytest.raises(ValueError, match="subtitle_text_source"):
        capture_display_subtitle_snapshot(session, "session", {"subtitle_text_source": invalid})
    session.get.assert_not_called()
    session.scalar.assert_not_called()


def test_selected_capture_does_not_load_any_plan_or_unused_run(case):
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    with case.database.session() as session:
        event.listen(session.bind, "before_cursor_execute", capture)
        try:
            with patch.object(session, "get", side_effect=AssertionError("loaded row")):
                assert capture_display_subtitle_snapshot(session, case.record.id, {
                    "subtitle_text_source": "selected_artifact",
                    "generation_run_id": "unused-nonexistent-run",
                }) is None
        finally:
            event.remove(session.bind, "before_cursor_execute", capture)
    assert statements == []


def _plan_and_takes(case):
    with case.database.session() as session:
        rows = list(session.scalars(select(GenerationSegment).where(
            GenerationSegment.plan_revision_id == case.revision_id
        ).order_by(GenerationSegment.ordinal)))
        return (
            [(row.id, row.text, row.optimized_text, row.removed, row.revision) for row in rows],
            [(take.id, take.generation_segment_id, take.generation_run_id, take.artifact_id,
              take.status, take.is_active)
             for take in session.scalars(select(AudioTake).order_by(AudioTake.id))],
        )


@pytest.mark.parametrize("format_name", ["srt", "vtt"])
def test_selected_master_freezes_both_tracks_and_preserves_caption_windows(case, format_name):
    before = _plan_and_takes(case)
    _, source_path = case.artifacts.resolve(case.master.id)
    source_bytes = source_path.read_bytes()
    job = case.workflow.run_stage(case.record.id, "export", {
        "export_mode": "subtitles", "subtitle_format": format_name,
        "subtitle_selection": "dual", "subtitle_text_source": "selected_artifact",
    })
    payload = case.queue.get(job.id).payload_json
    assert payload["settings"]["subtitle_text_source"] == "selected_artifact"
    assert payload["display_subtitle_snapshot"] is None
    tracks = payload["subtitle_track_snapshot"]["tracks"]
    assert {track["artifact_id"] for track in tracks} == {case.master.id, case.translation.id}
    assert next(track for track in tracks if track["artifact_id"] == case.master.id)[
        "content_hash"
    ] == hashlib.sha256(source_bytes).hexdigest()
    later_master = case.artifact("later-master.srt", MASTER.replace("Amen.", "Later edit."), "correction")
    later_translation = case.artifact(
        "later-translation.srt", TRANSLATION.replace("Amen.", "Later translation."), "translation"
    )
    result = case.handlers.export(payload, lambda *_: None, threading.Event())
    assert len(result["artifact_ids"]) == 2
    expected = {"export_subtitle_source": MASTER, "export_subtitle_translation": TRANSLATION}
    for artifact_id in result["artifact_ids"]:
        output, path = case.artifacts.resolve(artifact_id)
        assert path.read_text(encoding="utf-8") == (
            expected[output.role] if format_name == "srt" else srt_to_vtt(expected[output.role])
        )
        assert output.metadata_json["subtitle_diagnostics"]["text_integrity"]["matches"] is True
    with case.database.session() as session:
        parents = set(session.scalars(select(ArtifactEdge.parent_artifact_id).where(
            ArtifactEdge.child_artifact_id.in_(result["artifact_ids"])
        )))
    assert parents == {case.master.id, case.translation.id}
    assert parents.isdisjoint({later_master.id, later_translation.id})
    assert source_path.read_bytes() == source_bytes
    assert _plan_and_takes(case) == before


def test_current_mode_still_omits_only_removed_song_label(case):
    resolved = case.workflow.resolve_stage(case.record.id, "export", {
        "export_mode": "subtitles", "subtitle_selection": "source", "subtitle_text_source": "current",
    })
    assert [row["removed"] for row in resolved.payload["display_subtitle_snapshot"]["segments"]] == [True, False]
    result = case.handlers.export(resolved.payload, lambda *_: None, threading.Event())
    _, path = case.artifacts.resolve(result["artifact_ids"][0])
    assert [(cue.text, cue.start_ms, cue.end_ms) for cue in parse_srt(path.read_text())] == [
        ("Amen.", 4000, 4100)
    ]


def test_real_generation_start_and_handler_request_only_spoken_amen(case):
    requested = []

    def synthesize(text, _settings, **_options):
        requested.append(text)
        return AudioSegment.silent(duration=20)

    with patch.object(case.handlers.tts_providers, "synthesize", side_effect=synthesize), patch.object(
        case.handlers.tts_providers, "synthesis_capabilities",
        return_value=TtsCapabilities(batch_synthesis=False, streaming_batch=False),
    ):
        run = case.generation.start(
            case.record.id, speech_plan_revision_id=case.revision_id,
            run_override={"tts": {
                "service": "openai", "model": "test-model", "voice": "test-voice",
                "language": "en", "tts_batch_size": 1,
            }},
        )
        result = case.handlers.run_generation(
            {"generation_run_id": run["id"]}, lambda *_: None, threading.Event()
        )
    assert result["status"] == "completed"
    assert result["generated"] == 1
    assert requested == ["Amen."]
    rows, _takes = _plan_and_takes(case)
    assert [(row[1], row[2], row[3]) for row in rows] == [
        ("[Song: Spirit of Life]", "", True), ("Amen.", "", False),
    ]
    before_export = _plan_and_takes(case)
    job = case.workflow.run_stage(case.record.id, "export", {
        "export_mode": "subtitles", "subtitle_selection": "source",
        "subtitle_text_source": "selected_artifact",
    })
    case.handlers.export(case.queue.get(job.id).payload_json, lambda *_: None, threading.Event())
    assert _plan_and_takes(case) == before_export
