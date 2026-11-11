"""Casting retains both historical identity vocabularies without source authority."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import event, select

from pandrator.web import generation_voice_references as references
from pandrator.web import models as m
from pandrator.web.generation_audio_identity import (
    IDENTITY_KEY,
    AudioIdentityContext,
    _hash,
    plan_audio_identities,
    source_cast_reference_hashes,
    take_reuse_reason,
)
from pandrator.web.generation_cast_runtime import segment_render_parts
from pandrator.web.generation_performance_snapshot import performance_runtime_settings
from pandrator.web.jobs import Worker
from pandrator.web.speech_input_contracts import (
    JSON_LIMIT,
    PROJECT_SPEECH_BINDING_KEY,
    PROJECT_SPEECH_INPUT_KEY,
    PROJECT_SPEECH_SOURCE_KEY,
    validate_speech_source_reference,
)
from pandrator.web.voice_library import resolve_audio_cpp_voice_reference
from tests.test_cast_retention_integration import native_transport
from tests.test_evidence_access_routes import body
from tests.test_generation_audible_speech_preparation import (
    cast_setup,
    defaults,
    freeze,
    publish,
    segment_ids,
)
from tests.test_generation_run_speech_preparation import run_project as run_project  # noqa: F401
from tests.test_generation_run_speech_preparation import source_settings
from tests.test_project_tts_retention import case as case  # noqa: F401
from tests.test_project_tts_retention import design as design  # noqa: F401
from tests.test_project_tts_retention import input_paths
from tests.test_project_tts_retention import no_network as no_network  # noqa: F401
from tests.test_project_tts_retention import project as project  # noqa: F401
from tests.test_project_tts_retention import retained as retained  # noqa: F401
from tests.test_provider_workspace_access import scoped_token
from tests.test_voice_crud_workspace_access import sample as add_sample


def worker(g: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[Worker, list[Any]]:
    handlers = g.p.r.f.case.app.extensions["pandrator"]["workflow_handlers"]
    return Worker(
        g.p.r.f.services.jobs, "cast-compatibility", handlers.handlers()
    ), native_transport(monkeypatch)


def run_snapshot(g: Any, identifier: str) -> dict[str, Any]:
    with g.p.r.f.case.database.snapshot_session() as session:
        return deepcopy(session.get_one(m.GenerationRun, identifier).settings_snapshot_json)


def rendered(g: Any, snapshot: dict[str, Any], identifier: str) -> list[dict[str, Any]]:
    with g.p.r.f.case.database.snapshot_session() as session:
        segment = session.get_one(m.GenerationSegment, identifier)
        return segment_render_parts(
            performance_runtime_settings(snapshot), snapshot, identifier, segment.text
        )


def source_nodes(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if PROJECT_SPEECH_SOURCE_KEY in value:
            result.append(value[PROJECT_SPEECH_SOURCE_KEY])
        for child in value.values():
            result.extend(source_nodes(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(source_nodes(child))
    return result


def assert_job(g: Any, run_id: str) -> None:
    with g.p.r.f.case.database.snapshot_session() as session:
        run = session.get_one(m.GenerationRun, run_id)
        job = session.get_one(m.Job, run.job_id)
        assert job.status == "succeeded", job.error_message
        assert (
            plan_audio_identities(session, g.revision, run.settings_snapshot_json)
            == run.settings_snapshot_json["generation_audio_identities"]
        )


@pytest.mark.parametrize("change", ["audio", "transcript"])
def test_registered_cast_and_fallback_reuse_current_history_then_stale_source(
    run_project, monkeypatch, change
):
    g, p = run_project, run_project.p
    identifier = cast_setup(g, fallback=True, two_sources=True)
    prepared = freeze(g)
    sources = source_nodes(prepared["frozen_snapshot"])
    assert sources and all(source.get("cast_reference_hashes") for source in sources)
    result = publish(g, prepared)
    w, calls = worker(g, monkeypatch)
    assert w.run_once()
    assert_job(g, result["id"])
    assert len(calls) == 3
    current = g.service.list_segments(p.source["id"])["items"][0]
    history = g.service.list_segments(p.source["id"], generation_run_id=result["id"])["items"][0]
    assert current["has_reusable_take"] and history["has_reusable_take"], (current, history)
    with p.r.f.case.database.snapshot_session() as session:
        segment = session.get_one(m.GenerationSegment, identifier)
        frozen = run_snapshot(g, result["id"])
        snapshot, _digest = g.service.settings.resolve(p.source["id"])
        live = AudioIdentityContext(session, snapshot).for_segment(segment)
        old = frozen["generation_audio_identities"][identifier]
        assert live["voice_reference_hash"] == old["legacy_voice_reference_hash"]
        assert live["source_voice_reference_hash"] == old["source_voice_reference_hash"]
    if change == "audio":
        data = bytearray(p.r.path.read_bytes())
        data[-1] = 1
        p.r.path.write_bytes(data)
        import hashlib

        with p.r.f.case.database.immediate_session() as session:
            session.get_one(m.Artifact, p.r.aid).content_hash = hashlib.sha256(data).hexdigest()
    else:
        with p.r.f.case.database.immediate_session() as session:
            session.get_one(m.VoiceSample, p.r.sid).transcript = "A changed reviewed source."
    current = g.service.list_segments(p.source["id"])["items"][0]
    history = g.service.list_segments(p.source["id"], generation_run_id=result["id"])["items"][0]
    assert not current["has_reusable_take"] and history["has_reusable_take"]
    assert len(input_paths(p)) == 4


def test_old_four_field_v2_worker_dictionary_unchanged_and_current_bridges(
    run_project, monkeypatch
):
    g, p = run_project, run_project.p
    identifier = cast_setup(g, fallback=True, two_sources=True)
    result = publish(g, freeze(g))
    w, _calls = worker(g, monkeypatch)
    assert w.run_once()
    snapshot = run_snapshot(g, result["id"])
    expected_old = {
        sid: {key: value for key, value in identity.items() if key != "legacy_voice_reference_hash"}
        for sid, identity in snapshot["generation_audio_identities"].items()
    }
    for source in source_nodes(snapshot):
        source.pop("cast_reference_hashes", None)
    with p.r.f.case.database.snapshot_session() as session:
        identities = plan_audio_identities(session, g.revision, snapshot)
    assert identities == expected_old
    assert all("legacy_voice_reference_hash" not in identity for identity in identities.values())
    snapshot["generation_audio_identities"] = identities
    with p.r.f.case.database.immediate_session() as session:
        session.get_one(m.GenerationRun, result["id"]).settings_snapshot_json = snapshot
        take = session.scalar(
            select(m.AudioTake).where(m.AudioTake.generation_run_id == result["id"])
        )
        assert take is not None
        artifact = session.get_one(m.Artifact, take.artifact_id)
        artifact.metadata_json = {**artifact.metadata_json, IDENTITY_KEY: identities[identifier]}
    with p.r.f.case.database.snapshot_session() as session:
        segment = session.get_one(m.GenerationSegment, identifier)
        assert (
            AudioIdentityContext(session, snapshot).for_segment(segment) == identities[identifier]
        )
    assert g.service.list_segments(p.source["id"])["items"][0]["has_reusable_take"]
    assert g.service.list_segments(p.source["id"], generation_run_id=result["id"])["items"][0][
        "has_reusable_take"
    ]


def test_old_unretained_cast_take_is_preserved_by_partial_fresh_run(run_project, monkeypatch):
    g, p = run_project, run_project.p
    cast_setup(g, fallback=True, two_sources=True)
    plan = g.service.create_plan(
        p.source["id"],
        source_revision_id=None,
        settings={},
        segments=[
            {"text": "Alpha speaks. Beta replies. Narrator closes."},
            {"text": "Alpha speaks. Beta replies. Narrator closes."},
        ],
    )
    g.revision = plan["active_revision_id"]
    ids = segment_ids(g)
    with p.r.f.case.database.immediate_session() as session:
        for identifier in ids:
            session.get_one(m.GenerationSegment, identifier).speech_plan_json = {
                "speech_xml": f'<segment id="{identifier}"><dialogue><speaker ref="a">Alpha speaks.</speaker></dialogue> '
                '<dialogue><speaker ref="b">Beta replies.</speaker></dialogue> Narrator closes.</segment>'
            }
    legacy = publish(g, freeze(g, retain_speech_inputs=False))
    w, _calls = worker(g, monkeypatch)
    assert w.run_once()
    old = run_snapshot(g, legacy["id"])
    assert all(
        "source_voice_reference_hash" not in item and "legacy_voice_reference_hash" not in item
        for item in old["generation_audio_identities"].values()
    )
    with p.r.f.case.database.immediate_session() as session:
        session.get_one(m.GenerationSegment, ids[0]).status = "pending"
    prepared = freeze(g, stale_only=True)
    assert prepared["requested_segment_ids"] == [ids[0]]
    assert ids[1] in prepared["reusable_take_ids"]
    assert all(
        value.get("legacy_voice_reference_hash")
        for value in prepared["frozen_snapshot"]["generation_audio_identities"].values()
    )
    fresh = publish(g, prepared)
    assert w.run_once()
    assert_job(g, fresh["id"])
    with p.r.f.case.database.snapshot_session() as session:
        assert (
            len(
                list(
                    session.scalars(
                        select(m.AudioTake).where(m.AudioTake.generation_run_id == fresh["id"])
                    )
                )
            )
            == 2
        )
        assert plan_audio_identities(session, g.revision, old) == old["generation_audio_identities"]
        for segment in session.scalars(
            select(m.GenerationSegment).where(
                m.GenerationSegment.plan_revision_id == g.revision,
                m.GenerationSegment.removed.is_(False),
            )
        ):
            assert (
                AudioIdentityContext(session, old).for_segment(segment)
                == old["generation_audio_identities"][segment.id]
            )


@pytest.mark.parametrize(
    "mutation",
    [
        "valid",
        "empty",
        "duplicate",
        "unsorted",
        "bool",
        "malformed",
        "overflow",
        "pin",
        "binding",
        "extra",
    ],
)
def test_optional_cast_fingerprints_are_strict_and_bound(mutation):
    settings = source_settings()
    source: dict[str, Any] = {
        "version": 2,
        "pin": deepcopy(settings[PROJECT_SPEECH_INPUT_KEY]),
        "binding": deepcopy(settings[PROJECT_SPEECH_BINDING_KEY]),
        "reference_identity": {
            "sample_id": "sample",
            "artifact_id": "artifact",
            "content_hash": "a" * 64,
            "transcript_hash": "b" * 64,
        },
        "cast_reference_hashes": ["c" * 64, "d" * 64],
    }
    settings[PROJECT_SPEECH_SOURCE_KEY] = source
    if mutation == "empty":
        source["cast_reference_hashes"] = []
    elif mutation == "duplicate":
        source["cast_reference_hashes"] = ["c" * 64, "c" * 64]
    elif mutation == "unsorted":
        source["cast_reference_hashes"] = ["d" * 64, "c" * 64]
    elif mutation == "bool":
        source["cast_reference_hashes"] = [True]
    elif mutation == "malformed":
        source["cast_reference_hashes"] = ["bad"]
    elif mutation == "overflow":
        source["cast_reference_hashes"] = [f"{n:064x}" for n in range(JSON_LIMIT // 64 + 1)]
    elif mutation == "pin":
        source["pin"]["content_hash"] = "e" * 64
    elif mutation == "binding":
        source["binding"]["voice"] = "transferred"
    elif mutation == "extra":
        source["path"] = "forbidden"
    assert (source_cast_reference_hashes(settings) is not None) is (mutation == "valid")
    source.pop("cast_reference_hashes")
    if mutation not in {"pin", "binding", "extra"}:
        assert (
            validate_speech_source_reference(
                source, settings[PROJECT_SPEECH_INPUT_KEY], settings[PROJECT_SPEECH_BINDING_KEY]
            )
            is not None
        )


@pytest.mark.parametrize("mismatch_field", ["sample", "transcript"])
def test_targeted_capture_index_reads_only_actual_voice_and_checks_prepared_sample(
    run_project, monkeypatch, mismatch_field
):
    g, p = run_project, run_project.p
    cast_setup(g, fallback=True, two_sources=True)
    with p.r.f.case.database.immediate_session() as session:
        sample = session.get_one(m.VoiceSample, p.r.sid)
        sample.transcript = "  The selected original reference.  "
    from tests.test_voice_crud_workspace_access import create

    unused = body(
        create(
            p.r.f.users.alice,
            p.r.f.users.ah,
            "Unrelated compatibility voice",
            wid=p.r.f.users.workspace_id,
        ),
        201,
    )
    add_sample(p.r.f.case, unused)
    original_status = references.sample_file_status
    original_index = references.voice_reference_index
    selected = []

    def status(session, paths, sample):
        assert sample.voice_id != unused["id"]
        return original_status(session, paths, sample)

    def index(*args, **kwargs):
        selected.append(kwargs["_voice_ids"])
        return original_index(*args, **kwargs)

    monkeypatch.setattr(references, "sample_file_status", status)
    monkeypatch.setattr(references, "voice_reference_index", index)
    prepared = freeze(g)
    assert len(selected) == 2 and all(len(value) == 1 for value in selected)
    assert len(prepared["_speech_preparation"]._cast_indexes) == 2
    original_sources = source_nodes(prepared["frozen_snapshot"])
    assert all(source.get("cast_reference_hashes") for source in original_sources)
    material_hashes = {_hash(source["reference_identity"]) for source in original_sources}
    g.service.cleanup_prepared_start(prepared)

    def mismatch(*args, **kwargs):
        value = original_index(*args, **kwargs)
        for items in value.values():
            for item in items:
                for sample in item["samples"]:
                    if mismatch_field == "sample":
                        sample["id"] = "different-sample"
                    else:
                        sample["transcript_hash"] = "0" * 64
        return value

    monkeypatch.setattr(references, "voice_reference_index", mismatch)
    prepared = freeze(g)
    mismatched_sources = source_nodes(prepared["frozen_snapshot"])
    assert all("cast_reference_hashes" not in source for source in mismatched_sources)
    assert {_hash(source["reference_identity"]) for source in mismatched_sources} == material_hashes
    assert all(
        value.get("source_voice_reference_hash") and "legacy_voice_reference_hash" not in value
        for value in prepared["frozen_snapshot"]["generation_audio_identities"].values()
    )
    g.service.cleanup_prepared_start(prepared)


def test_live_metadata_resolver_is_cached_selection_guarded_and_never_hashes_missing_waveform(
    run_project, monkeypatch
):
    g, p = run_project, run_project.p
    identifier = cast_setup(g, fallback=True)
    snapshot, _digest = g.service.settings.resolve(p.source["id"])
    original = resolve_audio_cpp_voice_reference
    calls = []

    def resolve(*args, **kwargs):
        calls.append(kwargs)
        assert kwargs["strict_selection"] is True and kwargs["_metadata_only"] is True
        return original(*args, **kwargs)

    monkeypatch.setattr("pandrator.web.voice_library.resolve_audio_cpp_voice_reference", resolve)
    with p.r.f.case.database.snapshot_session() as session:
        context = AudioIdentityContext(session, snapshot)
        segment = session.get_one(m.GenerationSegment, identifier)
        first = context.for_segment(segment)
        assert first.get("source_voice_reference_hash")
        assert context.for_segment(segment) == first
        assert len(calls) == 2
        assert any(item["voice_id"] == p.r.voice["id"] for item in calls)
    with p.r.f.case.database.immediate_session() as session:
        session.get_one(m.Artifact, p.r.aid).content_hash = None
    with (
        p.r.f.case.database.snapshot_session() as session,
        patch.object(Path, "open", side_effect=AssertionError("waveform opened")),
    ):
        with pytest.raises(ValueError, match="published content hash"):
            original(
                session,
                p.r.f.services.paths,
                {**snapshot["tts"], "voice": "workshop-reference"},
                workspace_id=p.r.f.users.workspace_id,
                voice_id=p.r.voice["id"],
                strict_selection=True,
                _metadata_only=True,
            )
        unknown = AudioIdentityContext(session, snapshot).for_segment(
            session.get_one(m.GenerationSegment, identifier)
        )
        assert "source_voice_reference_hash" not in unknown


def test_sample_timestamp_tie_uses_descending_id_in_capture_index_and_resolver(run_project):
    g, p = run_project, run_project.p
    defaults(p, casting_enabled=True)
    sid, _aid, _path = add_sample(p.r.f.case, p.r.voice)
    with p.r.f.case.database.immediate_session() as session:
        original = session.get_one(m.VoiceSample, p.r.sid)
        newer = session.get_one(m.VoiceSample, sid)
        newer.created_at = original.created_at
        newer.transcript, newer.transcript_reviewed = "Tied second transcript.", True
    selected_id = max(sid, p.r.sid)
    prepared = freeze(g)
    source = source_nodes(prepared["frozen_snapshot"])[0]
    assert source["reference_identity"]["sample_id"] == selected_id
    with p.r.f.case.database.snapshot_session() as session:
        settings, _digest = g.service.settings.resolve(p.source["id"])
        selected = resolve_audio_cpp_voice_reference(
            session,
            p.r.f.services.paths,
            settings["tts"],
            workspace_id=p.r.f.users.workspace_id,
            voice_id=p.r.voice["id"],
            strict_selection=True,
            _metadata_only=True,
        )
        assert selected is not None
        assert selected[3].id == selected_id
        index = references.voice_reference_index(
            session,
            p.r.f.services.paths,
            workspace_id=p.r.f.users.workspace_id,
            _voice_ids={p.r.voice["id"]},
        )
        assert index[p.r.voice["id"]][0]["samples"][0]["id"] == selected_id
    g.service.cleanup_prepared_start(prepared)


def test_project_only_current_metadata_has_no_trusted_material_alternate(run_project, monkeypatch):
    from pandrator.web.accounts.execution_actor import actor_from_principal, execution_actor_scope

    g, p = run_project, run_project.p
    identifier = cast_setup(g)
    legacy = freeze(g, retain_speech_inputs=False)["frozen_snapshot"]
    legacy.pop("generation_audio_identities")
    _token, bearer = scoped_token(
        p.r.f.case, p.r.f.users.bob_id, {"app.read"}, project=p.source["project_id"]
    )
    principal = p.r.f.case.auth.resolve_api_token(
        bearer["Authorization"].removeprefix("Bearer "),
        network_zone="loopback",
        target_instance_id=p.r.f.services.identity.snapshot(
            observed_origin="http://localhost"
        ).instance_id,
    )
    assert principal is not None
    monkeypatch.setattr(
        "pandrator.web.voice_library.resolve_audio_cpp_voice_reference",
        lambda *_a, **_k: pytest.fail("project-only metadata reached protected resolver"),
    )
    statements = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    with (
        execution_actor_scope(actor_from_principal(principal)),
        p.r.f.case.database.snapshot_session() as session,
    ):
        segment = session.get_one(m.GenerationSegment, identifier)
        event.listen(p.r.f.case.database.engine, "before_cursor_execute", observe)
        try:
            identity = AudioIdentityContext(session, legacy).for_segment(segment)
        finally:
            event.remove(p.r.f.case.database.engine, "before_cursor_execute", observe)
    assert "source_voice_reference_hash" not in identity
    assert "legacy_voice_reference_hash" not in identity
    assert not any(
        "FROM voice_samples" in statement or "FROM voices" in statement for statement in statements
    )


def test_optional_hash_candidates_keep_settings_and_performance_guards():
    segment = m.GenerationSegment(status="completed")
    take = m.AudioTake(status="completed")
    expected = {
        "schema_version": 1,
        "settings_hash": "settings",
        "voice_reference_hash": "a" * 64,
        "performance_request_hash": "same",
    }
    artifact = m.Artifact(
        state="current",
        metadata_json={
            IDENTITY_KEY: {
                **expected,
                "voice_reference_hash": "b" * 64,
                "legacy_voice_reference_hash": "a" * 64,
            }
        },
    )
    assert take_reuse_reason(segment, take, artifact, expected) == "reusable"
    assert (
        take_reuse_reason(segment, take, artifact, {**expected, "settings_hash": "changed"})
        == "generation_settings_changed"
    )
    assert (
        take_reuse_reason(
            segment, take, artifact, {**expected, "performance_request_hash": "changed"}
        )
        == "performance_changed"
    )
    artifact.metadata_json[IDENTITY_KEY]["legacy_voice_reference_hash"] = True
    assert take_reuse_reason(segment, take, artifact, expected) == "voice_reference_changed"


def test_mixed_old_v2_new_and_saved_parts_omit_incomplete_legacy_alternate(run_project):
    g = run_project
    identifier = cast_setup(g, fallback=True, two_sources=True)
    snapshot = freeze(g)
    publish(g, snapshot)
    mixed = deepcopy(snapshot["frozen_snapshot"])
    cast = mixed["generation_control_snapshot"]
    first = next(iter(cast["resolved_bindings"].values()))
    original_hashes = first[PROJECT_SPEECH_SOURCE_KEY].pop("cast_reference_hashes")
    base = cast["segments"][identifier]["controls"]["_base_voice_settings"]
    base.pop(PROJECT_SPEECH_SOURCE_KEY)
    with g.p.r.f.case.database.snapshot_session() as session:
        identity = plan_audio_identities(session, g.revision, mixed)[identifier]
    assert "legacy_voice_reference_hash" not in identity
    assert (
        identity["voice_reference_hash"]
        == mixed["generation_audio_identities"][identifier]["voice_reference_hash"]
    )
    first[PROJECT_SPEECH_SOURCE_KEY]["cast_reference_hashes"] = original_hashes
    with g.p.r.f.case.database.snapshot_session() as session:
        identity = plan_audio_identities(session, g.revision, mixed)[identifier]
    parts = rendered(g, mixed, identifier)
    legacy_parts = []
    for part in parts:
        hashes = source_cast_reference_hashes(part["settings"])
        if hashes is None:
            hashes = [
                _hash(
                    {
                        "retained_content_hash": part["settings"][PROJECT_SPEECH_INPUT_KEY][
                            "content_hash"
                        ]
                    }
                )
            ]
        legacy_parts.append(hashes)
    assert identity["legacy_voice_reference_hash"] == _hash(legacy_parts)


def test_explicit_managed_material_does_not_follow_ambiguous_alias(run_project, monkeypatch):
    g, p = run_project, run_project.p
    identifier = cast_setup(g, two_sources=True)
    with p.r.f.case.database.immediate_session() as session:
        second = session.scalar(
            select(m.Voice).where(
                m.Voice.workspace_id == p.r.f.users.workspace_id,
                m.Voice.name == "Second audible voice",
            )
        )
        assert second is not None
        metadata = deepcopy(second.metadata_json)
        metadata["providers"]["audio_cpp"]["voice_id"] = "workshop-reference"
        second.metadata_json = metadata
    result = publish(g, freeze(g))
    w, _calls = worker(g, monkeypatch)
    assert w.run_once()
    snapshot, _digest = g.service.settings.resolve(p.source["id"])
    with p.r.f.case.database.snapshot_session() as session:
        segment = session.get_one(m.GenerationSegment, identifier)
        current = AudioIdentityContext(session, snapshot).for_segment(segment)
        recorded = run_snapshot(g, result["id"])["generation_audio_identities"][identifier]
    assert current["voice_reference_hash"] != recorded["legacy_voice_reference_hash"]
    assert current["source_voice_reference_hash"] == recorded["source_voice_reference_hash"]
    assert g.service.list_segments(p.source["id"])["items"][0]["has_reusable_take"]


def test_missing_live_material_suppresses_mixed_alternate_without_database(monkeypatch):
    from typing import cast
    from unittest.mock import MagicMock

    from sqlalchemy.orm import Session

    from pandrator.runtime import DataPaths
    from tests.test_retained_preview_workspace import MODEL

    settings = {
        "service": "audio_cpp",
        "model": MODEL,
        "casting_enabled": True,
        "performance_enabled": False,
    }
    available = {**settings, "voice": "available"}
    missing = {**settings, "voice": "missing"}
    snapshot = {"tts": settings, "generation_control_snapshot": {"plan_revision_id": "revision"}}
    context = object.__new__(AudioIdentityContext)
    fake_session = MagicMock(spec=Session)
    context.session = cast(Session, fake_session)
    context.paths = cast(DataPaths, MagicMock(spec=DataPaths))
    context.snapshot = snapshot
    context.workspace_id = "workspace"
    context._live_cast_material = True
    context._live_cast_references = {}
    context._service_config_cache = {}
    context._validated_controls = {}
    context._session_ids_by_revision = {"revision": "session"}
    context._pending_proof = None
    context.selected_rvc = {}
    context.performance_states = {"revision": (snapshot, {})}
    monkeypatch.setattr(
        context, "_references_for_settings", lambda values: [{"legacy": values["voice"]}]
    )
    monkeypatch.setattr(
        context, "_memoized_material_settings", lambda values: {"voice": values["voice"]}
    )
    monkeypatch.setattr(
        "pandrator.web.speech_inputs.speech_input_adapter", lambda _values: ("audio_cpp", set())
    )
    read_guard = MagicMock()
    monkeypatch.setattr(
        "pandrator.web.generation_audio_identity.require_current_voice_library_read", read_guard
    )
    sample = m.VoiceSample(id="sample", transcript="Reviewed material.", transcript_reviewed=True)
    artifact = m.Artifact(id="artifact")
    resolved = ("a" * 64, Path("unused.wav"), artifact, sample)
    resolver = MagicMock(
        side_effect=lambda _s, _p, values, *_a, **_k: (
            resolved if values["voice"] == "available" else None
        )
    )
    monkeypatch.setattr("pandrator.web.voice_library.resolve_audio_cpp_voice_reference", resolver)
    monkeypatch.setattr(
        "pandrator.web.generation_performance_snapshot.segment_performance_settings",
        lambda values, *_a, **_k: values,
    )
    monkeypatch.setattr(
        "pandrator.web.generation_cast_runtime.segment_render_parts",
        lambda *_a, **_k: [
            {"text": "First.", "start": 0, "end": 6, "settings": available},
            {"text": "Second.", "start": 6, "end": 13, "settings": missing},
        ],
    )
    monkeypatch.setattr(
        "pandrator.logic.speech_performance.compile_performance",
        lambda *_a, **_k: MagicMock(fingerprint="request"),
    )
    segment = m.GenerationSegment(id="segment", plan_revision_id="revision", text="First.Second.")
    identity = context._with_performance(segment, {"voice_reference_hash": "baseline"}, settings)
    assert identity["voice_reference_hash"] == _hash(
        [
            [_hash({"legacy": "available"})],
            [_hash({"legacy": "missing"})],
        ]
    )
    assert "source_voice_reference_hash" not in identity
    assert context._live_cast_material_references(missing) is None
    assert context._live_cast_references[("audio_cpp", MODEL, "missing", "")] is None
    assert resolver.call_count == 2 and read_guard.call_count == 3
    fake_session.execute.assert_not_called()
    fake_session.scalar.assert_not_called()
    fake_session.get.assert_not_called()
