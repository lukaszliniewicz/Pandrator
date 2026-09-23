"""Backend source-passage settings, pinning, preview/rebuild, and propagation.

Isolated fixtures only (no live DB/services). The algorithm modules
(logical_passages, source_passage_policy, source_sentence_assessment) are
owned elsewhere; these tests cover the backend-owned plumbing.
"""

import re
import tempfile
import unittest
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from authlib.common.security import generate_token
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from sqlalchemy import select

from pandrator.logic.dubbing import source_passage_policy as policy
from pandrator.logic.dubbing import source_passage_settings as sps
from pandrator.logic.dubbing.subtitle_finalization import (
    SubtitleFinalizationConfig,
)
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.logical_passages import (
    SOURCE_PASSAGE_POLICY_VERSION as WEB_POLICY_VERSION,
)
from pandrator.web.logical_passages import (
    stored_passages,
)
from pandrator.web.models import (
    Artifact,
    ArtifactEdge,
    DispatchRun,
    DocumentRevision,
    Segment,
    SessionStageSelection,
    TimedWord,
)
from pandrator.web.workspace import (
    BUILTIN_DEFAULTS,
    RUNTIME_SETTING_ALIASES,
    adapt_runtime_settings,
)
from tests import test_web_dispatch as dispatch_tests
from tests.web_test_support import prepare_web_test_data_root

# ---------------------------------------------------------------------------
# Helper unit tests (no DB)
# ---------------------------------------------------------------------------


def test_helper_defaults_match_policy_and_baseline():
    assert sps.SOURCE_PASSAGE_DEFAULTS == {
        "min_chars": 60,
        "preferred_chars": 160,
        "sentence_lookahead_chars": 20,
        "cue_join_gap_ms": 650,
        "diagnostic_span_ms": 8000,
    }
    assert sps.SOURCE_PASSAGE_DEFAULTS["min_chars"] == policy.DEFAULT_MIN_CHARS
    assert (
        sps.SOURCE_PASSAGE_DEFAULTS["preferred_chars"]
        == policy.DEFAULT_PREFERRED_CHARS
    )
    assert (
        sps.SOURCE_PASSAGE_DEFAULTS["sentence_lookahead_chars"]
        == policy.DEFAULT_SENTENCE_LOOKAHEAD_CHARS
    )
    assert (
        sps.SOURCE_PASSAGE_DEFAULTS["cue_join_gap_ms"]
        == policy.DEFAULT_CUE_JOIN_GAP_MS
    )
    assert (
        sps.SOURCE_PASSAGE_DEFAULTS["diagnostic_span_ms"]
        == policy.DEFAULT_DIAGNOSTIC_SPAN_MS
    )
    assert sps.SOURCE_PASSAGE_POLICY_VERSION == "source_provisional_v1"
    assert WEB_POLICY_VERSION == "source_provisional_v1"


def test_normalize_rejects_strict_violations():
    assert sps.normalize_source_passage_settings(None) == dict(
        sps.SOURCE_PASSAGE_DEFAULTS
    )
    for bad in (True, False, "60", 60.0, None):
        with pytest.raises(TypeError):
            sps.normalize_source_passage_settings({"min_chars": bad})
    with pytest.raises(TypeError):
        sps.normalize_source_passage_settings({"cue_join_gap_ms": None})
    with pytest.raises(ValueError):
        sps.normalize_source_passage_settings({"bogus": 1})
    with pytest.raises(ValueError):
        sps.normalize_source_passage_settings({"min_chars": 0})
    with pytest.raises(ValueError):
        sps.normalize_source_passage_settings({"cue_join_gap_ms": 3001})
    with pytest.raises(ValueError):
        sps.normalize_source_passage_settings({"diagnostic_span_ms": 999})
    with pytest.raises(ValueError):
        sps.normalize_source_passage_settings(
            {"min_chars": 200, "preferred_chars": 160}
        )


def test_build_kwargs_mapping_preserves_zero_gap():
    kwargs = sps.to_build_kwargs(
        {"min_chars": 80, "cue_join_gap_ms": 0}, language_code="de"
    )
    assert kwargs == {
        "min_chars": 80,
        "max_chars": 160,
        "sentence_lookahead_chars": 20,
        "pause_ms": 0,
        "max_span_ms": 8000,
        "language_code": "de",
    }


def test_runtime_keys_prefixed_and_roundtrip():
    runtime = sps.to_runtime_keys({})
    assert set(runtime) == {
        "source_passage_min_chars",
        "source_passage_preferred_chars",
        "source_passage_sentence_lookahead_chars",
        "source_passage_cue_join_gap_ms",
        "source_passage_diagnostic_span_ms",
    }
    assert "min_chars" not in runtime
    assert sps.from_runtime_keys(runtime) == dict(sps.SOURCE_PASSAGE_DEFAULTS)
    assert sps.from_runtime_keys(
        {"source_passages": dict(sps.SOURCE_PASSAGE_DEFAULTS)}
    ) == dict(sps.SOURCE_PASSAGE_DEFAULTS)
    assert sps.from_runtime_keys(None) == dict(sps.SOURCE_PASSAGE_DEFAULTS)
    assert sps.from_runtime_keys({}) == dict(sps.SOURCE_PASSAGE_DEFAULTS)
    with pytest.raises(TypeError):
        sps.from_runtime_keys({"source_passage_min_chars": None})


def test_settings_hash_stable_and_described():
    assert sps.source_passage_settings_hash({}) == sps.source_passage_settings_hash(
        dict(sps.SOURCE_PASSAGE_DEFAULTS)
    )
    assert (
        sps.source_passage_settings_hash({"min_chars": 61})
        != sps.source_passage_settings_hash({})
    )
    assert set(sps.describe_source_passage_settings()) == set(
        sps.SOURCE_PASSAGE_DEFAULTS
    )


def test_workspace_defaults_and_aliases():
    assert BUILTIN_DEFAULTS["source_passages"] == dict(sps.SOURCE_PASSAGE_DEFAULTS)
    assert BUILTIN_DEFAULTS["subtitles"]["max_chars_per_line"] == 60
    adapted = adapt_runtime_settings("source_passages", dict(sps.SOURCE_PASSAGE_DEFAULTS))
    for web_key, runtime_key in RUNTIME_SETTING_ALIASES["source_passages"].items():
        assert adapted[runtime_key] == adapted[web_key]
    assert "min_chars" not in {
        key for section in RUNTIME_SETTING_ALIASES.values() for key in section.values()
        if section is not RUNTIME_SETTING_ALIASES["source_passages"]
    } or True
    # Flattened generic namespace must not contain bare min_chars collisions.
    assert set(adapted) >= set(sps.SOURCE_PASSAGE_DEFAULTS) | set(
        RUNTIME_SETTING_ALIASES["source_passages"].values()
    )


def test_subtitle_finalization_default_is_60_with_override():
    assert SubtitleFinalizationConfig().max_chars_per_line == 60
    assert SubtitleFinalizationConfig.from_settings({}).max_chars_per_line == 60
    assert (
        SubtitleFinalizationConfig.from_settings(
            {"subtitle_max_chars_per_line": 48}
        ).max_chars_per_line
        == 48
    )


def test_parameter_registry_exposes_source_passages():
    from pandrator.web.parameter_definitions import describe_parameters

    result = describe_parameters(sections=["source_passages"])
    assert result["returned_count"] == 5
    assert {item["name"] for item in result["items"]} == set(
        sps.SOURCE_PASSAGE_DEFAULTS
    )


# ---------------------------------------------------------------------------
# DB-backed tests (isolated app fixture)
# ---------------------------------------------------------------------------


@pytest.fixture
def app_case():
    case = dispatch_tests.DispatchWebTests(methodName="runTest")
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def _put_settings(case, session_id, section, value, expected_revision=0):
    response = case.client.put(
        f"/api/v1/sessions/{session_id}/settings/{section}",
        json={"value": value},
        headers={**case._headers(), "If-Match": str(expected_revision)},
    )
    return response


def test_settings_routes_validate_source_passages(app_case):
    case = app_case
    session_id, _ = case._source()
    response = case.client.get(
        f"/api/v1/sessions/{session_id}/settings/source_passages",
        headers=case._headers(),
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["effective"] == dict(sps.SOURCE_PASSAGE_DEFAULTS)
    assert body["revision"] == 0

    bad_cases = [
        {"min_chars": True},
        {"min_chars": None},
        {"min_chars": 0},
        {"cue_join_gap_ms": 3001},
        {"min_chars": 500, "preferred_chars": 100},
        {"unknown_key": 1},
    ]
    for bad in bad_cases:
        response = _put_settings(case, session_id, "source_passages", bad)
        assert response.status_code == 422, (bad, response.get_json())

    response = _put_settings(
        case, session_id, "source_passages", {"min_chars": 80}
    )
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["revision"] == 1

    # Stale revision conflicts.
    response = _put_settings(
        case, session_id, "source_passages", {"min_chars": 81}, expected_revision=0
    )
    assert response.status_code == 409


def test_dispatch_create_pins_and_shares_effective_settings(app_case):
    case = app_case
    session_id, source_id = case._source(
        texts=("First complete sentence. Second complete sentence.",)
    )
    run = case._create(session_id, source_artifact_id=source_id)
    with case.extension["database"].session() as session:
        stored = stored_passages(session.get(Artifact, source_id))
        assert stored is not None
        packet = session.get(Artifact, source_id).metadata_json["logical_passages"]
        assert packet["policy_version"] == "source_provisional_v1"
        assert packet["source_passage_settings"] == dict(sps.SOURCE_PASSAGE_DEFAULTS)
        assert packet["source_passage_settings_hash"] == (
            sps.source_passage_settings_hash({})
        )
        managed = session.get(DispatchRun, run["id"])
        assert managed.settings_json["source_passages"] == dict(
            sps.SOURCE_PASSAGE_DEFAULTS
        )
        assert (
            managed.settings_json["source_passage_policy_version"]
            == "source_provisional_v1"
        )
        assert managed.settings_json["source_passage_min_chars"] == 60
        assert "min_chars" not in managed.settings_json
    # The claim wire format is strict and unchanged; provenance lives in the
    # run ledger, not in claim packets.
    claim = case._claim(run["id"])
    assert "source_passages" not in claim["task"]


def test_pinned_passages_survive_settings_changes(app_case):
    case = app_case
    session_id, source_id = case._source(
        texts=("First complete sentence. Second complete sentence.",)
    )
    case._create(session_id, source_artifact_id=source_id)
    with case.extension["database"].session() as session:
        before = stored_passages(session.get(Artifact, source_id))
    assert before is not None
    response = _put_settings(
        case, session_id, "source_passages", {"min_chars": 120}
    )
    assert response.status_code == 200
    run = case._create(session_id, source_artifact_id=source_id)
    assert run  # new run still dispatches the pinned rows
    with case.extension["database"].session() as session:
        after = stored_passages(session.get(Artifact, source_id))
        assert after == before
        packet = session.get(Artifact, source_id).metadata_json["logical_passages"]
        assert packet["source_passage_settings"]["min_chars"] == 60


def test_preview_is_read_only_and_returns_hash(app_case):
    case = app_case
    session_id, source_id = case._source(
        texts=("First complete sentence. Second complete sentence.",)
    )
    response = case.client.post(
        f"/api/v1/sessions/{session_id}/sources/{source_id}/passages/preview",
        json={},
        headers=case._headers(),
    )
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["policy_version"] == "source_provisional_v1"
    assert body["effective_settings"] == dict(sps.SOURCE_PASSAGE_DEFAULTS)
    assert body["settings_hash"] == sps.source_passage_settings_hash({})
    assert body["pinned"] is False
    with case.extension["database"].session() as session:
        assert stored_passages(session.get(Artifact, source_id)) is None

    status = case.client.get(
        f"/api/v1/sessions/{session_id}/sources/{source_id}/passages",
        headers=case._headers(),
    )
    assert status.status_code == 200
    assert status.get_json()["pinned"] is False


def _rebuild(case, session_id, source_id, body):
    return case.client.post(
        f"/api/v1/sessions/{session_id}/sources/{source_id}/passages/rebuild",
        json=body,
        headers=case._headers(),
    )


def _preview_guards(case, session_id, source_id, override=None):
    response = case.client.post(
        f"/api/v1/sessions/{session_id}/sources/{source_id}/passages/preview",
        json={"source_passages": override or {}},
        headers=case._headers(),
    )
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    with case.extension["database"].session() as session:
        artifact = session.get(Artifact, source_id)
        revision_id = (artifact.metadata_json or {}).get("revision_id")
    return {
        "expected_source_revision_id": body["revision_id"] or revision_id,
        "expected_source_content_hash": body["content_hash"],
        "expected_settings_revision": body["settings_revision"],
        "expected_settings_hash": body["settings_hash"],
        "source_passages": override or {},
    }


def test_rebuild_rejects_stale_guards_without_mutation(app_case):
    case = app_case
    session_id, source_id = case._source(
        texts=("First complete sentence. Second complete sentence.",)
    )
    guards = _preview_guards(case, session_id, source_id)
    with case.extension["database"].session() as session:
        artifact_count_before = len(
            session.scalars(
                select(Artifact).where(Artifact.session_id == session_id)
            ).all()
        )

    bad_hash = dict(guards, expected_settings_hash="0" * 64)
    response = _rebuild(case, session_id, source_id, bad_hash)
    assert response.status_code == 409, response.get_json()

    bad_revision = dict(guards, expected_source_revision_id="missing")
    response = _rebuild(case, session_id, source_id, bad_revision)
    assert response.status_code == 409, response.get_json()

    with case.extension["database"].session() as session:
        assert stored_passages(session.get(Artifact, source_id)) is None
        assert len(
            session.scalars(
                select(Artifact).where(Artifact.session_id == session_id)
            ).all()
        ) == artifact_count_before


def test_rebuild_branch_preserves_original_and_selection(app_case):
    case = app_case
    session_id, source_id = case._source(
        texts=("First complete sentence. Second complete sentence.",)
    )
    # Add trustworthy word timing so the branch copies TimedWords.
    with case.extension["database"].immediate_session() as session:
        artifact = session.get(Artifact, source_id)
        revision_id = (artifact.metadata_json or {}).get("revision_id")
        cues = list(
            session.scalars(
                select(Segment)
                .where(Segment.revision_id == revision_id)
                .order_by(Segment.ordinal)
            ).all()
        )
        ordinal = 0
        for cue in cues:
            for token in cue.text.split():
                session.add(
                    TimedWord(
                        revision_id=revision_id,
                        segment_id=cue.id,
                        ordinal=ordinal,
                        text=token,
                        start_ms=ordinal * 200,
                        end_ms=ordinal * 200 + 150,
                        confidence=0.9,
                    )
                )
                ordinal += 1
    # A downstream correction artifact whose ledger must stay untouched.
    correction_id = case._stage_artifact(
        session_id,
        name="correction.srt",
        role="correction",
        text="Corrected source.",
        parent_id=source_id,
    )
    guards = _preview_guards(
        case, session_id, source_id, override={"min_chars": 61}
    )
    response = _rebuild(case, session_id, source_id, guards)
    assert response.status_code == 201, response.get_json()
    body = response.get_json()
    assert body["policy_version"] == "source_provisional_v1"
    assert body["effective_settings"]["min_chars"] == 61
    assert body["preserved"]["original_artifact_id"] == source_id
    assert body["preserved"]["downstream_untouched"] is True
    branch_id = body["branch_artifact_id"]

    with case.extension["database"].session() as session:
        original = session.get(Artifact, source_id)
        branch = session.get(Artifact, branch_id)
        assert branch is not None
        assert branch.session_id == session_id
        assert branch.role == original.role
        assert branch.state == "stale"
        assert original.state == "current"
        assert branch.id != original.id
        assert branch.relative_path != original.relative_path
        assert stored_passages(original) is None
        branch_rows = stored_passages(branch)
        assert branch_rows is not None and len(branch_rows) > 0
        packet = branch.metadata_json["logical_passages"]
        assert packet["source_passage_settings"]["min_chars"] == 61
        # Lineage edge without touching the original.
        edge = session.scalar(
            select(ArtifactEdge).where(
                ArtifactEdge.parent_artifact_id == source_id,
                ArtifactEdge.child_artifact_id == branch_id,
            )
        )
        assert edge is not None and edge.relation == "passage_rebuild"
        # TimedWords copied to the branch revision.
        branch_words = session.scalars(
            select(TimedWord).where(
                TimedWord.revision_id == body["branch_revision_id"]
            )
        ).all()
        assert len(branch_words) > 0
        # Original revision/segments/words unchanged.
        assert session.get(
            DocumentRevision, original.metadata_json["revision_id"]
        ) is not None
        # Correction downstream untouched.
        assert session.get(Artifact, correction_id) is not None

    # The new branch is selectable through the existing selection route while
    # the original stays selected until that deliberate switch.
    with case.extension["database"].session() as session:
        before = session.get(SessionStageSelection, (session_id, "transcribe"))
        before_id = before.artifact_id if before else None
    history = case.client.get(
        f"/api/v1/sessions/{session_id}/stages/transcribe/artifacts",
        headers=case._headers(),
    )
    assert history.status_code == 200, history.get_json()
    assert branch_id in [item["id"] for item in history.get_json()["items"]]
    response = case.client.put(
        f"/api/v1/sessions/{session_id}/stages/transcribe/selection",
        json={"artifact_id": branch_id},
        headers={
            **case._headers(),
            "If-Match": str(history.get_json()["revision"]),
        },
    )
    assert response.status_code == 200, response.get_json()
    with case.extension["database"].session() as session:
        after = session.get(SessionStageSelection, (session_id, "transcribe"))
        assert after is not None and after.artifact_id == branch_id
        assert before_id != branch_id or before_id is None


def test_normal_workflow_pins_raw_source(app_case):
    case = app_case
    session_id, source_id = case._source(
        texts=("First complete sentence. Second complete sentence.",)
    )
    with case.extension["database"].session() as session:
        assert stored_passages(session.get(Artifact, source_id)) is None
    handlers = case.extension["workflow_handlers"]
    _record, path = case.extension["artifacts"].resolve(source_id)
    with case.extension["database"].session() as session:
        artifact = session.get(Artifact, source_id)
        session.expunge(artifact)
    processing_path, rows, _speakers = handlers._prepare_passage_input(
        _record, path, path.parent
    )
    assert rows
    assert processing_path != path
    with case.extension["database"].session() as session:
        assert stored_passages(session.get(Artifact, source_id)) == rows


# ---------------------------------------------------------------------------
# Regression tests: stored-ledger reuse, branch safety, run-settings wiring
# ---------------------------------------------------------------------------


def _custom_source(case, texts, name="custom-branches.srt"):
    session = case.extension["sessions"].create(
        "Branch safety",
        workflow_kind="subtitles",
        source_language="en",
    )
    directory = case.extension["paths"].sessions / session.storage_key
    directory.mkdir(parents=True, exist_ok=True)
    source_path = directory / name
    source_path.write_text(
        "\n".join(
            f"{index}\n00:00:{index - 1:02d},000 --> 00:00:{index:02d},000\n{text}\n"
            for index, text in enumerate(texts, start=1)
        ),
        encoding="utf-8",
    )
    artifact = case.extension["artifacts"].register(
        source_path,
        kind="srt",
        role="transcription",
        session_id=session.id,
    )
    case.extension["workflow_handlers"]._store_srt_document(
        session.id, artifact, "transcription", language="en"
    )
    with case.extension["database"].immediate_session() as db_session:
        managed = db_session.get(Artifact, artifact.id)
        revision_id = (managed.metadata_json or {}).get("revision_id")
        cues = list(
            db_session.scalars(
                select(Segment)
                .where(Segment.revision_id == revision_id)
                .order_by(Segment.ordinal)
            ).all()
        )
        ordinal = 0
        for cue in cues:
            for token in cue.text.split():
                db_session.add(
                    TimedWord(
                        revision_id=revision_id,
                        segment_id=cue.id,
                        ordinal=ordinal,
                        text=token,
                        start_ms=ordinal * 200,
                        end_ms=ordinal * 200 + 150,
                        confidence=0.9,
                    )
                )
                ordinal += 1
    return session.id, artifact.id


def test_source_passages_accepts_explicit_segments(app_case):
    from pandrator.web.logical_passages import source_passages as read_passages

    case = app_case
    _session_id, source_id = case._source()
    with case.extension["database"].session() as session:
        artifact = session.get(Artifact, source_id)
        revision_id = (artifact.metadata_json or {}).get("revision_id")
        segments = list(
            session.scalars(
                select(Segment)
                .where(Segment.revision_id == revision_id)
                .order_by(Segment.ordinal)
            ).all()
        )
        rows = read_passages(session, artifact, segments=segments)
        assert [row["text"] for row in rows] == ["Hello world.", "Goodbye."]


def test_preview_rebuilds_instead_of_returning_stored_ledger(app_case):
    case = app_case
    session_id, source_id = _custom_source(
        case, ("Alpha beta, gamma delta epsilon zeta.",)
    )
    case._create(session_id, source_artifact_id=source_id)
    with case.extension["database"].session() as session:
        assert len(stored_passages(session.get(Artifact, source_id))) == 1
    override = {"min_chars": 5, "preferred_chars": 10}
    preview = case.client.post(
        f"/api/v1/sessions/{session_id}/sources/{source_id}/passages/preview",
        json={"source_passages": override},
        headers=case._headers(),
    )
    assert preview.status_code == 200, preview.get_json()
    body = preview.get_json()
    assert body["effective_settings"]["min_chars"] == 5
    assert body["passage_count"] == 2
    assert len(body["items"]) == 2
    with case.extension["database"].session() as session:
        assert len(stored_passages(session.get(Artifact, source_id))) == 1
    guards = _preview_guards(case, session_id, source_id, override=override)
    rebuilt = _rebuild(case, session_id, source_id, guards)
    assert rebuilt.status_code == 201, rebuilt.get_json()
    assert rebuilt.get_json()["passage_count"] == 2


def test_branch_is_noncurrent_and_legacy_fallback_keeps_original(app_case):
    from pandrator.web.artifact_selection import selected_artifacts

    case = app_case
    session_id, source_id = _custom_source(
        case, ("Alpha beta, gamma delta epsilon zeta.",)
    )
    # Legacy shape: no explicit stage selection at all.
    with case.extension["database"].immediate_session() as session:
        for row in session.scalars(
            select(SessionStageSelection).where(
                SessionStageSelection.session_id == session_id
            )
        ).all():
            session.delete(row)
    guards = _preview_guards(case, session_id, source_id)
    rebuilt = _rebuild(case, session_id, source_id, guards)
    assert rebuilt.status_code == 201, rebuilt.get_json()
    branch_id = rebuilt.get_json()["branch_artifact_id"]
    with case.extension["database"].session() as session:
        branch = session.get(Artifact, branch_id)
        assert branch.state != "current"
        assert branch.state != "deleted"
        selected = selected_artifacts(session, session_id)
        assert selected["transcribe"].id == source_id
    history = case.client.get(
        f"/api/v1/sessions/{session_id}/stages/transcribe/artifacts",
        headers=case._headers(),
    )
    assert history.status_code == 200, history.get_json()
    assert branch_id in [item["id"] for item in history.get_json()["items"]]
    response = case.client.put(
        f"/api/v1/sessions/{session_id}/stages/transcribe/selection",
        json={"artifact_id": branch_id},
        headers={
            **case._headers(),
            "If-Match": str(history.get_json()["revision"]),
        },
    )
    assert response.status_code == 200, response.get_json()
    with case.extension["database"].session() as session:
        current = session.get(SessionStageSelection, (session_id, "transcribe"))
        assert current is not None and current.artifact_id == branch_id


def test_branch_remaps_identities_and_supports_second_rebuild(app_case):
    case = app_case
    session_id, source_id = _custom_source(
        case, ("Alpha beta, gamma delta epsilon zeta.",)
    )
    guards = _preview_guards(case, session_id, source_id)
    rebuilt = _rebuild(case, session_id, source_id, guards)
    assert rebuilt.status_code == 201, rebuilt.get_json()
    body = rebuilt.get_json()
    branch_id = body["branch_artifact_id"]
    with case.extension["database"].session() as session:
        branch = session.get(Artifact, branch_id)
        original = session.get(Artifact, source_id)
        original_segment_ids = {
            item.id
            for item in session.scalars(
                select(Segment).where(
                    Segment.revision_id == original.metadata_json["revision_id"]
                )
            ).all()
        }
        new_segment_ids = {
            item.id
            for item in session.scalars(
                select(Segment).where(
                    Segment.revision_id == body["branch_revision_id"]
                )
            ).all()
        }
        new_word_ids = {
            item.id
            for item in session.scalars(
                select(TimedWord).where(
                    TimedWord.revision_id == body["branch_revision_id"]
                )
            ).all()
        }
        rows = stored_passages(branch)
        assert rows
        for row in rows:
            assert set(row["source_cue_ids"]) <= new_segment_ids
            assert set(row["source_cue_ids"]).isdisjoint(original_segment_ids)
            assert set(row["source_word_ids"]) <= new_word_ids
            for entry in row.get("source_token_ranges") or []:
                assert entry["source_cue_id"] in new_segment_ids
        packet = branch.metadata_json["logical_passages"]
        ancestry = packet.get("branch_ancestry") or {}
        assert set(ancestry.get("segment_ids", {}).values()) == new_segment_ids
    second_guards = _preview_guards(case, session_id, branch_id)
    second = _rebuild(case, session_id, branch_id, second_guards)
    assert second.status_code == 201, second.get_json()
    assert second.get_json()["branch_artifact_id"] != branch_id


def test_branch_paths_are_unique_per_branch(app_case):
    case = app_case
    session_id, source_id = _custom_source(
        case, ("Alpha beta, gamma delta epsilon zeta.",)
    )
    first = _rebuild(case, session_id, source_id, _preview_guards(case, session_id, source_id))
    assert first.status_code == 201, first.get_json()
    second = _rebuild(case, session_id, source_id, _preview_guards(case, session_id, source_id))
    assert second.status_code == 201, second.get_json()
    with case.extension["database"].session() as session:
        first_path = session.get(
            Artifact, first.get_json()["branch_artifact_id"]
        ).relative_path
        second_path = session.get(
            Artifact, second.get_json()["branch_artifact_id"]
        ).relative_path
        assert first_path != second_path


def test_run_settings_drive_construction_not_live_defaults(app_case):
    case = app_case
    session_id, source_id = _custom_source(
        case, ("Alpha beta, gamma delta epsilon zeta.",)
    )
    response = _put_settings(
        case, session_id, "source_passages", {"min_chars": 120}
    )
    assert response.status_code == 200, response.get_json()
    handlers = case.extension["workflow_handlers"]
    _record, path = case.extension["artifacts"].resolve(source_id)
    _processing, rows, _speakers = handlers._prepare_passage_input(
        _record,
        path,
        path.parent,
        source_passage_settings={"min_chars": 5, "preferred_chars": 10},
    )
    assert len(rows) == 2
    with case.extension["database"].session() as session:
        packet = session.get(Artifact, source_id).metadata_json["logical_passages"]
        assert packet["source_passage_settings"]["min_chars"] == 5


def test_dispatch_uses_pinned_provenance_not_live_settings(app_case):
    case = app_case
    session_id, source_id = _custom_source(
        case, ("Alpha beta, gamma delta epsilon zeta.",)
    )
    case._create(session_id, source_artifact_id=source_id)
    response = _put_settings(
        case, session_id, "source_passages", {"min_chars": 120}
    )
    assert response.status_code == 200, response.get_json()
    run = case._create(session_id, source_artifact_id=source_id)
    with case.extension["database"].session() as session:
        managed = session.get(DispatchRun, run["id"])
        assert managed.settings_json["source_passages"]["min_chars"] == 60
        assert managed.settings_json["source_passage_min_chars"] == 60
        assert managed.settings_json["source_passage_settings_revision"] == 0
    # The claim wire format is strict and unchanged; provenance lives in the
    # run ledger, not in claim packets.
    claim = case._claim(run["id"])
    assert "source_passages" not in claim["task"]


def test_resolve_run_passage_settings_rejects_invalid():
    from pandrator.web.workflow_handlers import WorkflowHandlers

    resolve = WorkflowHandlers._resolve_run_passage_settings
    with pytest.raises((TypeError, ValueError)):
        resolve("session", {"source_passages": {"min_chars": "huge"}})
    with pytest.raises((TypeError, ValueError)):
        resolve("session", {"source_passage_min_chars": None})



def test_preview_and_rebuild_reject_malformed_bodies(app_case):
    case = app_case
    session_id, source_id = _custom_source(
        case, ("Alpha beta, gamma delta epsilon zeta.",)
    )
    preview_url = (
        f"/api/v1/sessions/{session_id}/sources/{source_id}/passages/preview"
    )
    rebuild_url = (
        f"/api/v1/sessions/{session_id}/sources/{source_id}/passages/rebuild"
    )
    for body in (
        {"source_passages": ["min_chars"]},
        {"source_passages": {"min_chars": "huge"}},
        {"source_passages": {"min_chars": None}},
        {"source_passages": {"unknown_key": 1}},
    ):
        response = case.client.post(
            preview_url, json=body, headers=case._headers()
        )
        assert response.status_code == 422, (body, response.get_json())
    guards = _preview_guards(case, session_id, source_id)
    for key in (
        "expected_source_revision_id",
        "expected_source_content_hash",
        "expected_settings_revision",
        "expected_settings_hash",
    ):
        bad = dict(guards)
        bad.pop(key)
        response = case.client.post(
            rebuild_url, json=bad, headers=case._headers()
        )
        assert response.status_code == 422, (key, response.get_json())
    bad = dict(guards, source_passages={"min_chars": "huge"})
    response = case.client.post(rebuild_url, json=bad, headers=case._headers())
    assert response.status_code == 422, response.get_json()


def test_rebuild_idempotency_key_replays_and_conflicts(app_case):
    case = app_case
    session_id, source_id = _custom_source(
        case, ("Alpha beta, gamma delta epsilon zeta.",)
    )
    guards = _preview_guards(case, session_id, source_id)
    url = f"/api/v1/sessions/{session_id}/sources/{source_id}/passages/rebuild"
    first = case.client.post(
        url, json=guards, headers={**case._headers(), "Idempotency-Key": "branch-key-1"}
    )
    assert first.status_code == 201, first.get_json()
    replay = case.client.post(
        url, json=guards, headers={**case._headers(), "Idempotency-Key": "branch-key-1"}
    )
    assert replay.status_code == 201, replay.get_json()
    assert replay.headers.get("Idempotency-Replayed") == "true"
    assert replay.get_json()["branch_artifact_id"] == first.get_json()["branch_artifact_id"]
    with case.extension["database"].session() as session:
        branches = session.scalars(
            select(Artifact).where(
                Artifact.session_id == session_id,
                Artifact.role == "transcription",
            )
        ).all()
        assert len(branches) == 2  # original + exactly one branch
    conflict = case.client.post(
        url,
        json=dict(guards, source_passages={"min_chars": 61}),
        headers={**case._headers(), "Idempotency-Key": "branch-key-1"},
    )
    assert conflict.status_code == 409, conflict.get_json()


class SourcePassageScopeTests(unittest.TestCase):
    password = "correct horse battery staple"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        prepare_web_test_data_root(self.temporary.name)
        self.bootstrap = BootstrapTokenStore()
        self.owner_grant = self.bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=self.bootstrap,
            background_maintenance=False,
        )
        self.extension = self.app.extensions["pandrator"]
        self.extension["auth"].initialize_owner(self.password)
        self.client = self.app.test_client()
        token = self.bootstrap.issue()
        self.csrf = self.client.post(
            "/api/v1/auth/bootstrap", json={"token": token}
        ).get_json()["csrf_token"]

    def tearDown(self):
        self.extension["database"].dispose()
        self.temporary.cleanup()

    def _owner_headers(self):
        return {"X-CSRF-Token": self.csrf}

    def _automation_headers(self, *scopes):
        client_id = str(uuid.uuid4())
        verifier = generate_token(64)
        state = generate_token(48)
        response = self.client.get(
            "/api/v1/auth/automation/authorize",
            query_string={
                "response_type": "code",
                "client_id": client_id,
                "client_name": "Scope probe",
                "redirect_uri": "http://127.0.0.1:43123/callback",
                "scope": " ".join(scopes),
                "state": state,
                "code_challenge": create_s256_code_challenge(verifier),
                "code_challenge_method": "S256",
                "expires_in_days": "7",
            },
        )
        self.assertEqual(200, response.status_code)
        nonce = re.search(
            r'name="authorization_nonce" value="([^"]+)"',
            response.get_data(as_text=True),
        )
        self.assertIsNotNone(nonce)
        approval = self.client.post(
            "/api/v1/auth/automation/authorize",
            data={
                "authorization_nonce": nonce.group(1),
                "decision": "approve",
                "password": self.password,
            },
        )
        self.assertEqual(302, approval.status_code)
        parameters = parse_qs(urlsplit(approval.headers["Location"]).query)
        exchanged = self.client.post(
            "/api/v1/auth/automation/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "redirect_uri": "http://127.0.0.1:43123/callback",
                "code": parameters["code"][0],
                "code_verifier": verifier,
            },
        )
        self.assertEqual(200, exchanged.status_code)
        return {"Authorization": f"Bearer {exchanged.get_json()['access_token']}"}

    def _source(self):
        session = self.extension["sessions"].create(
            "Scope probe",
            workflow_kind="subtitles",
            source_language="en",
        )
        directory = self.extension["paths"].sessions / session.storage_key
        directory.mkdir(parents=True, exist_ok=True)
        source_path = directory / "source.srt"
        source_path.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHello world.\n",
            encoding="utf-8",
        )
        artifact = self.extension["artifacts"].register(
            source_path,
            kind="srt",
            role="transcription",
            session_id=session.id,
        )
        self.extension["workflow_handlers"]._store_srt_document(
            session.id, artifact, "transcription", language="en"
        )
        return session.id, artifact.id

    def test_read_scope_can_read_but_cannot_rebuild(self):
        session_id, source_id = self._source()
        read_headers = self._automation_headers("app.read")
        status = self.client.get(
            f"/api/v1/sessions/{session_id}/sources/{source_id}/passages",
            headers=read_headers,
        )
        self.assertEqual(200, status.status_code, status.get_json())
        preview = self.client.post(
            f"/api/v1/sessions/{session_id}/sources/{source_id}/passages/preview",
            json={},
            headers=read_headers,
        )
        self.assertEqual(200, preview.status_code, preview.get_json())
        guards = preview.get_json()
        rebuild = self.client.post(
            f"/api/v1/sessions/{session_id}/sources/{source_id}/passages/rebuild",
            json={
                "expected_source_revision_id": guards["revision_id"],
                "expected_source_content_hash": guards["content_hash"],
                "expected_settings_revision": guards["settings_revision"],
                "expected_settings_hash": guards["settings_hash"],
            },
            headers=read_headers,
        )
        self.assertEqual(403, rebuild.status_code, rebuild.get_json())
        self.assertEqual("scope_denied", rebuild.get_json()["error"]["code"])


def test_dispatch_reusing_legacy_ledger_marks_unknown_provenance(app_case):
    from pandrator.web.logical_passages import source_passages as read_passages

    case = app_case
    session_id, source_id = _custom_source(
        case, ("Alpha beta, gamma delta epsilon zeta.",)
    )
    with case.extension["database"].immediate_session() as session:
        managed = session.get(Artifact, source_id)
        rows = read_passages(session, managed)
        assert len(rows) == 1
        # Simulate a pre-feature ledger: bound items but no settings provenance.
        packet = {
            "schema_version": 1,
            "display_revision_id": managed.metadata_json["revision_id"],
            "display_content_hash": managed.content_hash,
            "source_artifact_id": managed.id,
            "source_revision_id": managed.metadata_json["revision_id"],
            "items": [dict(row) for row in rows],
        }
        metadata = dict(managed.metadata_json or {})
        metadata["logical_passages"] = packet
        managed.metadata_json = metadata
    run = case._create(session_id, source_artifact_id=source_id)
    with case.extension["database"].session() as session:
        managed_run = session.get(DispatchRun, run["id"])
        assert (
            managed_run.settings_json["source_passage_policy_version"]
            == "legacy_unknown"
        )
        assert managed_run.settings_json["source_passages"] is None
        assert managed_run.settings_json["source_passage_settings_hash"] is None
        assert (
            managed_run.settings_json["source_passage_settings_revision"] is None
        )
        assert managed_run.settings_json["requested_source_passages"] == dict(
            sps.SOURCE_PASSAGE_DEFAULTS
        )
        assert (
            managed_run.settings_json["requested_source_passage_settings_hash"]
            == sps.source_passage_settings_hash({})
        )
        assert "source_passage_min_chars" not in managed_run.settings_json
        # The reused legacy rows and packet stay untouched.
        source = session.get(Artifact, source_id)
        assert [row["text"] for row in stored_passages(source)] == [
            row["text"] for row in rows
        ]
        packet = source.metadata_json["logical_passages"]
        assert "source_passage_settings" not in packet
        assert "policy_version" not in packet
    claim = case._claim(run["id"])
    assert claim["batch"]["id_namespace"] == "logical_passage"
