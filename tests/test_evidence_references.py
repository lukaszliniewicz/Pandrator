"""Pinned historical evidence reads and bounded current-revision references."""

from __future__ import annotations

import json
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from pandrator.web import models as m
from pandrator.web.access.contracts import AccessContext
from pandrator.web.accounts.credential_resources import CredentialResources
from pandrator.web.accounts.execution_actor import actor_from_principal
from pandrator.web.evidence_access import list_evidence_in_session
from pandrator.web.model_base import new_id
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_account_authentication_integration import current_browser_principal
from tests.test_collaboration_authentication import enable, new_account
from tests.test_evidence_access_routes import admit, body, get, headers, resolve, run, source
from tests.test_evidence_access_routes import (
    local_provider_boundary as local_provider_boundary,  # noqa: F401
)
from tests.test_project_sharing_routes import login
from tests.test_two_person_collaboration_flow import _active_transcription

ERROR = "Saved audio evidence references are unavailable."


def _context(case) -> AccessContext:
    actor = actor_from_principal(current_browser_principal(case))
    assert actor is not None
    return AccessContext(actor)


def refs(client, src, artifact_id, *, flag="true", **kwargs):
    query = {"include_referenced": flag}
    if artifact_id is not None:
        query["source_artifact_id"] = artifact_id
    return client.get(
        f"/api/v1/sessions/{src.session_id}/subtitle-evidence", query_string=query, **kwargs
    )


def review(case, client, csrf, src, evidence_ids, key):
    revision, _ = _active_transcription(case, src.session_id)
    body(
        client.post(
            f"/api/v1/sessions/{src.session_id}/subtitles/transcription/review",
            json={
                "expected_revision": revision,
                "segments": [
                    {
                        "start_ms": 1000,
                        "end_ms": 2000,
                        "text": "Saved verified cue",
                        "evidence_ids": evidence_ids,
                    }
                ],
            },
            headers=headers(csrf, key),
        ),
        201,
    )
    return _active_transcription(case, src.session_id)[1]


@pytest.fixture
def history(case, monkeypatch):
    enable(case, monkeypatch)
    owner, csrf = login(case, "owner")
    src = source(case, owner, csrf)
    first = admit(owner, csrf, src, "original-evidence")
    run(case, first)
    pinned = get(owner, first)["record"]
    decision = {
        "action": "accepted",
        "candidate_id": pinned["candidates"][0]["id"],
        "expected_revision": pinned["revision"],
    }
    body(resolve(owner, csrf, src, first, decision, "original-accepted-decision"))
    r2 = review(case, owner, csrf, src, [pinned["id"]], "save-reviewed-r2")
    r3 = review(case, owner, csrf, src, [], "save-reviewed-r3")
    direct = admit(
        owner,
        csrf,
        SimpleNamespace(session_id=src.session_id, artifact_id=r2),
        "r2-direct-evidence",
    )
    unrelated = admit(
        owner,
        csrf,
        SimpleNamespace(session_id=src.session_id, artifact_id=r3),
        "r3-unrelated-evidence",
    )
    return SimpleNamespace(
        owner=owner,
        csrf=csrf,
        src=src,
        first=first,
        pinned=pinned,
        r2=r2,
        r3=r3,
        direct=direct,
        unrelated=unrelated,
    )


def ids(response):
    return {item["record"]["id"] for item in body(response)["items"]}


@contextmanager
def sql_reads(database):
    seen = []

    def capture(_conn, _cursor, statement, parameters, _context, _many):
        seen.append((statement, parameters))

    event.listen(database.engine, "before_cursor_execute", capture)
    try:
        yield seen
    finally:
        event.remove(database.engine, "before_cursor_execute", capture)


def test_normal_r2_returns_saved_r1_and_direct_requests_without_unrelated_history(case, history):
    h = history
    expected = {h.first["record"]["id"], h.direct["record"]["id"]}
    response = refs(h.owner, h.src, h.r2)
    assert ids(response) == expected
    assert ids(refs(h.owner, h.src, h.r2, flag="false")) == {h.direct["record"]["id"]}
    # The document active pointer is r3; the requested r2 pin controls references.
    assert _active_transcription(case, h.src.session_id)[1] == h.r3
    old = next(
        item["record"]
        for item in response.get_json()["items"]
        if item["record"]["id"] == h.pinned["id"]
    )
    assert old["source_revision_id"] == h.pinned["source_revision_id"]
    assert old["source_artifact_id"] == h.pinned["source_artifact_id"] == h.src.artifact_id
    assert old["candidates"] == h.pinned["candidates"]
    with case.database.snapshot_session() as session:
        artifact = session.get_one(m.Artifact, h.r2)
        revision_id = artifact.metadata_json["revision_id"]
        segment = session.scalar(select(m.Segment).where(m.Segment.revision_id == revision_id))
        assert h.pinned["id"] in segment.metadata_json["evidence_ids"]
        assert artifact.metadata_json.get("logical_passages") is not None
    # Direct call measures only the reader, independent of transport bookkeeping.
    context = _context(case)
    with sql_reads(case.database) as sql, case.database.snapshot_session() as session:
        value = list_evidence_in_session(
            session, context, h.src.session_id, h.r2, include_referenced=True
        )
    assert {item["record"]["id"] for item in value["items"]} == expected
    reference = [
        statement
        for statement, _ in sql
        if "FROM artifacts JOIN document_revisions" in statement or "FROM segments" in statement
    ]
    assert len(reference) == 2
    assert len([statement for statement, _ in sql if "FROM subtitle_evidence" in statement]) == 1
    assert len([statement for statement, _ in sql if "FROM jobs" in statement]) == 1
    assert not any(
        statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        for statement, _ in sql
    )


def test_viewing_foreign_artifacts_token_ceiling_and_revocation(case, history):
    h = history
    reader = new_account(case, "reader")
    new_account(case, "foreign-admin", "administrator")
    viewer, vh = login(case, "reader")
    admin, _ = login(case, "foreign-admin")
    path = f"/api/v1/projects/{h.src.project_id}/viewers"
    revision = body(h.owner.get(path))["revision"]
    granted = body(
        h.owner.put(
            f"{path}/{reader.user_id}", json={"expected_revision": revision}, headers=h.csrf
        )
    )
    assert ids(refs(viewer, h.src, h.r2)) == {h.pinned["id"], h.direct["record"]["id"]}
    decision = {
        "action": "edited",
        "text": "Cannot decide",
        "expected_revision": get(viewer, h.first)["record"]["revision"],
    }
    assert resolve(viewer, vh, h.src, h.first, decision, "viewer-no-resolve").status_code == 403
    assert refs(admin, h.src, h.r2).status_code == 404
    foreign = source(case, viewer, vh)
    assert refs(viewer, h.src, foreign.artifact_id).status_code == 404
    _, raw = case.auth.create_api_token(
        "Referenced project reader",
        scopes=frozenset({"app.read"}),
        user_id=reader.user_id,
        resource_grants=CredentialResources(project_ids=frozenset({h.src.project_id})),
    )
    api = case.app.test_client()
    bearer = {"Authorization": f"Bearer {raw}"}
    assert ids(refs(api, h.src, h.r2, headers=bearer)) == {h.pinned["id"], h.direct["record"]["id"]}
    assert refs(api, foreign, foreign.artifact_id, headers=bearer).status_code == 404
    assert refs(api, h.src, foreign.artifact_id, headers=bearer).status_code == 404
    body(
        h.owner.post(
            f"{path}/{reader.user_id}/revoke",
            json={"expected_revision": granted["revision"]},
            headers=h.csrf,
        )
    )
    assert refs(viewer, h.src, h.r2).status_code == 404
    assert refs(api, h.src, h.r2, headers=bearer).status_code == 404


def test_missing_corrupt_and_bad_shaped_references_fail_safely(case, history):
    h = history
    assert refs(h.owner, h.src, None).status_code == 422
    assert refs(h.owner, h.src, h.r2, flag="yes").status_code == 422
    assert refs(h.owner, h.src, new_id()).status_code == 404
    with case.database.snapshot_session() as session:
        original = deepcopy(session.get_one(m.Artifact, h.r2).metadata_json)
        revision_id = original["revision_id"]
        segment_id = session.scalar(
            select(m.Segment.id).where(m.Segment.revision_id == revision_id)
        )
        segment_metadata = deepcopy(session.get_one(m.Segment, segment_id).metadata_json)
    for packet in (
        [],
        {"items": {}},
        {"items": [None]},
        {"items": [{"evidence_ids": "bad"}]},
        {"items": [{"evidence_ids": [""]}]},
        {"items": [{"evidence_ids": [7]}]},
        {"items": [{"evidence_ids": ["x" * 121]}]},
        {"items": [{"evidence_ids": [new_id()]}]},
    ):
        with case.database.immediate_session() as session:
            session.get_one(m.Artifact, h.r2).metadata_json = {
                **original,
                "logical_passages": packet,
            }
        with sql_reads(case.database) as sql:
            rejected = refs(h.owner, h.src, h.r2)
        assert body(rejected, 422)["error"]["message"] == ERROR
        assert not any(
            statement.lstrip()
            .upper()
            .startswith(("INSERT INTO SUBTITLE_EVIDENCE", "UPDATE SUBTITLE_EVIDENCE"))
            for statement, _ in sql
        )
    with case.database.immediate_session() as session:
        session.get_one(m.Artifact, h.r2).metadata_json = original
        session.get_one(m.Segment, segment_id).metadata_json = {
            **segment_metadata,
            "evidence_ids": "bad",
        }
    assert body(refs(h.owner, h.src, h.r2), 422)["error"]["message"] == ERROR
    with case.database.immediate_session() as session:
        session.get_one(m.Segment, segment_id).metadata_json = segment_metadata
        session.get_one(m.Artifact, h.r2).metadata_json = {"revision_id": new_id()}
    assert refs(h.owner, h.src, h.r2).status_code == 404


def test_cross_session_link_is_opaque_and_never_fetches_foreign_body(case, history):
    h = history
    new_account(case, "other-source")
    client, csrf = login(case, "other-source")
    foreign = source(case, client, csrf)
    accepted = admit(client, csrf, foreign, "foreign-evidence")
    with case.database.immediate_session() as session:
        artifact = session.get_one(m.Artifact, h.r2)
        artifact.metadata_json = {
            **artifact.metadata_json,
            "logical_passages": {"items": [{"evidence_ids": [accepted["record"]["id"]]}]},
        }
    context = _context(case)
    with (
        sql_reads(case.database) as sql,
        case.database.snapshot_session() as session,
        pytest.raises(ValueError, match="^" + ERROR.replace(".", "\\.") + "$"),
    ):
        list_evidence_in_session(session, context, h.src.session_id, h.r2, include_referenced=True)
    queries = [
        (statement, parameters)
        for statement, parameters in sql
        if "FROM subtitle_evidence" in statement
    ]
    assert len(queries) == 1 and "subtitle_evidence.session_id = ?" in queries[0][0]
    assert h.src.session_id in queries[0][1] and foreign.session_id not in queries[0][1]
    assert not any("FROM jobs" in statement for statement, _ in sql)
    assert body(refs(h.owner, h.src, h.r2), 422)["error"]["message"] == ERROR


def test_missing_null_packets_and_empty_jobs_preserve_direct_behavior(case):
    csrf = {"X-CSRF-Token": case.csrf}
    src = source(case, case.client, csrf)
    with case.database.snapshot_session() as session:
        metadata = deepcopy(session.get_one(m.Artifact, src.artifact_id).metadata_json)
        revision_id = metadata["revision_id"]
        segments = list(
            session.scalars(select(m.Segment.id).where(m.Segment.revision_id == revision_id))
        )
    context = _context(case)
    for packet in (None, {}, {"items": None}, {"items": [{}, {"evidence_ids": None}]}):
        with case.database.immediate_session() as session:
            session.get_one(m.Artifact, src.artifact_id).metadata_json = {
                **metadata,
                "logical_passages": packet,
            }
            for segment_id in segments:
                session.get_one(m.Segment, segment_id).metadata_json = {"evidence_ids": None}
        with sql_reads(case.database) as sql, case.database.snapshot_session() as session:
            assert not list_evidence_in_session(
                session, context, src.session_id, src.artifact_id, include_referenced=True
            )["items"]
        assert not any("FROM jobs" in statement for statement, _ in sql)
    assert body(refs(case.client, src, None, flag="false"))["items"] == []


def test_thousand_real_rows_use_two_bound_json_arrays_and_limit_before_body(case, tmp_path):
    csrf = {"X-CSRF-Token": case.csrf}
    src = source(case, case.client, csrf)
    services = case.app.extensions["pandrator"]["services"]
    with case.database.snapshot_session() as session:
        artifact = session.get_one(m.Artifact, src.artifact_id)
        old_revision = artifact.metadata_json["revision_id"]
        document_id = session.get_one(m.DocumentRevision, old_revision).document_id
        workspace_id = artifact.workspace_id
        path = services.paths.managed_path(artifact.relative_path).with_name("current-bulk.srt")
    identifiers = [new_id() for _ in range(1000)]
    with case.database.immediate_session() as session:
        revision = m.DocumentRevision(
            document_id=document_id, revision_number=2, content_hash="bulk-revision"
        )
        session.add(revision)
        session.flush()
        segment = m.Segment(
            revision_id=revision.id,
            ordinal=0,
            start_ms=1000,
            end_ms=2000,
            text="No text read",
            metadata_json={"evidence_ids": identifiers[500:]},
        )
        session.add(segment)
        for evidence_id in identifiers:
            job = m.Job(
                id=new_id(),
                kind="subtitle.evidence",
                workspace_id=workspace_id,
                project_id=src.project_id,
                session_id=src.session_id,
                status="failed",
                payload_json={"private-input": "MUST NOT SELECT"},
                result_json={"private-result": "MUST NOT SELECT"},
            )
            session.add(job)
            session.flush()
            session.add(
                m.SubtitleEvidence(
                    id=evidence_id,
                    session_id=src.session_id,
                    job_id=job.id,
                    source_artifact_id=src.artifact_id,
                    source_media_artifact_id=src.media_id,
                    source_revision_id=old_revision,
                    cue_id=1,
                    start_ms=1000,
                    end_ms=2000,
                    clip_start_ms=0,
                    clip_end_ms=4000,
                    reason="Minimal saved reference",
                    status="failed",
                )
            )
        current_revision = revision.id
    path.write_text("1\n00:00:01,000 --> 00:00:02,000\nBulk current cue\n")
    current = services.artifacts.register(
        path,
        kind="srt",
        role="transcription",
        session_id=src.session_id,
        metadata={
            "revision_id": current_revision,
            "logical_passages": {"items": [{"evidence_ids": identifiers[:500]}]},
        },
    )
    context = _context(case)
    with sql_reads(case.database) as sql, case.database.snapshot_session() as session:
        value = list_evidence_in_session(
            session, context, src.session_id, current.id, include_referenced=True
        )
    assert {item["record"]["id"] for item in value["items"]} == set(identifiers)
    evidence_queries = [(s, p) for s, p in sql if "FROM subtitle_evidence" in s]
    job_queries = [(s, p) for s, p in sql if "FROM jobs" in s]
    assert len(evidence_queries) == len(job_queries) == 1
    assert all(
        "json_each" in statement.lower() and len(parameters) < 50
        for statement, parameters in [*evidence_queries, *job_queries]
    )
    assert not any(
        "jobs.payload_json" in statement or "jobs.result_json" in statement for statement, _ in sql
    )
    segment_queries = [statement for statement, _ in sql if "FROM segments" in statement]
    assert len(segment_queries) == 1 and "JSON_EXTRACT" in segment_queries[0]
    assert (
        "segments.text" not in segment_queries[0]
        and "segments.words_json" not in segment_queries[0]
    )
    assert not any(
        statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        for statement, _ in sql
    )
    summary = {
        "rows": 1000,
        "statements": len(sql),
        "evidence_queries": len(evidence_queries),
        "job_queries": len(job_queries),
        "evidence_bind_count": len(evidence_queries[0][1]),
        "job_bind_count": len(job_queries[0][1]),
        "max_bind_count": max(len(parameters) for _, parameters in sql),
        "segment_query": segment_queries[0],
        "evidence_query": evidence_queries[0][0],
        "job_query": job_queries[0][0],
    }
    output = tmp_path / "bounded-sql.json"
    output.write_text(json.dumps(summary, indent=2))
    with case.database.immediate_session() as session:
        artifact = session.get_one(m.Artifact, current.id)
        artifact.metadata_json = {
            **artifact.metadata_json,
            "logical_passages": {"items": [{"evidence_ids": identifiers + [new_id()]}]},
        }
    with (
        sql_reads(case.database) as rejected,
        case.database.snapshot_session() as session,
        pytest.raises(ValueError, match="Saved audio evidence references are unavailable"),
    ):
        list_evidence_in_session(
            session, context, src.session_id, current.id, include_referenced=True
        )
    assert not any(
        "FROM subtitle_evidence" in statement or "FROM jobs" in statement
        for statement, _ in rejected
    )
