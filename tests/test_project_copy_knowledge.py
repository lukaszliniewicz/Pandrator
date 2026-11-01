"""Saved research and glossary state follows current-project copies."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import zlib
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import event, select

from pandrator.web import models as m
from pandrator.web.knowledge import KnowledgeLedgerStore
from pandrator.web.model_base import new_id
from pandrator.web.projects import copy_jobs, copy_publication
from pandrator.web.projects.copy_contracts import (
    COPY_STATE_VERSION,
    ProjectCopyError,
    public_copy_job_result,
)
from pandrator.web.projects.copy_publication import copy_identity
from tests.project_copy_test_support import prepare_legacy_copy_state, preview_with_connections
from tests.test_project_copy_jobs import _admit, _job, _worker
from tests.test_project_copy_jobs import job_case as _job_case
from tests.test_project_copy_publication import _capture as _capture_publication
from tests.test_project_copy_publication import _publish, _stage
from tests.test_project_copy_publication import publication_case as _publication_case
from tests.test_project_copy_snapshot import _capture as _capture_snapshot
from tests.test_project_copy_snapshot import copy_case as _copy_case
from tests.test_project_export_bundles import bundle_case as _bundle_case

bundle_case = _bundle_case
copy_case = _copy_case
job_case = _job_case
publication_case = _publication_case

_COPY_ERROR = "The saved project state is incomplete or inconsistent."


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*_args, **_kwargs):
        raise AssertionError("Network is not part of project copying")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


def _capture(case):
    return _capture_snapshot(case)


def _insert_ledger(
    case,
    session_id: str,
    kind: str,
    *,
    source_language: str = "en",
    target_language: str = "fr",
    payload: dict[str, Any] | None = None,
) -> str:
    if payload is None:
        payload = (
            {"evidence": [], "warnings": [], "summary": "saved research"}
            if kind == "research"
            else {"entries": [], "conflicts": []}
        )
    with case.database.immediate_session() as session:
        row = m.KnowledgeLedger(
            session_id=session_id,
            kind=kind,
            source_language=source_language,
            target_language=target_language,
            payload_json=payload,
        )
        session.add(row)
        session.flush()
        return row.id


def _ledger_rows(case, session_ids: set[str] | None = None) -> list[dict[str, Any]]:
    with case.database.snapshot_session() as session:
        statement = select(m.KnowledgeLedger)
        if session_ids is not None:
            statement = statement.where(m.KnowledgeLedger.session_id.in_(session_ids))
        return [
            {
                "id": row.id,
                "session_id": row.session_id,
                "kind": row.kind,
                "source_language": row.source_language,
                "target_language": row.target_language,
                "payload_json": row.payload_json,
                "revision": row.revision,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
            for row in session.scalars(statement.order_by(m.KnowledgeLedger.id))
        ]


def _set_snapshot_state(snapshot, state):
    encoded = json.dumps(
        state, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return replace(
        snapshot,
        state_json=encoded,
        fingerprint=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    )


def _refresh_publication_case(case):
    case.snapshot = _capture_publication(case)
    case.staged = _stage(case, case.snapshot)
    return case.snapshot, case.staged


def _publish_failure(case, snapshot, staged):
    with case.database.immediate_session() as session:
        with pytest.raises(ProjectCopyError) as failure:
            _publish(session, case, snapshot, staged)
    root_id = copy_identity(case.copy_id, "projects", case.source.project_id)
    with case.database.snapshot_session() as session:
        assert session.get(m.Project, root_id) is None
    return failure.value


def test_capture_saves_current_ledgers_for_every_language_without_creating_rows(copy_case):
    case = copy_case
    session_ids = [case.source.id, *(branch["session_id"] for branch in case.branches)]
    payloads = [
        {"evidence": [{"url": "https://research.invalid/source", "quote": "Saved quote"}],
         "warnings": [" Keep  this warning "], "summary": " Research   summary ",
         "custom": {"note": "extra field"}},
        {"entries": [{"source": "  source phrase ", "target": "  target phrase ",
                       "locked": True, "status": "disabled", "note": "keep exact"}],
         "conflicts": [{"term": "source phrase", "candidates": ["a", "b"]}]},
    ]
    ids = []
    for index, session_id in enumerate(session_ids):
        target = "" if index == 0 else case.branches[index - 1]["target_language"]
        source = "auto" if index == 0 else "en"
        ids.extend(
            [
                _insert_ledger(
                    case,
                    session_id,
                    "research",
                    source_language=source,
                    target_language=target,
                    payload=payloads[0],
                ),
                _insert_ledger(
                    case,
                    session_id,
                    "glossary",
                    source_language=source,
                    target_language=target,
                    payload=payloads[1],
                ),
            ]
        )

    before_rows = _ledger_rows(case)
    snapshot = _capture(case)
    state = json.loads(snapshot.state_json)
    ledgers = state["rows"]["knowledge_ledgers"]

    assert snapshot.ready, snapshot.blockers
    assert state["schema_version"] == COPY_STATE_VERSION == 10
    assert len(state["rows"]) == 27
    assert {row["id"] for row in ledgers} == set(ids)
    assert {row["session_id"] for row in ledgers} == set(session_ids)
    assert {row["kind"] for row in ledgers} == {"research", "glossary"}
    assert all(
        set(row)
        == {"id", "session_id", "kind", "source_language", "target_language", "payload_json"}
        for row in ledgers
    )
    glossary = next(row for row in ledgers if row["kind"] == "glossary")
    assert glossary["payload_json"] == payloads[1]
    research = next(row for row in ledgers if row["kind"] == "research")
    assert research["payload_json"] == payloads[0]
    assert snapshot.manifest()["counts"]["knowledge_ledgers"] == len(ids)
    assert _ledger_rows(case) == before_rows


def test_ledger_payload_changes_fingerprint_but_revision_and_timestamps_do_not(copy_case):
    case = copy_case
    ledger_id = _insert_ledger(
        case,
        case.source.id,
        "research",
        source_language="auto",
        target_language="",
    )
    original = _capture(case)
    with case.database.immediate_session() as session:
        row = session.get(m.KnowledgeLedger, ledger_id)
        row.revision += 1
        row.created_at = m.utcnow()
        row.updated_at = m.utcnow()
    assert _capture(case).fingerprint == original.fingerprint

    with case.database.immediate_session() as session:
        row = session.get(m.KnowledgeLedger, ledger_id)
        row.payload_json = {**row.payload_json, "summary": "changed saved research"}
    changed = _capture(case)
    assert changed.fingerprint != original.fingerprint


def test_unknown_ledger_kind_is_blocked_counted_and_body_is_not_captured(copy_case):
    case = copy_case
    secret_body = "unknown ledger body must never enter the copy"
    _insert_ledger(
        case,
        case.source.id,
        "terms",
        payload={"body": secret_body},
    )

    snapshot = _capture(case)
    state = json.loads(snapshot.state_json)

    assert not snapshot.ready
    assert ("unsupported_state", "knowledge_ledgers") in snapshot.blockers
    assert state["unsupported_counts"]["knowledge_ledgers"] == 1
    assert state["rows"]["knowledge_ledgers"] == []
    assert secret_body not in snapshot.state_json

    with case.database.immediate_session() as session:
        unknown = session.scalar(
            select(m.KnowledgeLedger).where(m.KnowledgeLedger.kind == "terms")
        )
        session.delete(unknown)
    _insert_ledger(
        case,
        case.source.id,
        "research",
        source_language="auto",
        target_language="",
        payload={"evidence": "not a list", "warnings": [], "summary": "bad"},
    )

    with pytest.raises(ProjectCopyError) as failure:
        _capture(case)

    assert failure.value.code == "copy_graph_invalid"
    assert str(failure.value) == _COPY_ERROR


def test_known_ledger_cap_fails_after_count_and_before_payload_select(copy_case):
    case = copy_case
    payload = {"evidence": [], "warnings": [], "summary": "bounded"}
    with case.database.immediate_session() as session:
        session.add_all(
            m.KnowledgeLedger(
                session_id=case.source.id,
                kind="research",
                source_language=f"lang-{index:04d}",
                target_language="en",
                payload_json=payload,
            )
            for index in range(1_001)
        )
    selected_payloads: list[str] = []

    def observe(_conn, _cursor, statement, _parameters, _context, _executemany):
        normalized = statement.lower()
        if "select" in normalized and "knowledge_ledgers" in normalized and "payload_json" in normalized:
            selected_payloads.append(statement)

    event.listen(case.database.engine, "before_cursor_execute", observe)
    try:
        with pytest.raises(ProjectCopyError) as failure:
            _capture(case)
    finally:
        event.remove(case.database.engine, "before_cursor_execute", observe)

    assert failure.value.code == "copy_limit_exceeded"
    assert selected_payloads == []


def test_real_worker_copies_scoped_ledgers_and_copy_edits_are_independent(job_case):
    case = job_case
    session_ids = [case.source.id, *(branch["session_id"] for branch in case.branches)]
    source_rows = []
    private_secret = "ledger-api-key-must-not-copy"
    private_path = "/private/source/ledger.json"
    for index, session_id in enumerate(session_ids):
        target = "" if index == 0 else case.branches[index - 1]["target_language"]
        source = "auto" if index == 0 else "en"
        source_rows.extend(
            [
                {
                    "id": _insert_ledger(
                        case,
                        session_id,
                        "research",
                        source_language=source,
                        target_language=target,
                        payload={
                            "evidence": [
                                {
                                    "url": "https://research.invalid/evidence",
                                    "quote": f"Reference UUID prose {new_id()}",
                                    "note": "retain this evidence field",
                                    "api_key": private_secret,
                                }
                            ],
                            "warnings": ["  preserve   warning  "],
                            "summary": " Research   summary ",
                            "extra": {"note": "retained evidence detail"},
                            "source_path": private_path,
                            "job_id": new_id(),
                        },
                    ),
                    "kind": "research",
                    "session_id": session_id,
                },
                {
                    "id": _insert_ledger(
                        case,
                        session_id,
                        "glossary",
                        source_language=source,
                        target_language=target,
                        payload={
                            "entries": [
                                {
                                    "source": "  source phrase ",
                                    "target": "  target phrase ",
                                    "locked": True,
                                    "status": "disabled",
                                    "note": f"Human UUID {new_id()}",
                                    "api_key": private_secret,
                                }
                            ],
                            "conflicts": [
                                {"term": "source phrase", "candidates": ["one", "two"]}
                            ],
                            "source_path": private_path,
                            "generation_run_id": new_id(),
                        },
                    ),
                    "kind": "glossary",
                    "session_id": session_id,
                },
            ]
        )
    before = {row["id"]: row for row in _ledger_rows(case)}
    preview = preview_with_connections(
        case, case.owner_context, case.source.project_id, case.destination
    )
    assert preview["ready"] is True
    assert "Research   summary" not in json.dumps(preview)
    assert private_secret not in json.dumps(preview)

    admitted, status = _admit(case, fingerprint=preview["fingerprint"])
    assert status == 202
    public_result = public_copy_job_result(admitted["id"])
    assert public_result == {"copy_id": admitted["id"]}
    assert private_secret not in json.dumps(public_result)
    _worker(case)
    job = _job(case, admitted["id"])
    assert job.status == "succeeded"
    result = job.result_json
    assert result["fingerprint"] == preview["fingerprint"]
    copied_session_ids = {
        copy_identity(admitted["id"], "sessions", session_id) for session_id in session_ids
    }
    copied_rows = _ledger_rows(case, copied_session_ids)
    assert len(copied_rows) == len(source_rows) == 6
    copied_by_id = {row["id"]: row for row in copied_rows}
    source_after_worker = {row["id"]: row for row in _ledger_rows(case)}
    for item in source_rows:
        source_row = before[item["id"]]
        target_id = copy_identity(admitted["id"], "knowledge_ledgers", item["id"])
        copied = copied_by_id[target_id]
        assert target_id != item["id"]
        assert copied["session_id"] == copy_identity(
            admitted["id"], "sessions", item["session_id"]
        )
        assert copied["kind"] == item["kind"]
        assert copied["source_language"] == source_row["source_language"]
        assert copied["target_language"] == source_row["target_language"]
        assert copied["revision"] == 1
        assert copied["created_at"] is not None and copied["updated_at"] is not None
        assert source_after_worker[item["id"]]["payload_json"] == source_row["payload_json"]
        assert source_after_worker[item["id"]]["revision"] == source_row["revision"]
        assert source_after_worker[item["id"]]["session_id"] == source_row["session_id"]
        assert source_after_worker[item["id"]]["source_language"] == source_row["source_language"]
        assert source_after_worker[item["id"]]["target_language"] == source_row["target_language"]
        payload = copied["payload_json"]
        assert private_secret not in json.dumps(payload)
        assert private_path not in json.dumps(payload)
        assert "job_id" not in payload and "generation_run_id" not in payload
        if item["kind"] == "research":
            assert payload["summary"] == " Research   summary "
            assert payload["warnings"] == ["  preserve   warning  "]
            evidence = payload["evidence"][0]
            assert evidence["url"] == "https://research.invalid/evidence"
            assert evidence["quote"].startswith("Reference UUID prose ")
            assert evidence["note"] == "retain this evidence field"
        else:
            entry = payload["entries"][0]
            assert entry["source"] == "  source phrase "
            assert entry["target"] == "  target phrase "
            assert entry["locked"] is True and entry["status"] == "disabled"
            assert entry["note"].startswith("Human UUID ")
            assert payload["conflicts"] == [
                {"term": "source phrase", "candidates": ["one", "two"]}
            ]

    glossary_source_id = next(item["id"] for item in source_rows if item["kind"] == "glossary")
    glossary_target_id = copy_identity(
        admitted["id"], "knowledge_ledgers", glossary_source_id
    )
    changed_payload = {
        **copied_by_id[glossary_target_id]["payload_json"],
        "entries": [
            {
                **copied_by_id[glossary_target_id]["payload_json"]["entries"][0],
                "target": "edited only in copy",
            }
        ],
    }
    KnowledgeLedgerStore(case.database).replace(glossary_target_id, 1, changed_payload)
    after = {row["id"]: row for row in _ledger_rows(case)}
    assert after[glossary_source_id]["payload_json"] == before[glossary_source_id]["payload_json"]
    assert after[glossary_target_id]["revision"] == 2


def test_forged_ledger_scopes_are_rejected_before_any_publication(publication_case):
    case = publication_case
    original_id = _insert_ledger(case, case.source.id, "research")
    snapshot, _staged = _refresh_publication_case(case)
    for mutation in (
        "duplicate_scope",
        "foreign_session",
        "noncanonical_kind",
        "malformed_payload",
    ):
        state = json.loads(snapshot.state_json)
        row = next(
            item for item in state["rows"]["knowledge_ledgers"] if item["id"] == original_id
        )
        if mutation == "duplicate_scope":
            duplicate = dict(row)
            duplicate["id"] = new_id()
            state["rows"]["knowledge_ledgers"].append(duplicate)
        elif mutation == "foreign_session":
            row["session_id"] = new_id()
        elif mutation == "noncanonical_kind":
            row["kind"] = "Research"
        else:
            row["payload_json"] = {
                "evidence": "invalid after publication remapping",
                "warnings": [],
                "summary": "bad",
            }
        forged = _set_snapshot_state(snapshot, state)
        staged = _stage(case, forged)

        failure = _publish_failure(case, forged, staged)

        assert failure.code == "copy_graph_invalid"
        assert str(failure) == _COPY_ERROR
        with case.database.snapshot_session() as session:
            assert session.get(m.KnowledgeLedger, original_id) is not None
            assert session.get(
                m.KnowledgeLedger,
                copy_identity(case.copy_id, "knowledge_ledgers", original_id),
            ) is None


def test_caller_rollback_removes_published_ledger_and_graph(publication_case):
    case = publication_case
    source_ledger_id = _insert_ledger(case, case.source.id, "research")
    snapshot, staged = _refresh_publication_case(case)
    copied_ledger_id = copy_identity(case.copy_id, "knowledge_ledgers", source_ledger_id)
    copied_root_id = copy_identity(case.copy_id, "projects", case.source.project_id)

    with pytest.raises(RuntimeError, match="rollback the caller-owned copy"):
        with case.database.immediate_session() as session:
            _publish(session, case, snapshot, staged)
            assert session.get(m.KnowledgeLedger, copied_ledger_id) is not None
            raise RuntimeError("rollback the caller-owned copy")

    with case.database.snapshot_session() as session:
        assert session.get(m.Project, copied_root_id) is None
        assert session.get(m.KnowledgeLedger, copied_ledger_id) is None
        assert session.get(m.KnowledgeLedger, source_ledger_id) is not None


def test_schema1_frozen_copy_keeps_exact_fingerprint_and_ignores_new_source_ledgers(
    publication_case,
):
    case = publication_case
    state = json.loads(case.snapshot.state_json)
    assert state["schema_version"] == COPY_STATE_VERSION
    state["schema_version"] = 1
    prepare_legacy_copy_state(state)
    state["rows"].pop("project_speech_references")
    state["rows"].pop("knowledge_ledgers")
    state["rows"].pop("subtitle_evidence")
    state["rows"].pop("media_edit_plans")
    state["rows"].pop("media_edit_plan_revisions")
    legacy = _set_snapshot_state(case.snapshot, state)
    digest, encoded = copy_jobs._encode_snapshot(legacy)
    decoded = copy_jobs._decode_snapshot(encoded, digest)
    outer = json.loads(zlib.decompress(base64.b64decode(encoded)))
    frozen_fingerprint = decoded.fingerprint

    assert outer["schema_version"] == 1
    assert decoded.state_json == legacy.state_json
    assert decoded.fingerprint == legacy.fingerprint == frozen_fingerprint
    working_rows, _by_id, _all_ids = copy_publication._decode(decoded)
    assert working_rows["knowledge_ledgers"] == []
    assert working_rows["subtitle_evidence"] == []
    assert working_rows["media_edit_plans"] == []
    assert working_rows["media_edit_plan_revisions"] == []
    assert decoded.state_json == legacy.state_json

    new_source_ledger = _insert_ledger(
        case,
        case.source.id,
        "research",
        payload={"evidence": [], "warnings": [], "summary": "added after freeze"},
    )
    new_source_evidence = new_id()
    with case.database.immediate_session() as session:
        session.add(
            m.SubtitleEvidence(
                id=new_source_evidence,
                session_id=case.branches[0]["session_id"],
                source_artifact_id=case.current.id,
                source_revision_id=case.revisions[0][1],
                cue_id=1,
                start_ms=100,
                end_ms=950,
                clip_start_ms=0,
                clip_end_ms=1000,
                reason="added after freeze",
                status="queued",
            )
        )
    staged = _stage(case, decoded)
    with case.database.immediate_session() as session:
        result = _publish(session, case, decoded, staged)

    assert result.fingerprint == frozen_fingerprint
    with case.database.snapshot_session() as session:
        copied_sessions = set(
            session.scalars(
                select(m.SessionRecord.id).where(m.SessionRecord.project_id == result.project_id)
            )
        )
        assert session.get(m.KnowledgeLedger, new_source_ledger) is not None
        assert session.get(m.SubtitleEvidence, new_source_evidence) is not None
        assert not list(
            session.scalars(
                select(m.KnowledgeLedger.id).where(
                    m.KnowledgeLedger.session_id.in_(copied_sessions)
                )
            )
        )
        assert not list(
            session.scalars(
                select(m.SubtitleEvidence.id).where(
                    m.SubtitleEvidence.session_id.in_(copied_sessions)
                )
            )
        )


def test_schema2_frozen_copy_keeps_exact_fingerprint_and_ignores_later_evidence(
    publication_case,
):
    case = publication_case
    state = json.loads(case.snapshot.state_json)
    state["schema_version"] = 2
    prepare_legacy_copy_state(state)
    state["rows"].pop("project_speech_references")
    state["rows"].pop("subtitle_evidence")
    state["rows"].pop("media_edit_plans")
    state["rows"].pop("media_edit_plan_revisions")
    legacy = _set_snapshot_state(case.snapshot, state)
    digest, encoded = copy_jobs._encode_snapshot(legacy)
    decoded = copy_jobs._decode_snapshot(encoded, digest)
    working_rows, _by_id, _all_ids = copy_publication._decode(decoded)

    assert len(json.loads(decoded.state_json)["rows"]) == 18
    assert decoded.state_json == legacy.state_json
    assert decoded.fingerprint == legacy.fingerprint
    assert working_rows["subtitle_evidence"] == []

    later_evidence_id = new_id()
    with case.database.immediate_session() as session:
        session.add(
            m.SubtitleEvidence(
                id=later_evidence_id,
                session_id=case.branches[0]["session_id"],
                source_artifact_id=case.current.id,
                source_revision_id=case.revisions[0][1],
                cue_id=1,
                start_ms=100,
                end_ms=950,
                clip_start_ms=0,
                clip_end_ms=1000,
                reason="later evidence is outside the frozen bytes",
                status="completed",
            )
        )
    staged = _stage(case, decoded)
    with case.database.immediate_session() as session:
        result = _publish(session, case, decoded, staged)

    with case.database.snapshot_session() as session:
        copied_sessions = set(
            session.scalars(
                select(m.SessionRecord.id).where(m.SessionRecord.project_id == result.project_id)
            )
        )
        assert session.get(m.SubtitleEvidence, later_evidence_id) is not None
        assert not list(
            session.scalars(
                select(m.SubtitleEvidence.id).where(
                    m.SubtitleEvidence.session_id.in_(copied_sessions)
                )
            )
        )


def test_schema3_frozen_copy_ignores_later_media_edit_plan(publication_case):
    case = publication_case
    state = json.loads(case.snapshot.state_json)
    state["schema_version"] = 3
    prepare_legacy_copy_state(state)
    state["rows"].pop("project_speech_references")
    state["rows"].pop("media_edit_plans")
    state["rows"].pop("media_edit_plan_revisions")
    legacy = _set_snapshot_state(case.snapshot, state)
    digest, encoded = copy_jobs._encode_snapshot(legacy)
    decoded = copy_jobs._decode_snapshot(encoded, digest)
    working_rows, _by_id, _all_ids = copy_publication._decode(decoded)

    assert len(json.loads(decoded.state_json)["rows"]) == 19
    assert decoded.state_json == legacy.state_json
    assert decoded.fingerprint == legacy.fingerprint
    assert working_rows["media_edit_plans"] == []
    assert working_rows["media_edit_plan_revisions"] == []

    with case.database.immediate_session() as session:
        session.add(m.MediaEditPlan(session_id=case.source.id))
    staged = _stage(case, decoded)
    with case.database.immediate_session() as session:
        result = _publish(session, case, decoded, staged)

    with case.database.snapshot_session() as session:
        copied_sessions = set(
            session.scalars(
                select(m.SessionRecord.id).where(m.SessionRecord.project_id == result.project_id)
            )
        )
        assert not list(
            session.scalars(
                select(m.MediaEditPlan.id).where(m.MediaEditPlan.session_id.in_(copied_sessions))
            )
        )


def test_durable_codec_rejects_unsupported_or_boolean_state_versions(publication_case):
    case = publication_case
    for version in (True, 5):
        state = json.loads(case.snapshot.state_json)
        state["schema_version"] = version
        invalid = _set_snapshot_state(case.snapshot, state)
        digest, encoded = copy_jobs._encode_snapshot(invalid)

        with pytest.raises(ProjectCopyError) as failure:
            copy_jobs._decode_snapshot(encoded, digest)

        assert failure.value.code == "copy_input_invalid"


def test_schema2_requires_the_exact_family_key_set_before_publication(publication_case):
    case = publication_case
    _insert_ledger(case, case.source.id, "research")
    snapshot, _staged = _refresh_publication_case(case)
    for mutation in ("missing", "extra"):
        state = json.loads(snapshot.state_json)
        state["schema_version"] = 2
        prepare_legacy_copy_state(state)
        state["rows"].pop("project_speech_references")
        state["rows"].pop("subtitle_evidence")
        state["rows"].pop("media_edit_plans")
        state["rows"].pop("media_edit_plan_revisions")
        if mutation == "missing":
            state["rows"].pop("knowledge_ledgers")
        else:
            state["rows"]["unrecognized"] = []
        forged = _set_snapshot_state(snapshot, state)
        staged = _stage(case, forged)

        failure = _publish_failure(case, forged, staged)

        assert failure.code == "copy_graph_invalid"
