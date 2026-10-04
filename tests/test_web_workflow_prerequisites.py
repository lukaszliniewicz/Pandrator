"""Persisted prerequisite decisions and CLI queueing without an execution graph."""

from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy.pool import Pool

from pandrator.runtime import DataPaths
from pandrator.web import cli
from pandrator.web.credentials import database_reference
from pandrator.web.database import SCHEMA_HEAD, Database
from pandrator.web.legacy_migration import GENERATION_PROMOTION_VERSION
from pandrator.web.models import (
    Artifact,
    ArtifactEdge,
    Job,
    OutcomePlan,
    Provider,
    ProviderModel,
    SessionRecord,
    SessionSetting,
    SessionSource,
    SessionStageSelection,
    SourceAsset,
    StoredCredential,
)
from pandrator.web.settings_policy import adapt_runtime_settings
from pandrator.web.workflow_handlers import WorkflowHandlers
from pandrator.web.workspace_settings import WorkspaceSettingsService
from tests.web_test_support import prepare_web_test_data_root

FINGERPRINT = {
    "backend": "llm",
    "target_language": "pl",
    "model": "openai/fixture-model",
    "instructions": "",
}
EXECUTION_CONSTRUCTORS = (
    "pandrator.web.workflow_handlers.ArtifactService",
    "pandrator.web.media_edit.MediaEditService",
    "pandrator.web.tts_providers.TtsProviderRegistry",
    "pandrator.web.workflow_handlers.JobQueue",
    "pandrator.web.quick_transcription.QuickTranscriptionService",
    "pandrator.web.subtitle_evidence.SubtitleEvidenceService",
    "pandrator.web.job_handler_domains.build_workflow_handler_registry",
)


@dataclass
class Graph:
    database: Database
    paths: DataPaths
    connection: sqlite3.Connection
    pool: Pool

    @contextmanager
    def unmodified(self) -> Iterator[None]:
        rows = list(self.connection.iterdump())
        directories = sorted(
            str(p.relative_to(self.paths.root)) for p in self.paths.root.rglob("*") if p.is_dir()
        )
        try:
            yield
        finally:
            assert list(self.connection.iterdump()) == rows
            assert (
                sorted(
                    str(p.relative_to(self.paths.root))
                    for p in self.paths.root.rglob("*")
                    if p.is_dir()
                )
                == directories
            )
            assert self.connection.execute("SELECT 1").fetchone() == (1,)
            assert self.database.engine.pool is self.pool


def artifact(
    identifier: str, role: str, *, session_id: str = "voice", metadata: dict[str, Any] | None = None
) -> Artifact:
    return Artifact(
        id=identifier,
        session_id=session_id,
        kind="srt",
        role=role,
        relative_path=f"uploads/{identifier}.srt",
        content_hash="same-content",
        metadata_json=metadata or {},
    )


@pytest.fixture
def graph(tmp_path: Path) -> Iterator[Graph]:
    paths = prepare_web_test_data_root(tmp_path)
    paths.migration_marker.write_text(
        json.dumps(
            {
                "status": "complete",
                "web_schema": SCHEMA_HEAD,
                "generation_promotion_version": GENERATION_PROMOTION_VERSION,
            }
        ),
        encoding="utf-8",
    )
    database = Database(paths.database)
    try:
        with database.session() as session:
            session.add_all(
                [
                    SessionRecord(
                        id="voice",
                        storage_key="voice",
                        name="Fixture",
                        workflow_kind="voiceover",
                        target_language="pl",
                        included_stages_json=["translate", "generate_audio"],
                    ),
                    SessionRecord(
                        id="foreign",
                        storage_key="foreign",
                        name="Foreign",
                        workflow_kind="voiceover",
                    ),
                    Provider(
                        id="fixture-provider",
                        provider_key="openai",
                        label="Fixture",
                        secret_ref=database_reference("fixture-llm"),
                    ),
                    StoredCredential(
                        key="fixture-llm",
                        label="Synthetic test credential",
                        secret_value="synthetic-fixture-credential",
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    ProviderModel(
                        id="fixture-model",
                        provider_id="fixture-provider",
                        model_id="fixture-model",
                        is_default=True,
                        is_active=True,
                    ),
                    artifact("source", "upload", metadata={"original_filename": "source.srt"}),
                    artifact(
                        "translated",
                        "translation",
                        metadata={
                            "source_artifact_id": "source",
                            "source_content_hash": "same-content",
                            "settings_fingerprint": FINGERPRINT,
                        },
                    ),
                    OutcomePlan(
                        session_id="voice",
                        value_json={
                            "inputs": {"translation": "source", "generation": "translation"},
                            "transformations": {"translation": True, "generate_audio": True},
                        },
                    ),
                ]
            )
            session.flush()
            session.add(
                SessionStageSelection(
                    session_id="voice", stage_key="translate", artifact_id="translated"
                )
            )
        for identifier in ("source", "translated"):
            (paths.uploads / f"{identifier}.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nFixture\n", encoding="utf-8"
            )
        with database.engine.connect() as checkout:
            checkout.exec_driver_sql("SELECT 1")
            native = checkout.connection.driver_connection
            assert isinstance(native, sqlite3.Connection)
        yield Graph(database, paths, native, database.engine.pool)
    finally:
        database.dispose()


@pytest.fixture(params=["workflow", "prerequisites"])
def owner(request: pytest.FixtureRequest, graph: Graph) -> Iterator[Any]:
    with ExitStack() as stack:
        if request.param == "workflow":
            value = WorkflowHandlers(graph.database, graph.paths)
            stack.callback(value.tts_providers.close)
        else:
            for name in EXECUTION_CONSTRUCTORS:
                stack.enter_context(
                    patch(name, side_effect=AssertionError("execution graph constructed"))
                )
            module = importlib.import_module("pandrator.web.workflow_prerequisites")
            value = module.WorkflowPrerequisiteService(graph.database, graph.paths)
        yield value


def mismatch(
    *,
    stored: dict[str, Any] | None = FINGERPRINT,
    current: dict[str, Any] | None = FINGERPRINT,
    reasons: list[str],
    fields: list[str] | None = None,
    stage: str = "translate",
) -> list[dict[str, Any]]:
    return [
        {
            "stage": stage,
            "changed_fields": fields or [],
            "reasons": reasons,
            "stored": stored,
            "current": current,
        }
    ]


def raw_translation_settings(graph: Graph) -> dict[str, Any]:
    resolved, _ = WorkspaceSettingsService(graph.database).resolve(
        "voice", ["translation", "subtitles"]
    )
    settings: dict[str, Any] = {}
    for section in ("translation", "subtitles"):
        settings.update(adapt_runtime_settings(section, resolved[section]))
    return settings


def test_semantic_reuse_and_changed_language(owner: Any, graph: Graph) -> None:
    with graph.unmodified():
        assert owner.settings_mismatches("voice") == []
    with graph.database.session() as session:
        record = session.get(SessionRecord, "voice")
        assert record is not None
        record.target_language = "de"
    with graph.unmodified():
        assert owner.settings_mismatches("voice") == mismatch(
            current={**FINGERPRINT, "target_language": "de"},
            reasons=["settings_changed"],
            fields=["target_language"],
        )


def test_legacy_raw_hash_fallback(owner: Any, graph: Graph) -> None:
    with graph.database.session() as session:
        row = session.get(Artifact, "translated")
        assert row is not None
        row.metadata_json = {"source_artifact_id": "source"}
    with graph.unmodified():
        assert owner.settings_mismatches("voice") == mismatch(
            stored=None, current=None, reasons=["settings_unverifiable"]
        )
    settings = raw_translation_settings(graph)
    digest = hashlib.sha256(
        json.dumps(
            settings, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
    ).hexdigest()
    with graph.database.session() as session:
        row = session.get(Artifact, "translated")
        assert row is not None
        row.settings_hash = digest
    with graph.unmodified():
        assert owner.settings_mismatches("voice") == []


@pytest.mark.parametrize("with_edge", [False, True])
def test_translation_same_bytes_require_source_identity_or_edge(
    owner: Any, graph: Graph, with_edge: bool
) -> None:
    with graph.database.session() as session:
        session.add(artifact("replacement", "upload"))
        session.flush()
        session.add(
            SessionSetting(
                session_id="voice",
                section="translation",
                value_json={"source_artifact_id": "replacement"},
            )
        )
        if with_edge:
            session.add(
                ArtifactEdge(parent_artifact_id="replacement", child_artifact_id="translated")
            )
    with graph.unmodified():
        assert owner.settings_mismatches("voice") == (
            [] if with_edge else mismatch(reasons=["source_lineage_changed"])
        )


def test_attached_foreign_translation_input_is_safe(owner: Any, graph: Graph) -> None:
    with graph.database.session() as session:
        session.add(artifact("foreign-input", "upload", session_id="foreign"))
        session.flush()
    with graph.unmodified():
        assert owner._persisted_translation_input("voice", "foreign-input") is None
    with graph.database.session() as session:
        session.add(
            SourceAsset(
                id="attached", artifact_id="foreign-input", display_name="Fixture", kind="srt"
            )
        )
        session.flush()
        session.add(SessionSource(session_id="voice", source_asset_id="attached"))
    with graph.unmodified():
        attached = owner._persisted_translation_input("voice", "foreign-input")
        assert attached is not None and attached.id == "foreign-input"
        assert owner._persisted_translation_input("voice", "missing") is None


def test_audiobook_historical_selection_is_read_without_files(owner: Any, graph: Graph) -> None:
    with graph.database.session() as session:
        record = session.get(SessionRecord, "voice")
        assert record is not None
        record.workflow_kind = "audiobook"
        record.included_stages_json = ["prepare_text", "generate_audio"]
        session.add(artifact("prepared", "prepared_text"))
        session.flush()
        row = session.get(Artifact, "prepared")
        assert row is not None
        row.state = "stale"
        session.add(
            SessionStageSelection(
                session_id="voice", stage_key="prepare_text", artifact_id="prepared"
            )
        )
    with graph.unmodified():
        assert owner.settings_mismatches("voice") == mismatch(
            stage="prepare_text", stored=None, current=None, reasons=["settings_unverifiable"]
        )
        assert not (graph.paths.sessions / "voice").exists()


def test_real_stored_llm_hydration_and_instance_override(owner: Any, graph: Graph) -> None:
    settings = {"translation_model": "default"}
    with graph.unmodified():
        hydrated = owner._with_database_llm_settings(settings, "translation")
        assert hydrated["translation_model"] == "openai/fixture-model"
        assert hydrated["llm_provider_configs"][0]["api_key"] == "synthetic-fixture-credential"
        assert settings == {"translation_model": "default"}
        assert owner._current_stage_fingerprint("translate", settings) == {
            **FINGERPRINT,
            "target_language": "",
        }
    with patch.object(
        owner, "_with_database_llm_settings", side_effect=ValueError("provider unavailable")
    ):
        with graph.unmodified():
            assert owner.settings_mismatches("voice") == []


def test_unknown_session_and_stage_keep_original_errors(owner: Any, graph: Graph) -> None:
    with graph.unmodified():
        with pytest.raises(ValueError, match="Unknown continuation stage: missing"):
            owner.settings_mismatches("voice", "missing")
        with pytest.raises(ValueError, match="Session not found: missing"):
            owner.settings_mismatches("missing")


@pytest.mark.parametrize("inputs", [None, [], "invalid"])
def test_legacy_non_mapping_inputs_keep_fallback(owner: Any, graph: Graph, inputs: Any) -> None:
    with graph.database.session() as session:
        outcome = session.get(OutcomePlan, "voice")
        assert outcome is not None
        outcome.value_json = {**outcome.value_json, "inputs": inputs}
    with graph.unmodified():
        assert owner.settings_mismatches("voice") == []


def test_parallel_fingerprints_keep_integer_values(owner: Any, graph: Graph) -> None:
    with graph.unmodified():
        translation = owner._current_stage_fingerprint(
            "translate", {"target_language": "pl", "llm_concurrent_calls": 2}
        )
        assert translation == {**FINGERPRINT, "llm_concurrent_calls": 2}
        correction = owner._current_stage_fingerprint("correct", {"llm_concurrent_calls": 2})
        assert correction is not None
        assert correction["llm_concurrent_calls"] == 2
        assert isinstance(correction["llm_concurrent_calls"], int)


def test_cli_queues_native_reuse_job_without_full_handlers(
    graph: Graph, capsys: pytest.CaptureFixture[str]
) -> None:
    with graph.database.session() as session:
        record = session.get(SessionRecord, "voice")
        assert record is not None
        record.target_language = "de"
    args = cli.build_parser().parse_args(
        [
            "--data-dir",
            str(graph.paths.root),
            "--json",
            "workflow",
            "run",
            "voice",
            "generate_audio",
        ]
    )
    with patch.object(
        cli, "WorkflowHandlers", side_effect=AssertionError("full execution graph constructed")
    ):
        assert args.handler(args) == 0
    output = capsys.readouterr()
    emitted = json.loads(output.out)
    with graph.database.session() as session:
        job = session.get(Job, emitted["id"])
        assert job is not None and job.status == "queued"
        assert job.payload_json["reuse_stages"] == ["translate"]
    assert "Reusing the existing translate output even though target_language changed" in output.err
