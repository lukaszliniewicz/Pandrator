"""Saved speech files copy independently of assembly execution and old inputs."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from uuid import UUID

import pytest
from sqlalchemy import event, func, select

from pandrator.web import models as m
from pandrator.web.access.contracts import AccessDenied
from pandrator.web.accounts.execution_actor import actor_from_principal, execution_actor_scope
from pandrator.web.export_inputs import resolve_export_inputs, select_generated_audio
from pandrator.web.model_base import new_id
from pandrator.web.projects import copy_publication, copy_snapshot
from pandrator.web.projects.copy_contracts import ProjectCopyError, ProjectCopySnapshot
from pandrator.web.projects.copy_outputs import (
    COPIED_OUTPUT_KEY,
    SAVED_SPEECH_ROLE,
    freeze_saved_speech_output,
    validate_output_copy_graph,
)
from pandrator.web.projects.copy_sound import SOUND_FAMILIES
from pandrator.web.projects.copy_speech_plan import SPEECH_FAMILIES
from pandrator.web.sound_project_models import ProjectSoundPlan
from tests.test_account_authentication_integration import current_browser_principal
from tests.test_cast_retention_integration import native_transport
from tests.test_evidence_access_routes import body, headers
from tests.test_google_design_workspace_http import case as case  # noqa: F401
from tests.test_google_design_workspace_http import design as design  # noqa: F401
from tests.test_google_design_workspace_http import no_network as no_network  # noqa: F401
from tests.test_project_audio_copy import active_rows, generate, route_config, worker_for
from tests.test_project_cast_copy import _context, canonical
from tests.test_project_speech_plan_copy import editorial as editorial  # noqa: F401
from tests.test_project_speech_plan_copy import (
    retained_editorial as retained_editorial,  # noqa: F401
)
from tests.test_voice_lifecycle_idempotency import silent_wav


def uid(number):
    return str(UUID(int=number))


def artifact(**changes):
    return {
        "id": uid(1),
        "workspace_id": uid(2),
        "project_id": uid(3),
        "session_id": uid(4),
        "kind": "audio",
        "role": "assembled_audio",
        "relative_path": "artifacts/speech.wav",
        "mime_type": "audio/wav",
        "size_bytes": 123,
        "content_hash": "a" * 64,
        "state": "current",
        "settings_hash": "opaque history",
        "metadata_json": {
            "output_assembly_id": uid(8),
            "duration_ms": 100,
            "segment_count": 2,
            "format": "wav",
            "bitrate": None,
            "metadata": {
                "title": uid(3),
                "artist": "Literal artist",
                "album": "",
                "genre": "Speech",
                "language": "en",
            },
            "chapters": [{"start_ms": 0, "title": uid(4)}, {"start_ms": 100, "title": "End"}],
            "takes": [{"generation_run_id": uid(9)}],
            "output_settings": {"api_key": "not transferred"},
        },
        **changes,
    }


def saved():
    return freeze_saved_speech_output(artifact())


class DiscardedManifest:
    def __deepcopy__(self, _memo):
        pytest.fail("Discarded assembly manifest was deep-copied")

    def __iter__(self):
        pytest.fail("Discarded assembly manifest was traversed")


def test_projection_is_detached_finite_and_never_walks_discarded_manifests():
    source = artifact()
    omitted = DiscardedManifest()
    source["metadata_json"]["takes"] = omitted
    source["metadata_json"]["output_settings"] = omitted
    source["metadata_json"]["metadata"]["private_tag"] = omitted
    result = freeze_saved_speech_output(source)
    assert source["role"] == "assembled_audio" and source["metadata_json"]["takes"] is omitted
    assert result["role"] == SAVED_SPEECH_ROLE
    assert result["metadata_json"] == {
        COPIED_OUTPUT_KEY: {"version": 1, "kind": "speech_assembly"},
        "duration_ms": 100,
        "segment_count": 2,
        "format": "wav",
        "bitrate": None,
        "metadata": {
            "title": uid(3),
            "artist": "Literal artist",
            "album": "",
            "genre": "Speech",
            "language": "en",
        },
        "chapters": [{"start_ms": 0, "title": uid(4)}, {"start_ms": 100, "title": "End"}],
    }
    for key in set(source) - {"role", "metadata_json"}:
        assert result[key] == source[key]
    result["metadata_json"]["chapters"][0]["title"] = "changed"
    result["metadata_json"]["metadata"]["title"] = "changed"
    assert source["metadata_json"]["chapters"][0]["title"] == uid(4)
    assert source["metadata_json"]["metadata"]["title"] == uid(3)


@pytest.mark.parametrize("format", ["wav", "mp3", "flac", "opus"])
def test_recopy_keeps_canonical_display_and_opaque_file_facts(format):
    source = artifact(state="stale")
    source["metadata_json"]["format"] = format
    first = freeze_saved_speech_output(source)
    second = freeze_saved_speech_output(first)
    assert (
        second == first
        and second is not first
        and second["metadata_json"] is not first["metadata_json"]
    )
    assert second["state"] == "stale" and second["settings_hash"] == "opaque history"
    validate_output_copy_graph({"artifacts": [second], "artifact_edges": []})


def test_projection_does_not_invent_optional_values_and_accepts_exact_text_bounds():
    source = artifact(metadata_json={"takes": DiscardedManifest()})
    assert freeze_saved_speech_output(source)["metadata_json"] == {
        COPIED_OUTPUT_KEY: {"version": 1, "kind": "speech_assembly"}
    }
    source["metadata_json"] = {
        "duration_ms": 2**53 - 1,
        "segment_count": 2**53 - 1,
        "metadata": {"title": "ż" * 8192},
    }
    assert freeze_saved_speech_output(source)["metadata_json"]["duration_ms"] == 2**53 - 1


@pytest.mark.parametrize(
    "defect",
    [
        "version_bool",
        "version",
        "capsule_extra",
        "capsule_kind",
        "root_extra",
        "missing_marker",
        "wrong_role",
        "raw_marker",
        "kind",
        "mime",
        "state",
        "duration_bool",
        "duration_zero",
        "duration_float",
        "duration_huge",
        "count_negative",
        "format",
        "bitrate",
        "tags_type",
        "tag_type",
        "tag_extra",
        "tag_size",
        "tag_unicode",
        "chapters_type",
        "chapter_extra",
        "chapter_bool",
        "chapter_order",
        "chapter_duration",
        "chapter_count",
        "json_size",
    ],
)
def test_closed_saved_output_rejects_invalid_display_and_file_headers(defect):
    value = saved()
    metadata = value["metadata_json"]
    if defect == "version_bool":
        metadata[COPIED_OUTPUT_KEY]["version"] = True
    elif defect == "version":
        metadata[COPIED_OUTPUT_KEY]["version"] = 2
    elif defect == "capsule_extra":
        metadata[COPIED_OUTPUT_KEY]["hidden"] = {}
    elif defect == "capsule_kind":
        metadata[COPIED_OUTPUT_KEY]["kind"] = {}
    elif defect == "root_extra":
        metadata["api_base"] = ""
    elif defect == "missing_marker":
        metadata.pop(COPIED_OUTPUT_KEY)
    elif defect == "wrong_role":
        value["role"] = "export"
    elif defect == "raw_marker":
        value["role"] = "assembled_audio"
    elif defect == "kind":
        value["kind"] = "video"
    elif defect == "mime":
        value["mime_type"] = "application/octet-stream"
    elif defect == "state":
        value["state"] = "superseded"
    elif defect == "duration_bool":
        metadata["duration_ms"] = True
    elif defect == "duration_zero":
        metadata["duration_ms"] = 0
    elif defect == "duration_float":
        metadata["duration_ms"] = float("inf")
    elif defect == "duration_huge":
        metadata["duration_ms"] = 2**53
    elif defect == "count_negative":
        metadata["segment_count"] = -1
    elif defect == "format":
        metadata["format"] = "m4b"
    elif defect == "bitrate":
        metadata["bitrate"] = "x" * 65
    elif defect == "tags_type":
        metadata["metadata"] = []
    elif defect == "tag_type":
        metadata["metadata"]["title"] = None
    elif defect == "tag_extra":
        metadata["metadata"]["path"] = ""
    elif defect == "tag_size":
        metadata["metadata"]["title"] = "ż" * 8193
    elif defect == "tag_unicode":
        metadata["metadata"]["title"] = "\ud800"
    elif defect == "chapters_type":
        metadata["chapters"] = {}
    elif defect == "chapter_extra":
        metadata["chapters"][0]["artifact_id"] = uid(3)
    elif defect == "chapter_bool":
        metadata["chapters"][0]["start_ms"] = False
    elif defect == "chapter_order":
        metadata["chapters"] = [{"start_ms": 50, "title": "A"}, {"start_ms": 1, "title": "B"}]
    elif defect == "chapter_duration":
        metadata["chapters"][1]["start_ms"] = 101
    elif defect == "chapter_count":
        metadata["chapters"] = [{"start_ms": 0, "title": ""}] * 10001
    else:
        metadata["chapters"] = [{"start_ms": 0, "title": "x" * 16000}] * 70
    with pytest.raises(ValueError):
        freeze_saved_speech_output(value)


def test_graph_cuts_only_incoming_saved_history_and_rejects_role_marker_mismatch():
    output = saved()
    ordinary = artifact(id=uid(5), role="source", metadata_json={"untouched": DiscardedManifest()})
    outgoing = {
        "parent_artifact_id": output["id"],
        "child_artifact_id": ordinary["id"],
        "relation": "derived_from",
    }
    rows = {"artifacts": [output, ordinary], "artifact_edges": [outgoing]}
    validate_output_copy_graph(rows)
    assert rows["artifact_edges"] == [outgoing]
    with pytest.raises(ValueError):
        validate_output_copy_graph(
            {
                **rows,
                "artifact_edges": [
                    {"parent_artifact_id": ordinary["id"], "child_artifact_id": output["id"]}
                ],
            }
        )
    with pytest.raises(ValueError):
        validate_output_copy_graph({"artifacts": [artifact()], "artifact_edges": []})
    with pytest.raises(ValueError):
        validate_output_copy_graph(
            {"artifacts": [{**output, "role": "export"}], "artifact_edges": []}
        )


def snapshot_for(state):
    text = canonical(state)
    return ProjectCopySnapshot(
        uid(90), uid(91), "Saved output", text, hashlib.sha256(text.encode()).hexdigest(), (), (), 0
    )


@pytest.mark.parametrize("version", range(1, 10))
def test_old_formats_keep_frozen_bytes_and_reject_reserved_output_role_or_marker(version):
    families = set(copy_publication._FIELDS) - SOUND_FAMILIES - {"performance_plans"}
    if version < 9:
        families.remove("audio_takes")
    if version < 8:
        families -= SPEECH_FAMILIES
    if version < 5:
        families.remove("project_speech_references")
    if version == 1:
        families -= {
            "knowledge_ledgers",
            "subtitle_evidence",
            "media_edit_plans",
            "media_edit_plan_revisions",
        }
    elif version == 2:
        families -= {"subtitle_evidence", "media_edit_plans", "media_edit_plan_revisions"}
    elif version == 3:
        families -= {"media_edit_plans", "media_edit_plan_revisions"}
    state = {
        "schema_version": version,
        "rows": {key: [] for key in families},
        "binding_fields": {},
        "unsupported_counts": {},
    }
    if version >= 6:
        state["destination_connections"] = []
    original = deepcopy(state)
    snapshot = snapshot_for(state)
    rows, _ids, _all = copy_publication._decode(snapshot)
    assert state == original and snapshot.state_json == canonical(original)
    assert snapshot.fingerprint == hashlib.sha256(snapshot.state_json.encode()).hexdigest()
    assert set(json.loads(snapshot.state_json)["rows"]) == families
    assert rows["audio_takes"] == []
    for mutation in ("role", "marker"):
        forged = deepcopy(original)
        output = saved()
        if mutation == "role":
            output["metadata_json"] = {}
        else:
            output["role"] = "source"
        forged["rows"]["artifacts"] = [output]
        with pytest.raises(ProjectCopyError):
            copy_publication._decode(snapshot_for(forged))


def test_v10_decode_and_generic_remap_reject_live_history_and_preserve_literal_titles():
    state = {
        "schema_version": 10,
        "rows": {key: [] for key in copy_publication._FIELDS if key not in SOUND_FAMILIES and key != "performance_plans"},
        "binding_fields": {},
        "unsupported_counts": {},
        "destination_connections": [],
    }
    state["rows"]["artifacts"] = [saved()]
    snapshot = snapshot_for(state)
    rows, _ids, _all = copy_publication._decode(snapshot)
    mapped = copy_publication._remap_json(
        rows["artifacts"][0]["metadata_json"], {uid(3): uid(103), uid(4): uid(104)}, {}
    )
    assert mapped == rows["artifacts"][0]["metadata_json"]
    assert snapshot.manifest()["counts"]["saved_speech_outputs"] == 1
    for version in (9, 10):
        empty = deepcopy(state)
        empty["schema_version"] = version
        empty["rows"]["artifacts"] = []
        assert "saved_speech_outputs" not in snapshot_for(empty).manifest()["counts"]
    legacy = deepcopy(state)
    legacy["schema_version"] = 9
    assert "saved_speech_outputs" not in snapshot_for(legacy).manifest()["counts"]
    state["rows"]["artifacts"] = [artifact()]
    with pytest.raises(ProjectCopyError):
        copy_publication._decode(snapshot_for(state))
    state["rows"]["artifacts"] = [saved()]
    state["rows"]["artifact_edges"] = [
        {"parent_artifact_id": uid(8), "child_artifact_id": uid(1), "relation": "derived_from"}
    ]
    with pytest.raises(ProjectCopyError):
        copy_publication._decode(snapshot_for(state))


def observe(database, operation):
    statements = []

    def callback(_c, _cu, sql, params, _co, _e):
        statements.append((sql, params))

    event.listen(database.engine, "before_cursor_execute", callback)
    try:
        result = operation()
    finally:
        event.remove(database.engine, "before_cursor_execute", callback)
    return result, statements


def new_source(f, client, auth, key):
    return body(
        client.post(
            "/api/v1/sessions",
            json={"name": key, "workflow_kind": "voiceover"},
            headers=headers(auth, key),
        ),
        201,
    )


def register_audio(f, client, source, role, metadata):
    actor = actor_from_principal(current_browser_principal(replace(f.case, client=client)))
    assert actor is not None
    path = f.services.paths.uploads / (new_id() + ".wav")
    path.write_bytes(silent_wav())
    with execution_actor_scope(actor):
        return f.services.artifacts.register(
            path, kind="audio", role=role, session_id=source["id"], metadata=metadata
        )


def test_no_output_capture_adds_no_archive_body_io_writes_or_assets(design, monkeypatch, tmp_path):
    f, users = design, design.users
    source = new_source(f, users.alice, users.ah, "no-saved-output")

    def forbidden(*_a, **_k):
        pytest.fail("Ordinary capture crossed saved-output projection")

    monkeypatch.setattr(copy_snapshot, "freeze_saved_speech_output", forbidden)
    before = set((f.services.paths.root / "artifacts").rglob("*"))
    context = _context(f, users.alice)

    def capture():
        with f.case.database.snapshot_session() as session:
            return copy_snapshot.capture_current_project(
                session,
                context,
                source["project_id"],
                users.workspace_id,
                settings_service=f.services.workspace_settings,
            )

    snapshot, statements = observe(f.case.database, capture)
    assert snapshot.ready and not snapshot.files
    assert "saved_speech_outputs" not in snapshot.manifest()["counts"]
    assert "output_assemblies" not in json.loads(snapshot.state_json)["rows"]
    assert not any("output_assemblies.settings_json" in sql for sql, _p in statements)
    assert not any(
        sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql, _p in statements
    )
    assert set((f.services.paths.root / "artifacts").rglob("*")) == before
    (tmp_path / "no-output-query-receipt.json").write_text(
        json.dumps(
            {"sql": len(statements), "writes": 0, "assets_added": 0, "archive_body_calls": 0},
            indent=2,
        )
    )


def test_opaque_closure_bulk_authorizes_retained_files_only_and_keeps_other_edges(design, tmp_path):
    f, users = design, design.users
    source = new_source(f, users.alice, users.ah, "opaque-output-source")
    other = new_source(f, users.owner, users.oh, "protected-old-output-input")
    old = register_audio(
        f, users.owner, other, "generation_take", {"private": "old protected body"}
    )
    outputs = [
        register_audio(
            f,
            users.alice,
            source,
            "assembled_audio",
            {"duration_ms": 20, "format": "wav", "takes": [old.id]},
        )
        for _ in range(3)
    ]
    ordinary = register_audio(f, users.alice, source, "ordinary_audio", {})
    with f.case.database.immediate_session() as session:
        for output in outputs:
            session.add(
                m.ArtifactEdge(
                    parent_artifact_id=old.id, child_artifact_id=output.id, relation="derived_from"
                )
            )
        session.add(
            m.ArtifactEdge(
                parent_artifact_id=outputs[0].id,
                child_artifact_id=ordinary.id,
                relation="derived_from",
            )
        )
    contexts = {"alice": _context(f, users.alice), "bob": _context(f, users.bob)}

    def closure(client, ids):
        with f.case.database.snapshot_session() as session:
            return copy_snapshot._artifact_closure(
                session, contexts["alice" if client is users.alice else "bob"], ids
            )

    single, one_sql = observe(f.case.database, lambda: closure(users.alice, {outputs[0].id}))
    many, many_sql = observe(
        f.case.database, lambda: closure(users.alice, {row.id for row in outputs})
    )
    assert set(single[0]) == {outputs[0].id} and single[1] == []
    assert set(many[0]) == {row.id for row in outputs} and many[1] == []
    assert all(row["role"] == SAVED_SPEECH_ROLE for row in many[0].values())
    assert not any(old.id in repr(params) for _sql, params in many_sql)
    one_reads = [sql for sql, _p in one_sql if sql.lstrip().upper().startswith("SELECT")]
    many_reads = [sql for sql, _p in many_sql if sql.lstrip().upper().startswith("SELECT")]
    assert len(one_reads) == len(many_reads)
    assert not any("FROM artifact_edges" in sql for sql in many_reads)
    with pytest.raises(AccessDenied):
        closure(users.bob, {outputs[0].id})
    retained, edges = closure(users.alice, {ordinary.id})
    assert set(retained) == {ordinary.id, outputs[0].id} and len(edges) == 1
    assert (
        edges[0]["parent_artifact_id"] == outputs[0].id
        and edges[0]["child_artifact_id"] == ordinary.id
    )
    validate_output_copy_graph({"artifacts": list(retained.values()), "artifact_edges": edges})
    with f.case.database.immediate_session() as session:
        session.add(
            m.ArtifactEdge(
                parent_artifact_id=old.id, child_artifact_id=ordinary.id, relation="derived_from"
            )
        )
    with pytest.raises(AccessDenied):
        closure(users.alice, {ordinary.id})
    (tmp_path / "opaque-closure-receipt.json").write_text(
        json.dumps(
            {
                "one_output_selects": len(one_reads),
                "three_output_selects": len(many_reads),
                "old_ancestor_reads": 0,
                "incoming_edges": 0,
                "outgoing_retained": 1,
            },
            indent=2,
        )
    )


def source_model_rows(session):
    return {
        model: [dict(row) for row in session.execute(select(model.__table__)).mappings()]
        for model in (
            m.Project,
            m.SessionRecord,
            m.GenerationPlan,
            m.GenerationPlanRevision,
            m.GenerationSegment,
            m.GenerationRun,
            m.AudioTake,
            m.OutputAssembly,
            m.Artifact,
            m.ArtifactEdge,
            m.ProjectSpeechReference,
            m.KnowledgeLedger,
            m.SessionSetting,
        )
    }


def assert_original_rows(session, expected):
    for model, rows in expected.items():
        columns = [column.name for column in model.__table__.primary_key]
        identities = {tuple(row[column] for column in columns) for row in rows}
        actual = [
            dict(row)
            for row in session.execute(select(model.__table__)).mappings()
            if tuple(row[column] for column in columns) in identities
        ]
        assert sorted(actual, key=lambda row: tuple(row[column] for column in columns)) == sorted(
            rows, key=lambda row: tuple(row[column] for column in columns)
        )


def public_copy(f, client, auth, source, worker, key):
    from tests.project_copy_test_support import snapshot_destination_connections

    preview = body(
        client.get(
            f"/api/v1/projects/{source['project_id']}/copy-preview",
            query_string={"workspace_id": f.users.workspace_id},
        )
    )
    assert preview["ready"], preview["blockers"]
    with f.case.database.snapshot_session() as session:
        snapshot = copy_snapshot.capture_current_project(
            session,
            _context(f, client),
            source["project_id"],
            f.users.workspace_id,
            settings_service=f.services.workspace_settings,
        )
    assert snapshot.ready and preview["fingerprint"] == snapshot.fingerprint
    queued = body(
        client.post(
            f"/api/v1/projects/{source['project_id']}/copy",
            json={
                "destination_workspace_id": f.users.workspace_id,
                "expected_fingerprint": preview["fingerprint"],
                "destination_connections": snapshot_destination_connections(snapshot),
            },
            headers=headers(auth, key),
        ),
        202,
    )
    assert worker.run_once()
    completed = body(client.get(f"/api/v1/project-copies/{queued['id']}"))
    assert completed["state"] == "succeeded", completed
    return snapshot, preview, completed["result"]


def test_registered_saved_output_lifecycle_recopy_and_current_assembly_selection(
    retained_editorial, monkeypatch, tmp_path
):
    p, f, users = retained_editorial, retained_editorial.f, retained_editorial.users
    route_config(users.alice, users.ah)
    calls = native_transport(monkeypatch)
    worker = worker_for(f)
    run_id = generate(
        f, users.alice, users.ah, p.source["id"], worker, "saved-output-source-generation"
    )
    assert len(calls) == 2
    assembled = body(
        users.alice.post(
            f"/api/v1/sessions/{p.source['id']}/output-assemblies",
            json={"generation_run_id": run_id},
            headers=headers(users.ah, "saved-output-source-assembly"),
        ),
        202,
    )
    assert worker.run_once()
    assembled_job = f.services.jobs.get(assembled["job_id"])
    assert assembled_job.status == "succeeded", assembled_job.error_message
    # Exercise the normal export descendant shape, with no project sound plan.
    exported = body(
        users.alice.post(
            f"/api/v1/sessions/{p.source['id']}/stages/export/run",
            json={
                "format": "mp3",
                "export_mode": "audio",
                "audio_mode": "dubbed",
                "include_sounds": False,
                "generation_run_id": run_id,
            },
            headers=headers(users.ah, "saved-output-source-export"),
        ),
        202,
    )
    assert worker.run_once()
    export_job = f.services.jobs.get(exported["id"])
    assert export_job.status == "succeeded", export_job.error_message
    with f.case.database.snapshot_session() as session:
        original_assembly = session.get_one(m.OutputAssembly, assembled["id"])
        output = session.get_one(m.Artifact, original_assembly.artifact_id)
        assert output.role == "assembled_audio"
        output_bytes = (f.services.paths.root / output.relative_path).read_bytes()
        output_metadata = freeze_saved_speech_output(
            {column.name: getattr(output, column.name) for column in m.Artifact.__table__.columns}
        )["metadata_json"]
        original_rows = source_model_rows(session)
        original_files = {
            row.relative_path: hashlib.sha256(
                (f.services.paths.root / row.relative_path).read_bytes()
            ).hexdigest()
            for row in session.scalars(select(m.Artifact))
            if (f.services.paths.root / row.relative_path).is_file()
        }
        counts = {
            model: session.scalar(select(func.count()).select_from(model))
            for model in (m.Voice, m.VoiceSample, m.StoredCredential, m.ProviderModel)
        }
        assert session.scalar(select(func.count()).select_from(ProjectSoundPlan)) == 0
        masters = list(
            session.scalars(
                select(m.Artifact).where(
                    m.Artifact.session_id == p.source["id"], m.Artifact.role == "soundtrack_master"
                )
            )
        )
        exports = list(
            session.scalars(
                select(m.Artifact).where(
                    m.Artifact.session_id == p.source["id"],
                    m.Artifact.role == "export_dubbed_audio",
                )
            )
        )
        assert len(masters) == 1 and len(exports) == 1

    def no_library(*_a, **_k):
        pytest.fail("Saved output copy consulted the source voice library")

    for path in (
        "pandrator.web.project_speech_reference_capture.capture_speech_input",
        "pandrator.web.speech_inputs.capture_speech_input",
        "pandrator.web.generation_audio_identity.voice_reference_index",
    ):
        monkeypatch.setattr(path, no_library)
    snapshot, preview, result = public_copy(
        f, users.alice, users.ah, p.source, worker, "saved-output-first-copy"
    )
    assert preview["counts"]["saved_speech_outputs"] == 1
    state = json.loads(snapshot.state_json)
    assert state["schema_version"] == 10 and "output_assemblies" not in state["rows"]
    frozen_output = next(row for row in state["rows"]["artifacts"] if row["id"] == output.id)
    assert (
        frozen_output["role"] == SAVED_SPEECH_ROLE
        and frozen_output["metadata_json"] == output_metadata
    )
    assert not any(
        edge["child_artifact_id"] == output.id for edge in state["rows"]["artifact_edges"]
    )
    target = result["source_session_id"]

    def owned_saved(sid):
        with f.case.database.snapshot_session() as session:
            row = session.scalar(
                select(m.Artifact).where(
                    m.Artifact.session_id == sid, m.Artifact.role == SAVED_SPEECH_ROLE
                )
            )
            assert row is not None
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(m.OutputAssembly)
                    .where(m.OutputAssembly.session_id == sid)
                )
                == 0
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(m.GenerationRun)
                    .where(m.GenerationRun.session_id == sid)
                )
                == 0
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(m.ArtifactEdge)
                    .where(m.ArtifactEdge.child_artifact_id == row.id)
                )
                == 0
            )
            assert row.metadata_json == output_metadata
            return row

    copied = owned_saved(target)
    assert (f.services.paths.root / copied.relative_path).read_bytes() == output_bytes
    copied_takes = active_rows(f, target)
    assert len([row for row in copied_takes if row.take]) == 1
    assert all(
        row.take.generation_run_id is None and row.take.parent_take_id is None
        for row in copied_takes
        if row.take
    )
    with f.case.database.snapshot_session() as session:
        pins = list(
            session.scalars(
                select(m.ProjectSpeechReference).where(
                    m.ProjectSpeechReference.session_id == target
                )
            )
        )
        assert len(pins) == 2
        for pin in pins:
            artifact_row = session.get_one(m.Artifact, pin.input_artifact_id)
            assert (
                artifact_row.workspace_id,
                artifact_row.project_id,
                artifact_row.session_id,
            ) == (users.workspace_id, result["project_id"], target)
        copied_master = session.scalar(
            select(m.Artifact).where(
                m.Artifact.session_id == target, m.Artifact.role == "soundtrack_master"
            )
        )
        copied_export = session.scalar(
            select(m.Artifact).where(
                m.Artifact.session_id == target, m.Artifact.role == "export_dubbed_audio"
            )
        )
        assert copied_master is not None and copied_export is not None
        assert copied_master.metadata_json["soundtrack"]["speech_id"] == copied.id
        assert copied_export.metadata_json["soundtrack_master_id"] == copied_master.id
        for model, count in counts.items():
            assert session.scalar(select(func.count()).select_from(model)) == count
    with f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.WorkspaceMembership, (users.workspace_id, users.alice_id)))
    assert users.bob.get(f"/api/v1/sessions/{p.source['id']}").status_code == 404
    assert users.bob.get(f"/api/v1/artifacts/{output.id}/content").status_code == 404
    for voice in p.voices:
        assert users.bob.get(f"/api/v1/voices/{voice['id']}").status_code == 404
    download = users.bob.get(f"/api/v1/artifacts/{copied.id}/content")
    assert download.status_code == 200 and download.data == output_bytes
    actor = actor_from_principal(current_browser_principal(replace(f.case, client=users.bob)))
    assert actor is not None
    handlers = f.case.app.extensions["pandrator"]["workflow_handlers"]
    with execution_actor_scope(actor):
        inputs = resolve_export_inputs(
            handlers._output_workflow_context(),
            {
                "session_id": target,
                "settings": {
                    "format": "mp3",
                    "export_mode": "audio",
                    "audio_mode": "dubbed",
                    "include_sounds": False,
                },
            },
        )
    assert inputs.assembled_audio_candidates == ()
    assert select_generated_audio(inputs, legacy_role="audiobook_audio") is None
    _recopy_snapshot, recopy_preview, second = public_copy(
        f,
        users.bob,
        users.bh,
        {"project_id": result["project_id"]},
        worker,
        "saved-output-second-copy",
    )
    assert recopy_preview["counts"]["saved_speech_outputs"] == 1
    recopied = owned_saved(second["source_session_id"])
    assert (
        recopied.id != copied.id
        and (f.services.paths.root / recopied.relative_path).read_bytes() == output_bytes
    )
    assert users.bob.get(f"/api/v1/artifacts/{recopied.id}/content").data == output_bytes
    fresh = body(
        users.bob.post(
            f"/api/v1/sessions/{target}/output-assemblies",
            json={},
            headers=headers(users.bh, "saved-output-member-current-assembly"),
        ),
        202,
    )
    assert worker.run_once()
    fresh_job = f.services.jobs.get(fresh["job_id"])
    assert fresh_job.status == "succeeded", fresh_job.error_message
    with f.case.database.snapshot_session() as session:
        live = session.get_one(m.OutputAssembly, fresh["id"])
        assert live.generation_run_id is None and live.artifact_id != copied.id
        assert session.get_one(m.Artifact, live.artifact_id).role == "assembled_audio"
        assert_original_rows(session, original_rows)
        for model, count in counts.items():
            assert session.scalar(select(func.count()).select_from(model)) == count
    with execution_actor_scope(actor):
        fresh_inputs = resolve_export_inputs(
            handlers._output_workflow_context(),
            {
                "session_id": target,
                "settings": {
                    "format": "mp3",
                    "export_mode": "audio",
                    "audio_mode": "dubbed",
                    "include_sounds": False,
                },
            },
        )
    selected = select_generated_audio(fresh_inputs, legacy_role="audiobook_audio")
    assert selected is not None and selected.id == live.artifact_id
    for relative, digest in original_files.items():
        assert hashlib.sha256((f.services.paths.root / relative).read_bytes()).hexdigest() == digest
    assert len(calls) == 2
    (tmp_path / "registered-output-proof.json").write_text(
        json.dumps(
            {
                "source_generations": 1,
                "source_assemblies": 1,
                "normal_exports": 1,
                "saved_files": 1,
                "registered_copies": 2,
                "copied_runs": 0,
                "copied_assemblies": 0,
                "source_files_unchanged": len(original_files),
                "member_download_exact": True,
                "member_fresh_assembly": True,
                "provider_requests": len(calls),
            },
            indent=2,
        )
    )
