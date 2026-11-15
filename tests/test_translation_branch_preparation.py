"""Branch file preparation keeps media IO outside the SQLite writer lease."""
from __future__ import annotations

import errno
import os
import threading
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import select

from pandrator.web import artifacts, fork_file_preparation, session_forks, translation_projects
from pandrator.web.fork_file_preparation import _PreparedForkFiles
from pandrator.web.jobs import JobQueue
from pandrator.web.models import (
    ApiIdempotency,
    Artifact,
    Document,
    DocumentRevision,
    MediaEditPlan,
    Segment,
    SessionRecord,
    SessionSetting,
    SubtitleEvidence,
    TranslationProject,
    TranslationProjectBranch,
)
from tests import test_session_fork_assets as asset_cases
from tests import test_translation_projects as project_cases
from tests import test_web_session_forks as fork_cases


@pytest.fixture
def simple_case():
    case = project_cases.TranslationProjectServiceTests(methodName="runTest")
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


@pytest.fixture
def edited_case():
    case = fork_cases.WebSessionForkTests(methodName="runTest")
    case.setUp()
    asset_cases._edited_fixture(case)
    case.forks = case.app.extensions["pandrator"]["session_forks"]
    case.source_id = case.record["id"]
    case.checkpoint = case.correction
    try:
        yield case
    finally:
        case.tearDown()


def _project(case, *, planned=False):
    source_id = getattr(case, "source_id", case.source.id)
    if planned:
        with case.database.session() as db:
            db.add(SessionSetting(session_id=source_id, section="multilingual_setup", value_json={
                "target_languages": ["de", "fr", "it", "es"],
                "generate_voiceover": False,
            }))
    with case.database.snapshot_session() as db:
        revision = db.get(SessionRecord, source_id).revision
    payload = {"checkpoint_artifact_id": case.checkpoint.id, "expected_revision": revision,
               "create_planned_branches": planned}
    url = f"/api/v1/sessions/{source_id}/translation-project"
    headers = {**case.headers, "Idempotency-Key": "prepared-project-key"}
    if planned:
        return url, payload, headers
    response = case.client.post(url, json=payload, headers=headers)
    assert response.status_code == 200, response.get_json()
    project = response.get_json()["project"]
    return (f"/api/v1/translation-projects/{project['id']}/branches",
            {"expected_revision": project["revision"],
             "targets": [{"target_language": value} for value in ("de", "fr", "it", "es")]},
            {**case.headers, "Idempotency-Key": "prepared-branches-key"})


def _without_hard_links(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise OSError(errno.EXDEV, "synthetic: hard links unavailable")

    monkeypatch.setattr(os, "link", unavailable)


def _io_guard(case, monkeypatch):
    state = threading.local()
    original = case.database.immediate_session
    hashes = Counter()

    @contextmanager
    def writer(*args, **kwargs):
        with original(*args, **kwargs) as db:
            state.writer = True
            try:
                yield db
            finally:
                state.writer = False

    def check():
        assert not getattr(state, "writer", False), "File IO inside BEGIN IMMEDIATE"

    hash_file = artifacts.sha256_file

    def hash_checked(path):
        check()
        return hash_file(path)

    def preparation_hash(path):
        check()
        hashes[Path(path)] += 1
        return hash_file(path)

    copy_file = fork_file_preparation.shutil.copy2
    share_file = fork_file_preparation.link_or_copy

    def copy_checked(*args, **kwargs):
        check()
        return copy_file(*args, **kwargs)

    def share_checked(*args, **kwargs):
        check()
        return share_file(*args, **kwargs)

    monkeypatch.setattr(case.database, "immediate_session", writer)
    for module in (artifacts, fork_file_preparation, session_forks, translation_projects):
        monkeypatch.setattr(module, "sha256_file", hash_checked)
    monkeypatch.setattr(fork_file_preparation, "sha256_file", preparation_hash)
    monkeypatch.setattr(fork_file_preparation.shutil, "copy2", copy_checked)
    monkeypatch.setattr(fork_file_preparation, "link_or_copy", share_checked)
    for name in ("open", "read_text", "read_bytes"):
        method = getattr(Path, name)

        def guarded(path, *args, _method=method, _name=name, **kwargs):
            mode = args[0] if args and _name == "open" else kwargs.get("mode", "r")
            if path.is_relative_to(case.paths.sessions) and "r" in str(mode):
                check()
            return _method(path, *args, **kwargs)

        monkeypatch.setattr(Path, name, guarded)
    return hashes


@pytest.mark.parametrize("links", [True, False], ids=["hard_links", "copies"])
@pytest.mark.parametrize("planned", [False, True])
def test_edited_four_branch_http_has_no_writer_file_io_and_replays(edited_case, monkeypatch, planned, links):
    case = edited_case
    url, payload, headers = _project(case, planned=planned)
    if not links:
        _without_hard_links(monkeypatch)
    original_paths = {path for path in case.session_dir.rglob("*") if path.is_file()}
    hashes = _io_guard(case, monkeypatch)
    response = case.client.post(url, json=payload, headers=headers)
    assert response.status_code == 200, response.get_json()
    branches = response.get_json()["project"]["branches"]
    assert len(branches) == 4
    assert all(value == 1 for path, value in hashes.items() if path in original_paths)
    assert case.paths.managed_path(case.source.relative_path) in hashes
    with case.database.snapshot_session() as db:
        assert len(list(db.scalars(select(TranslationProjectBranch)))) == 4
        assert len(list(db.scalars(select(ApiIdempotency).where(
            ApiIdempotency.idempotency_key == headers["Idempotency-Key"])))) == 1
        child_paths = []
        originals = {}
        for branch in branches:
            child_id = branch["session_id"]
            assert db.scalar(select(MediaEditPlan).where(MediaEditPlan.session_id == child_id))
            assert len(list(db.scalars(select(SubtitleEvidence).where(SubtitleEvidence.session_id == child_id)))) == 1
            children = list(db.scalars(select(Artifact).where(Artifact.session_id == child_id)))
            assert len([item for item in children if item.role.startswith("media_edit_")]) == 3
            assert not any(item.role in {"assembled_audio", "translation"} for item in children)
            for item in children:
                path = case.paths.managed_path(item.relative_path)
                child_paths.append(path)
                originals[path] = item.content_hash
        assert len(set(child_paths)) == len(child_paths)
        originals_by_inode = {path.stat().st_ino for path in original_paths}
        shared = [path for path in child_paths if path.stat().st_ino in originals_by_inode]
        if links:
            # Every carried file is another name for its source; nothing is copied.
            assert len(shared) == len(child_paths)
        else:
            assert shared == []
            assert len({path.stat().st_ino for path in child_paths}) == len(child_paths)
    with monkeypatch.context() as retry_patch:
        retry_patch.setattr(_PreparedForkFiles, "materialize", lambda self: pytest.fail("replay prepared files"))
        replay = case.client.post(url, json=payload, headers=headers)
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.get_json() == response.get_json()
    assert not list(case.paths.sessions.glob(".fork-preparation-*"))
    # Removing one branch's files leaves the source and the other branches intact.
    first_branch = case.paths.sessions / child_paths[0].relative_to(case.paths.sessions).parts[0]
    for path in first_branch.rglob("*"):
        if path.is_file():
            path.unlink()
    for path, digest in originals.items():
        if not path.is_relative_to(first_branch):
            assert artifacts.sha256_file(path) == digest
    for path in original_paths:
        assert path.is_file()


def test_copy_gate_allows_actual_queue_heartbeat(simple_case, monkeypatch):
    case = simple_case
    url, payload, headers = _project(case)
    queue = JobQueue(case.database)
    queue.enqueue("export.create", session_id=case.source.id)
    job = queue.claim("prepared-branch-worker")
    assert job is not None
    entered = threading.Event()
    release = threading.Event()
    original_copy = fork_file_preparation.link_or_copy
    gated = False

    def gated_copy(*args, **kwargs):
        nonlocal gated
        if not gated:
            gated = True
            entered.set()
            assert release.wait(10), "test did not release copy gate"
        return original_copy(*args, **kwargs)

    monkeypatch.setattr(fork_file_preparation, "link_or_copy", gated_copy)
    result = []
    errors = []

    def request_branches():
        try:
            result.append(case.client.post(url, json=payload, headers=headers))
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=request_branches)
    thread.start()
    try:
        assert entered.wait(10)
        assert queue.heartbeat(job.id, "prepared-branch-worker", lease_generation=job.lease_generation,
                               progress=0.5, detail="copy is still gated")
    finally:
        release.set()
        thread.join(15)
    assert not thread.is_alive()
    assert not errors
    assert result[0].status_code == 200, result[0].get_json()
    assert len(result[0].get_json()["project"]["branches"]) == 4


@pytest.mark.parametrize("change", ["source", "artifact", "revision", "active_revision", "file"])
def test_stale_preparation_conflicts_without_children_or_directories(simple_case, monkeypatch, change):
    case = simple_case
    url, payload, headers = _project(case)
    before = {path.name for path in case.paths.sessions.iterdir()}
    materialize = _PreparedForkFiles.materialize

    def stale(self):
        materialize(self)
        with case.database.immediate_session() as db:
            if change == "source":
                db.get(SessionRecord, case.source.id).revision += 1
            elif change == "artifact":
                db.get(Artifact, case.checkpoint.id).metadata_json = {"language": "fr"}
            elif change in {"revision", "active_revision"}:
                document = Document(session_id=case.source.id, stage="correction", language="en")
                db.add(document)
                db.flush()
                revision = DocumentRevision(document_id=document.id, revision_number=1,
                                            content_hash="changed", reviewed=True)
                db.add(revision)
                db.flush()
                document.active_revision_id = revision.id if change == "revision" else None
                checkpoint = db.get(Artifact, case.checkpoint.id)
                checkpoint.metadata_json = {**checkpoint.metadata_json, "revision_id": revision.id}
            else:
                case.paths.managed_path(case.checkpoint.relative_path).write_text("changed", encoding="utf-8")

    monkeypatch.setattr(_PreparedForkFiles, "materialize", stale)
    response = case.client.post(url, json=payload, headers=headers)
    assert response.status_code == 409, response.get_json()
    assert {path.name for path in case.paths.sessions.iterdir()} == before
    with case.database.snapshot_session() as db:
        assert list(db.scalars(select(TranslationProjectBranch))) == []
        assert len(list(db.scalars(select(SessionRecord)))) == 1
        assert list(db.scalars(select(ApiIdempotency).where(
            ApiIdempotency.idempotency_key == headers["Idempotency-Key"]))) == []


@pytest.mark.parametrize("failure", ["copy", "missing", "corrupt"])
def test_failed_preparation_cleans_every_owned_tree(simple_case, monkeypatch, failure):
    case = simple_case
    url, payload, headers = _project(case)
    before = {path.name for path in case.paths.sessions.iterdir()}
    source_path = case.paths.managed_path(case.checkpoint.relative_path)
    source_bytes = source_path.read_bytes()
    if failure == "copy":
        original_copy = fork_file_preparation.link_or_copy
        count = 0

        def fail_copy(*args, **kwargs):
            nonlocal count
            count += 1
            original_copy(*args, **kwargs)
            if count == 2:
                raise OSError("synthetic disk full")

        monkeypatch.setattr(fork_file_preparation, "link_or_copy", fail_copy)
    else:
        path = case.paths.managed_path(case.checkpoint.relative_path)
        if failure == "missing":
            path.unlink()
        else:
            path.write_bytes(b"corrupted")
    response = case.client.post(url, json=payload, headers=headers)
    assert response.status_code == 409, response.get_json()
    assert {path.name for path in case.paths.sessions.iterdir()} == before
    if failure == "copy":
        # Cleaning up linked staging trees removes names, never the source file.
        assert source_path.read_bytes() == source_bytes
    with case.database.snapshot_session() as db:
        assert list(db.scalars(select(TranslationProjectBranch))) == []
        assert len(list(db.scalars(select(SessionRecord)))) == 1


def test_srt_fallback_is_parsed_before_writer(simple_case, monkeypatch):
    case = simple_case
    url, payload, headers = _project(case)
    _io_guard(case, monkeypatch)
    response = case.client.post(url, json=payload, headers=headers)
    assert response.status_code == 200, response.get_json()
    for branch in response.get_json()["project"]["branches"]:
        with case.database.snapshot_session() as db:
            document = db.scalar(select(Document).where(Document.session_id == branch["session_id"]))
            segment = db.scalar(select(Segment).where(Segment.revision_id == document.active_revision_id))
            assert segment.text == "Hello."
    assert not list(case.paths.sessions.glob(".fork-preparation-*"))


def test_planned_failure_rolls_back_project_and_owned_files(simple_case, monkeypatch):
    case = simple_case
    url, payload, headers = _project(case, planned=True)
    before = {path.name for path in case.paths.sessions.iterdir()}
    original = case.forks.fork_in_session
    count = 0

    def fail_second(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise ValueError("synthetic second branch failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(case.forks, "fork_in_session", fail_second)
    response = case.client.post(url, json=payload, headers=headers)
    assert response.status_code == 422
    assert {path.name for path in case.paths.sessions.iterdir()} == before
    with case.database.snapshot_session() as db:
        assert list(db.scalars(select(TranslationProject))) == []
        assert list(db.scalars(select(TranslationProjectBranch))) == []
        assert list(db.scalars(select(ApiIdempotency))) == []


def test_evidence_closure_includes_references_introduced_by_candidate_revision(edited_case, monkeypatch):
    case = edited_case
    with case.database.session() as db:
        first_evidence = db.scalar(select(SubtitleEvidence).where(SubtitleEvidence.session_id == case.source_id))
        candidate_id = first_evidence.candidates_json[0]["transcript_artifact_id"]
        candidate = db.get(Artifact, candidate_id)
        document = Document(session_id=case.source_id, stage="transcription", language="en")
        db.add(document)
        db.flush()
        revision = DocumentRevision(document_id=document.id, revision_number=1,
                                    content_hash="auxiliary-revision", reviewed=True)
        db.add(revision)
        db.flush()
        segment = Segment(revision_id=revision.id, ordinal=0, node_kind="subtitle_cue",
                          start_ms=0, end_ms=1000, text="Auxiliary", metadata_json={})
        db.add(segment)
        db.flush()
        extra = SubtitleEvidence(
            session_id=case.source_id, source_artifact_id=candidate.id,
            source_revision_id=revision.id, source_segment_id=segment.id,
            cue_id=1, start_ms=0, end_ms=1000, clip_start_ms=0, clip_end_ms=1000,
            reason="Nested evidence", status="completed", candidates_json=[],
        )
        db.add(extra)
        db.flush()
        segment.metadata_json = {"evidence_ids": [extra.id]}
        candidate.metadata_json = {**candidate.metadata_json, "revision_id": revision.id,
                                   "document_id": document.id}
    url, payload, headers = _project(case)
    _io_guard(case, monkeypatch)
    response = case.client.post(url, json=payload, headers=headers)
    assert response.status_code == 200, response.get_json()
    with case.database.snapshot_session() as db:
        for branch in response.get_json()["project"]["branches"]:
            child_id = branch["session_id"]
            evidence = list(db.scalars(select(SubtitleEvidence).where(SubtitleEvidence.session_id == child_id)))
            assert len(evidence) == 2
            copied_extra = next(item for item in evidence if item.reason == "Nested evidence")
            copied_segment = db.get(Segment, copied_extra.source_segment_id)
            assert copied_segment.metadata_json["evidence_ids"] == [copied_extra.id]


def test_concurrent_same_key_keeps_one_receipt_and_four_independent_branches(simple_case, monkeypatch):
    case = simple_case
    url, payload, headers = _project(case)
    before = {path.name for path in case.paths.sessions.iterdir()}
    barrier = threading.Barrier(2)
    materialize = _PreparedForkFiles.materialize

    def concurrent(self):
        materialize(self)
        barrier.wait(timeout=15)

    monkeypatch.setattr(_PreparedForkFiles, "materialize", concurrent)
    responses = []
    errors = []

    def request_branches():
        try:
            responses.append(case.client.post(url, json=payload, headers=headers))
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=request_branches) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors
    assert len(responses) == 2
    assert [response.status_code for response in responses] == [200, 200]
    assert sum(response.headers.get("Idempotency-Replayed") == "true" for response in responses) == 1
    assert responses[0].get_json() == responses[1].get_json()
    assert len({path.name for path in case.paths.sessions.iterdir()} - before) == 4
    assert not list(case.paths.sessions.glob(".fork-preparation-*"))
    with case.database.snapshot_session() as db:
        assert len(list(db.scalars(select(TranslationProjectBranch)))) == 4
        assert len(list(db.scalars(select(ApiIdempotency).where(
            ApiIdempotency.idempotency_key == headers["Idempotency-Key"])))) == 1


def test_empty_srt_retains_existing_success_semantics(simple_case, monkeypatch):
    case = simple_case
    path = case.paths.managed_path(case.checkpoint.relative_path)
    path.write_bytes(b"")
    case.checkpoint = case.artifacts.register(
        path, kind="srt", role="correction", session_id=case.source.id, metadata={"language": "en"},
    )
    url, payload, headers = _project(case)
    _io_guard(case, monkeypatch)
    response = case.client.post(url, json=payload, headers=headers)
    assert response.status_code == 200, response.get_json()
    assert len(response.get_json()["project"]["branches"]) == 4


@pytest.mark.parametrize("moment", ["link", "copy", "after_hash"])
def test_staged_corruption_is_rejected_and_cleaned(simple_case, monkeypatch, moment):
    case = simple_case
    url, payload, headers = _project(case)
    before = {path.name for path in case.paths.sessions.iterdir()}
    if moment != "link":
        _without_hard_links(monkeypatch)
    if moment in {"link", "copy"}:
        original_copy = fork_file_preparation.link_or_copy

        def corrupted_copy(source, destination):
            result = original_copy(source, destination)
            assert result is (moment == "link")
            Path(destination).write_bytes(b"corrupted staged file")
            return result

        monkeypatch.setattr(fork_file_preparation, "link_or_copy", corrupted_copy)
    else:
        original_hash = fork_file_preparation.sha256_file

        def corrupt_after_hash(path):
            digest = original_hash(path)
            if any(parent.name.startswith(".fork-preparation-") for parent in path.parents):
                path.write_bytes(b"changed after hashing")
            return digest

        monkeypatch.setattr(fork_file_preparation, "sha256_file", corrupt_after_hash)
    response = case.client.post(url, json=payload, headers=headers)
    assert response.status_code == 409, response.get_json()
    assert {path.name for path in case.paths.sessions.iterdir()} == before
    with case.database.snapshot_session() as db:
        assert list(db.scalars(select(TranslationProjectBranch))) == []


@pytest.mark.parametrize("moment", ["copy", "second_branch"])
def test_cancellation_cleans_staged_and_installed_files(simple_case, monkeypatch, moment):
    import asyncio

    case = simple_case
    url, payload, headers = _project(case, planned=True)
    before = {path.name for path in case.paths.sessions.iterdir()}
    count = 0
    if moment == "copy":
        original_copy = fork_file_preparation.link_or_copy

        def cancelled_copy(*args, **kwargs):
            original_copy(*args, **kwargs)
            raise asyncio.CancelledError("synthetic cancellation")

        monkeypatch.setattr(fork_file_preparation, "link_or_copy", cancelled_copy)
    else:
        original_fork = case.forks.fork_in_session

        def cancel_second(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise asyncio.CancelledError("synthetic cancellation")
            return original_fork(*args, **kwargs)

        monkeypatch.setattr(case.forks, "fork_in_session", cancel_second)
    with pytest.raises(asyncio.CancelledError):
        case.client.post(url, json=payload, headers=headers)
    assert {path.name for path in case.paths.sessions.iterdir()} == before
    with case.database.snapshot_session() as db:
        assert list(db.scalars(select(TranslationProject))) == []
        assert list(db.scalars(select(TranslationProjectBranch))) == []
        assert list(db.scalars(select(ApiIdempotency))) == []


def _forbid_preparation_io(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid request reached file preparation IO")

    monkeypatch.setattr(_PreparedForkFiles, "materialize", unexpected)
    monkeypatch.setattr(fork_file_preparation.shutil, "copy2", unexpected)
    monkeypatch.setattr(fork_file_preparation, "link_or_copy", unexpected)
    monkeypatch.setattr(os, "link", unexpected)
    for module in (artifacts, fork_file_preparation, session_forks, translation_projects):
        monkeypatch.setattr(module, "sha256_file", unexpected)


def _persisted_ids(case):
    with case.database.snapshot_session() as db:
        return tuple(
            tuple(sorted(db.scalars(select(model.id))))
            for model in (SessionRecord, TranslationProject, TranslationProjectBranch, ApiIdempotency)
        )


@pytest.mark.parametrize("duplicate", ["source", "existing", "requested"])
def test_invalid_branch_target_fails_before_preparation_io(simple_case, monkeypatch, duplicate):
    case = simple_case
    url, payload, headers = _project(case)
    if duplicate == "existing":
        created = case.client.post(url, json={**payload, "targets": [{"target_language": "de"}]},
                                   headers={**headers, "Idempotency-Key": "seed-existing-language"})
        assert created.status_code == 200, created.get_json()
        payload["expected_revision"] = created.get_json()["project"]["revision"]
        payload["targets"] = [{"target_language": "de"}]
    elif duplicate == "source":
        payload["targets"] = [{"target_language": "en"}]
    else:
        payload["targets"] = [{"target_language": "de"}, {"target_language": "DE"}]
    before = {path.name for path in case.paths.sessions.iterdir()}
    before_ids = _persisted_ids(case)
    _forbid_preparation_io(monkeypatch)
    response = case.client.post(url, json=payload, headers=headers)
    assert response.status_code == 409, response.get_json()
    assert {path.name for path in case.paths.sessions.iterdir()} == before
    assert _persisted_ids(case) == before_ids


@pytest.mark.parametrize("invalid", ["title", "missing_setup", "source_target", "existing_project"])
def test_invalid_planned_project_fails_before_preparation_io(simple_case, monkeypatch, invalid):
    case = simple_case
    if invalid == "existing_project":
        _project(case)
    url, payload, headers = _project(case, planned=True)
    with case.database.immediate_session() as db:
        if invalid == "title":
            # The submitted empty name is valid; its generated default exceeds
            # the limit only after consulting the current source name.
            db.get(SessionRecord, case.source.id).name = "x" * 255
        elif invalid == "missing_setup":
            db.delete(db.get(SessionSetting, (case.source.id, "multilingual_setup")))
        elif invalid == "source_target":
            setting = db.get(SessionSetting, (case.source.id, "multilingual_setup"))
            setting.value_json = {**setting.value_json, "target_languages": ["en"]}
    headers["Idempotency-Key"] = f"invalid-planned-{invalid}"
    before = {path.name for path in case.paths.sessions.iterdir()}
    before_ids = _persisted_ids(case)
    _forbid_preparation_io(monkeypatch)
    response = case.client.post(url, json=payload, headers=headers)
    assert response.status_code == (409 if invalid == "existing_project" else 422), response.get_json()
    assert {path.name for path in case.paths.sessions.iterdir()} == before
    assert _persisted_ids(case) == before_ids


def test_link_or_copy_links_when_possible(tmp_path):
    source = tmp_path / "source.wav"
    source.write_bytes(b"immutable media")
    destination = tmp_path / "branch" / "copy.wav"
    destination.parent.mkdir()
    assert artifacts.link_or_copy(source, destination) is True
    assert os.path.samefile(source, destination)
    destination.unlink()
    assert source.read_bytes() == b"immutable media"


@pytest.mark.parametrize("code", [errno.EXDEV, errno.EPERM, errno.EMLINK, errno.EOPNOTSUPP])
def test_link_or_copy_copies_when_links_fail(tmp_path, monkeypatch, code):
    source = tmp_path / "source.wav"
    source.write_bytes(b"immutable media")
    destination = tmp_path / "copy.wav"

    def unavailable(*_args, **_kwargs):
        raise OSError(code, "synthetic link failure")

    monkeypatch.setattr(os, "link", unavailable)
    assert artifacts.link_or_copy(source, destination) is False
    assert destination.read_bytes() == b"immutable media"
    assert not os.path.samefile(source, destination)


def test_link_or_copy_never_replaces_an_existing_file(tmp_path):
    source = tmp_path / "source.wav"
    source.write_bytes(b"new")
    destination = tmp_path / "existing.wav"
    destination.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        artifacts.link_or_copy(source, destination)
    assert destination.read_bytes() == b"existing"


def test_links_reporting_their_own_inode_are_hash_verified(simple_case, monkeypatch):
    # CIFS with noserverino and some FUSE mounts give each name its own inode.
    case = simple_case
    url, payload, headers = _project(case)
    copy_file = fork_file_preparation.shutil.copy2
    hashed = []
    hash_file = fork_file_preparation.sha256_file

    def link_with_own_inode(source, destination):
        copy_file(source, destination)
        return True

    def counted(path):
        hashed.append(Path(path))
        return hash_file(path)

    monkeypatch.setattr(fork_file_preparation, "link_or_copy", link_with_own_inode)
    monkeypatch.setattr(fork_file_preparation, "sha256_file", counted)
    response = case.client.post(url, json=payload, headers=headers)
    assert response.status_code == 200, response.get_json()
    assert len(response.get_json()["project"]["branches"]) == 4
    assert sum(".fork-preparation-" in str(path) for path in hashed) == 4


def test_subtitle_review_witness_ignores_new_links(tmp_path):
    from pandrator.web.subtitle_review import _stat_witness

    source = tmp_path / "checkpoint.srt"
    source.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello.\n", encoding="utf-8")
    before = _stat_witness(source)
    os.link(source, tmp_path / "branch.srt")
    assert _stat_witness(source) == before
