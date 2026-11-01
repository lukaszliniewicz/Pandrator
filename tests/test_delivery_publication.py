from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.delivery_publication import publish_delivery_artifact
from pandrator.web.models import Artifact, ArtifactEdge, SessionRecord
from pandrator.web.projects.identity import create_session_project
from tests.web_test_support import prepare_web_test_data_root


@dataclass
class PublicationCase:
    database: Database
    paths: DataPaths
    artifacts: ArtifactService
    session_id: str
    parent_id: str
    rendered: Path
    requested_destination: Path


@pytest.fixture
def publication_case(tmp_path: Path):
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    artifacts = ArtifactService(database, paths)
    try:
        with database.session() as session:
            record = SessionRecord(
                name="Synthetic delivery",
                workflow_kind="audiobook",
                source_language="en",
            )
            create_session_project(session, record)
            session_id = record.id

        parent_path = paths.uploads / "delivery-parent.txt"
        parent_path.write_bytes(b"synthetic parent source")
        parent = artifacts.register(
            parent_path,
            kind="text",
            role="source",
            session_id=session_id,
        )

        staging = paths.temporary / "delivery-invocation"
        staging.mkdir(parents=True, exist_ok=True)
        rendered = staging / "rendered.wav"
        rendered.write_bytes(b"synthetic rendered audio")
        destination_dir = paths.artifacts / "deliveries"
        destination_dir.mkdir(parents=True, exist_ok=True)
        requested_destination = destination_dir / "delivery.wav"
        yield PublicationCase(
            database=database,
            paths=paths,
            artifacts=artifacts,
            session_id=session_id,
            parent_id=parent.id,
            rendered=rendered,
            requested_destination=requested_destination,
        )
    finally:
        database.dispose()


def _publish(
    case: PublicationCase,
    *,
    rendered: Path | None = None,
    requested_destination: Path | None = None,
    before_io: Callable[[], None] = lambda: None,
    authorization_guard: Callable[[Session], None] = lambda _session: None,
    cancel_event: threading.Event | None = None,
    **kwargs,
) -> Artifact:
    return publish_delivery_artifact(
        case.database,
        case.paths,
        case.artifacts,
        rendered or case.rendered,
        requested_destination or case.requested_destination,
        session_id=case.session_id,
        before_io=before_io,
        authorization_guard=authorization_guard,
        cancel_event=cancel_event or threading.Event(),
        **kwargs,
    )


def _artifact_for_path(case: PublicationCase, path: Path) -> Artifact | None:
    relative_path = case.paths.relative_managed_path(path)
    with case.database.snapshot_session() as session:
        return session.scalar(select(Artifact).where(Artifact.relative_path == relative_path))


def test_success_infers_session_owner_and_writes_lineage(publication_case):
    case = publication_case
    artifact = _publish(
        case,
        parent_ids=[case.parent_id],
        settings={"sample_rate": 24000},
        metadata={"source": "synthetic"},
    )

    destination = case.paths.managed_path(artifact.relative_path)
    assert destination.read_bytes() == b"synthetic rendered audio"
    assert not case.rendered.exists()
    assert artifact.session_id == case.session_id
    with case.database.snapshot_session() as session:
        parent = session.get(Artifact, case.parent_id)
        edge = session.get(ArtifactEdge, (case.parent_id, artifact.id))
        stored = session.get(Artifact, artifact.id)
        assert parent is not None and stored is not None
        assert (stored.workspace_id, stored.project_id) == (
            parent.workspace_id,
            parent.project_id,
        )
        assert edge is not None
        assert edge.relation == "derived_from"
        assert stored.kind == "export"
        assert stored.role == "export"
        assert stored.metadata_json == {"source": "synthetic"}
        assert stored.settings_hash is not None


def test_cancellation_before_io_skips_callbacks_and_preparation(publication_case, monkeypatch):
    case = publication_case
    event = threading.Event()
    event.set()
    before_calls = []

    def unexpected_prepare(*_args, **_kwargs):
        raise AssertionError("preparation must not run after cancellation")

    monkeypatch.setattr(case.artifacts, "prepare_registration", unexpected_prepare)
    with pytest.raises(InterruptedError, match="canceled"):
        _publish(case, before_io=lambda: before_calls.append(True), cancel_event=event)

    assert before_calls == []
    assert case.rendered.read_bytes() == b"synthetic rendered audio"
    assert not case.requested_destination.exists()


def test_symlink_and_identical_paths_are_rejected_before_preparation(publication_case, monkeypatch):
    case = publication_case

    def unexpected_prepare(*_args, **_kwargs):
        raise AssertionError("path validation must precede preparation")

    monkeypatch.setattr(case.artifacts, "prepare_registration", unexpected_prepare)

    rendered_link = case.rendered.with_name("rendered-link.wav")
    rendered_link.symlink_to(case.rendered)
    with pytest.raises(ValueError, match="regular file"):
        _publish(case, rendered=rendered_link)

    linked_target = case.requested_destination.with_name("linked-target.wav")
    linked_target.write_bytes(b"existing managed target")
    requested_link = case.requested_destination.with_name("requested-link.wav")
    requested_link.symlink_to(linked_target)
    with pytest.raises(ValueError, match="must not be a symlink"):
        _publish(case, requested_destination=requested_link)

    with pytest.raises(ValueError, match="must differ"):
        _publish(case, requested_destination=case.rendered)

    assert case.rendered.read_bytes() == b"synthetic rendered audio"
    assert not case.requested_destination.exists()


def test_suffix_mismatch_preserves_existing_files_without_callbacks_or_preparation(
    publication_case, monkeypatch
):
    case = publication_case
    mismatched_destination = case.requested_destination.with_suffix(".mp3")
    existing_bytes = b"previous mp3 delivery"
    mismatched_destination.write_bytes(existing_bytes)
    callback_calls = []

    def unexpected_io(*_args, **_kwargs):
        raise AssertionError("suffix validation must precede publication work")

    monkeypatch.setattr(case.artifacts, "prepare_registration", unexpected_io)
    monkeypatch.setattr(case.artifacts, "next_available_path", unexpected_io)
    monkeypatch.setattr(case.artifacts, "register_in_session", unexpected_io)
    with pytest.raises(ValueError, match="suffixes must match"):
        _publish(
            case,
            requested_destination=mismatched_destination,
            before_io=lambda: callback_calls.append("before_io"),
            authorization_guard=lambda _session: callback_calls.append("authorization"),
        )

    assert callback_calls == []
    assert case.rendered.read_bytes() == b"synthetic rendered audio"
    assert mismatched_destination.read_bytes() == existing_bytes
    assert _artifact_for_path(case, mismatched_destination) is None


def test_missing_destination_parent_is_created_only_after_authorization(
    publication_case, monkeypatch
):
    case = publication_case
    requested = case.paths.artifacts / "exports" / "new" / "nested" / "delivery.wav"
    parent = requested.parent
    original_next_path = case.artifacts.next_available_path
    guard_observations = []

    def authorize(_session):
        guard_observations.append(parent.exists())

    def observe_next_path(path):
        assert parent.is_dir()
        return original_next_path(path)

    monkeypatch.setattr(case.artifacts, "next_available_path", observe_next_path)
    artifact = _publish(case, requested_destination=requested, authorization_guard=authorize)

    assert guard_observations == [False]
    assert parent.is_dir()
    assert case.paths.managed_path(artifact.relative_path).read_bytes() == (
        b"synthetic rendered audio"
    )


def test_authorization_denial_does_not_create_missing_destination_parent(publication_case):
    case = publication_case
    requested = case.paths.artifacts / "exports" / "denied" / "nested" / "delivery.wav"
    parent = requested.parent
    assert not parent.exists()

    def deny(_session):
        raise PermissionError("publication denied")

    with pytest.raises(PermissionError, match="publication denied"):
        _publish(case, requested_destination=requested, authorization_guard=deny)

    assert not parent.exists()
    assert case.rendered.read_bytes() == b"synthetic rendered audio"


def test_before_io_denial_precedes_preparation_and_move(publication_case, monkeypatch):
    case = publication_case
    authorization_calls = []

    def unexpected_prepare(*_args, **_kwargs):
        raise AssertionError("preparation must not run after before_io denial")

    def deny_before_io():
        raise PermissionError("source read denied")

    monkeypatch.setattr(case.artifacts, "prepare_registration", unexpected_prepare)
    with pytest.raises(PermissionError, match="source read denied"):
        _publish(
            case,
            before_io=deny_before_io,
            authorization_guard=lambda _session: authorization_calls.append(True),
        )

    assert authorization_calls == []
    assert case.rendered.read_bytes() == b"synthetic rendered audio"
    assert not case.requested_destination.exists()


def test_late_authorization_denial_happens_after_prepare_before_allocation(
    publication_case, monkeypatch
):
    case = publication_case
    prepared = []
    next_path_calls = []
    original_prepare = case.artifacts.prepare_registration

    def observe_prepare(*args, **kwargs):
        result = original_prepare(*args, **kwargs)
        prepared.append(result)
        return result

    def unexpected_allocation(_path):
        next_path_calls.append(True)
        raise AssertionError("allocation must follow authorization")

    def deny_in_writer(_session):
        assert prepared
        raise PermissionError("publication denied")

    monkeypatch.setattr(case.artifacts, "prepare_registration", observe_prepare)
    monkeypatch.setattr(case.artifacts, "next_available_path", unexpected_allocation)
    with pytest.raises(PermissionError, match="publication denied"):
        _publish(case, authorization_guard=deny_in_writer)

    assert prepared
    assert next_path_calls == []
    assert case.rendered.read_bytes() == b"synthetic rendered audio"
    assert not case.requested_destination.exists()


def test_source_replacement_after_preparation_is_not_moved(publication_case):
    case = publication_case
    replacement_bytes = b"new invocation-owned source"

    def replace_source(_session):
        replacement = case.rendered.with_name("replacement.wav")
        replacement.write_bytes(replacement_bytes)
        os.replace(replacement, case.rendered)

    with pytest.raises(RuntimeError, match="identity changed"):
        _publish(case, authorization_guard=replace_source)

    assert case.rendered.read_bytes() == replacement_bytes
    assert not case.requested_destination.exists()


def test_requested_name_and_deleted_artifact_path_are_preserved(publication_case):
    case = publication_case
    case.requested_destination.write_bytes(b"previous delivery")
    historical = case.artifacts.register(
        case.requested_destination,
        kind="export",
        role="export",
        session_id=case.session_id,
    )
    with case.database.session() as session:
        session.get(Artifact, historical.id).state = "deleted"

    artifact = _publish(case)

    later_destination = case.requested_destination.with_name("delivery-2.wav")
    assert case.requested_destination.read_bytes() == b"previous delivery"
    assert later_destination.read_bytes() == b"synthetic rendered audio"
    assert artifact.relative_path == case.paths.relative_managed_path(later_destination)
    with case.database.snapshot_session() as session:
        stored_history = session.get(Artifact, historical.id)
        assert stored_history is not None
        assert stored_history.state == "deleted"
        assert stored_history.relative_path == case.paths.relative_managed_path(
            case.requested_destination
        )


def test_registration_failure_rolls_back_and_removes_only_owned_output(
    publication_case, monkeypatch
):
    case = publication_case

    def fail_registration(*_args, **_kwargs):
        raise RuntimeError("synthetic registration failure")

    monkeypatch.setattr(case.artifacts, "register_in_session", fail_registration)
    with pytest.raises(RuntimeError, match="registration failure"):
        _publish(case)

    assert not case.requested_destination.exists()
    assert not case.rendered.exists()
    assert _artifact_for_path(case, case.requested_destination) is None


def test_failed_commit_confirmation_preserves_moved_bytes(publication_case, monkeypatch):
    case = publication_case

    def fail_registration(*_args, **_kwargs):
        raise RuntimeError("synthetic registration failure")

    class FailedSnapshot:
        def __enter__(self):
            raise RuntimeError("synthetic snapshot failure")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(case.artifacts, "register_in_session", fail_registration)
    monkeypatch.setattr(case.database, "snapshot_session", lambda: FailedSnapshot())
    with pytest.raises(RuntimeError, match="registration failure"):
        _publish(case)

    assert case.requested_destination.read_bytes() == b"synthetic rendered audio"


def test_failed_cleanup_preserves_replacement_inode(publication_case, monkeypatch):
    case = publication_case
    replacement_bytes = b"another invocation replaced this path"

    def replace_then_fail(_session, path, **_kwargs):
        replacement = path.with_name(path.name + ".replacement")
        replacement.write_bytes(replacement_bytes)
        os.replace(replacement, path)
        raise RuntimeError("synthetic registration failure")

    monkeypatch.setattr(case.artifacts, "register_in_session", replace_then_fail)
    with pytest.raises(RuntimeError, match="registration failure"):
        _publish(case)

    assert case.requested_destination.read_bytes() == replacement_bytes
    assert _artifact_for_path(case, case.requested_destination) is None


def test_cancel_during_registration_rolls_back_and_cleans_owned_bytes(
    publication_case, monkeypatch
):
    case = publication_case
    event = threading.Event()
    original_register = case.artifacts.register_in_session

    def cancel_after_registration(*args, **kwargs):
        artifact = original_register(*args, **kwargs)
        event.set()
        return artifact

    monkeypatch.setattr(case.artifacts, "register_in_session", cancel_after_registration)
    with pytest.raises(InterruptedError, match="canceled"):
        _publish(case, cancel_event=event)

    assert not case.requested_destination.exists()
    assert _artifact_for_path(case, case.requested_destination) is None


def test_commit_followed_by_exception_preserves_registered_bytes(publication_case, monkeypatch):
    case = publication_case
    original_immediate = case.database.immediate_session

    @contextmanager
    def commit_then_raise(*, busy_timeout_ms=None):
        with original_immediate(busy_timeout_ms=busy_timeout_ms) as session:
            yield session
        raise RuntimeError("synthetic exception after commit")

    monkeypatch.setattr(case.database, "immediate_session", commit_then_raise)
    with pytest.raises(RuntimeError, match="after commit"):
        _publish(case)

    artifact = _artifact_for_path(case, case.requested_destination)
    assert artifact is not None
    assert artifact.content_hash
    assert case.requested_destination.read_bytes() == b"synthetic rendered audio"


def test_media_hashing_stays_outside_the_immediate_writer(publication_case, monkeypatch):
    case = publication_case
    from pandrator.web import artifacts as artifacts_module

    original_hash = artifacts_module.sha256_file
    original_immediate = case.database.immediate_session
    writer_open = False
    hash_writer_states = []

    def record_hash(path):
        hash_writer_states.append(writer_open)
        return original_hash(path)

    @contextmanager
    def observe_immediate(*, busy_timeout_ms=None):
        nonlocal writer_open
        with original_immediate(busy_timeout_ms=busy_timeout_ms) as session:
            writer_open = True
            try:
                yield session
            finally:
                writer_open = False

    monkeypatch.setattr(artifacts_module, "sha256_file", record_hash)
    monkeypatch.setattr(case.database, "immediate_session", observe_immediate)
    artifact = _publish(case)

    assert artifact.content_hash
    assert hash_writer_states == [False]
