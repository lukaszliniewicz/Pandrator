from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from pandrator.runtime import DataPaths
from pandrator.web.projects.copy_contracts import (
    CopyFileSource,
    ProjectCopyError,
    ProjectCopySnapshot,
)
from pandrator.web.projects.copy_files import (
    StagedProjectFiles,
    cleanup_staged_project_files,
    stage_project_files,
)


def _id() -> str:
    return str(uuid.uuid4())


def _state_for(sources: tuple[CopyFileSource, ...]) -> str:
    state = {
        "schema_version": 1,
        "rows": {
            "artifacts": [
                {
                    "id": source.artifact_id,
                    "workspace_id": source.workspace_id,
                    "project_id": source.project_id,
                    "session_id": source.session_id,
                    "relative_path": source.relative_path,
                    "content_hash": source.content_hash,
                    "size_bytes": source.size_bytes,
                }
                for source in sources
            ]
        },
    }
    return json.dumps(
        state,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _snapshot(
    sources: tuple[CopyFileSource, ...],
    *,
    blockers: tuple[tuple[str, str], ...] = (),
    state_json: str | None = None,
) -> ProjectCopySnapshot:
    saved_state = _state_for(sources) if state_json is None else state_json
    return ProjectCopySnapshot(
        source_project_id=_id(),
        destination_workspace_id=_id(),
        name="Frozen source snapshot",
        state_json=saved_state,
        fingerprint=hashlib.sha256(saved_state.encode("utf-8")).hexdigest(),
        files=sources,
        blockers=blockers,
        active_job_count=0,
    )


def _source(
    paths: DataPaths,
    data: bytes,
    *,
    filename: str = "source.dat",
    size_bytes: int | None = None,
    write: bool = True,
) -> CopyFileSource:
    session_id = _id()
    relative_path = f"sessions/{session_id}/artifacts/{filename}"
    source_path = paths.managed_path(relative_path)
    if write:
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(data)
    return CopyFileSource(
        artifact_id=_id(),
        workspace_id=_id(),
        project_id=_id(),
        session_id=session_id,
        relative_path=relative_path,
        content_hash=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data) if size_bytes is None else size_bytes,
    )


def _artifact_ids(sources: tuple[CopyFileSource, ...]) -> dict[str, str]:
    return {source.artifact_id: _id() for source in sources}


def _attempts(paths: DataPaths, copy_id: str) -> list[Path]:
    directory = paths.artifacts / "project-copies" / copy_id
    return sorted(directory.glob("attempt-*")) if directory.is_dir() else []


def _assert_error(
    operation: Callable[[], object], code: str
) -> ProjectCopyError:
    with pytest.raises(ProjectCopyError) as failure:
        operation()
    assert failure.value.code == code
    return failure.value


def test_stage_copies_exact_bytes_with_target_names_hashes_and_private_paths(
    tmp_path: Path,
) -> None:
    paths = DataPaths.from_value(tmp_path / "data").ensure()
    first_data = b"first source bytes\x00"
    second_data = bytes(range(256)) * 9000
    sources = (
        _source(paths, first_data, filename="private-book.pdf"),
        _source(paths, second_data, filename="secret-name.tar.gz"),
        _source(paths, b"odd suffix", filename="name.bad-suffix-with-17chars"),
    )
    before = {
        source.artifact_id: paths.managed_path(source.relative_path).read_bytes()
        for source in sources
    }
    snapshot = _snapshot(sources)
    copy_id = _id()
    target_ids = _artifact_ids(sources)
    progress: list[tuple[int, int]] = []

    staged = stage_project_files(
        paths,
        snapshot,
        copy_id=copy_id,
        artifact_ids=target_ids,
        check=lambda: None,
        progress=lambda copied, total: progress.append((copied, total)),
    )

    assert isinstance(staged, StagedProjectFiles)
    assert staged.snapshot_fingerprint == snapshot.fingerprint
    assert staged.directory_relative_path.startswith(f"artifacts/project-copies/{copy_id}/attempt-")
    staged_directory = paths.managed_path(staged.directory_relative_path)
    if os.name != "nt":
        assert stat.S_IMODE(staged_directory.stat().st_mode) == 0o700
    assert staged.directory_device == staged_directory.stat().st_dev
    assert staged.directory_inode == staged_directory.stat().st_ino
    assert len(staged.files) == len(sources)
    assert progress[0] == (0, sum(len(value) for value in before.values()))
    assert progress[-1] == (sum(len(value) for value in before.values()), progress[0][1])

    suffixes = {sources[0].artifact_id: ".pdf", sources[1].artifact_id: ".gz", sources[2].artifact_id: ".bin"}
    for copied in staged.files:
        assert copied.source_artifact_id in before
        assert copied.artifact_id == target_ids[copied.source_artifact_id]
        assert Path(copied.relative_path).parent == Path(staged.directory_relative_path)
        assert Path(copied.relative_path).name == copied.artifact_id + suffixes[copied.source_artifact_id]
        output = paths.managed_path(copied.relative_path)
        content = output.read_bytes()
        assert content == before[copied.source_artifact_id]
        assert copied.content_hash == hashlib.sha256(content).hexdigest()
        assert copied.size_bytes == len(content)
        if os.name != "nt":
            assert stat.S_IMODE(output.stat().st_mode) == 0o600
        assert copied.source_artifact_id not in copied.relative_path
        assert "private-book" not in copied.relative_path
        assert "secret-name" not in copied.relative_path
    assert {
        source.artifact_id: paths.managed_path(source.relative_path).read_bytes()
        for source in sources
    } == before

    cleanup_staged_project_files(paths, staged)
    assert not staged_directory.exists()


@pytest.mark.parametrize("mismatch", ["hash", "size"])
def test_source_hash_or_size_mismatch_removes_all_partial_outputs(
    tmp_path: Path, mismatch: str
) -> None:
    paths = DataPaths.from_value(tmp_path / mismatch).ensure()
    first = _source(paths, b"first valid file", filename="first.bin")
    actual = b"second valid bytes"
    if mismatch == "hash":
        second = replace(
            _source(paths, actual, filename="second.bin"),
            content_hash=hashlib.sha256(b"different content").hexdigest(),
        )
    else:
        second = replace(
            _source(paths, actual, filename="second.bin"),
            size_bytes=len(actual) + 5,
        )
    sources = (first, second)
    snapshot = _snapshot(sources)
    copy_id = _id()

    failure = _assert_error(
        lambda: stage_project_files(
            paths,
            snapshot,
            copy_id=copy_id,
            artifact_ids=_artifact_ids(sources),
            check=lambda: None,
        ),
        "copy_file_unavailable",
    )

    assert str(failure) == "A source file is unavailable or has changed. Refresh the copy preview."
    assert _attempts(paths, copy_id) == []


@pytest.mark.parametrize("source_kind", ["missing", "directory", "external_symlink"])
def test_unavailable_nonregular_and_escaping_sources_are_rejected(
    tmp_path: Path, source_kind: str
) -> None:
    paths = DataPaths.from_value(tmp_path / source_kind).ensure()
    expected_data = b"expected source"
    source = _source(paths, expected_data, filename="placeholder.bin", write=False)
    source_path = paths.managed_path(source.relative_path)
    if source_kind == "directory":
        source_path.mkdir(parents=True)
    elif source_kind == "external_symlink":
        outside = tmp_path / "outside.bin"
        outside.write_bytes(expected_data)
        source_path.parent.mkdir(parents=True)
        source_path.symlink_to(outside)
    snapshot = _snapshot((source,))
    copy_id = _id()

    failure = _assert_error(
        lambda: stage_project_files(
            paths,
            snapshot,
            copy_id=copy_id,
            artifact_ids=_artifact_ids((source,)),
            check=lambda: None,
        ),
        "copy_file_unavailable",
    )

    assert str(failure) == "A source file is unavailable or has changed. Refresh the copy preview."
    assert _attempts(paths, copy_id) == []


def test_mid_copy_authorization_exception_is_rethrown_and_cleans_attempt(
    tmp_path: Path,
) -> None:
    paths = DataPaths.from_value(tmp_path / "data").ensure()
    source = _source(paths, b"x" * (3 * 1024 * 1024 + 19), filename="large.bin")
    snapshot = _snapshot((source,))
    copy_id = _id()
    sentinel = RuntimeError("authorization changed")
    check_count = 0

    def check() -> None:
        nonlocal check_count
        check_count += 1
        if check_count == 6:
            raise sentinel

    with pytest.raises(RuntimeError) as failure:
        stage_project_files(
            paths,
            snapshot,
            copy_id=copy_id,
            artifact_ids=_artifact_ids((source,)),
            check=check,
        )

    assert failure.value is sentinel
    assert _attempts(paths, copy_id) == []


def test_same_copy_id_attempts_are_exclusive_parallel_and_sequential(
    tmp_path: Path,
) -> None:
    paths = DataPaths.from_value(tmp_path / "data").ensure()
    source = _source(paths, b"parallel-safe" * 200_000, filename="shared.bin")
    snapshot = _snapshot((source,))
    copy_id = _id()
    target_ids = _artifact_ids((source,))
    barrier = Barrier(2)
    lock = threading.Lock()

    def worker(index: int) -> StagedProjectFiles:
        first = True

        def check() -> None:
            nonlocal first
            with lock:
                should_wait = first
                first = False
            if should_wait:
                barrier.wait(timeout=10)

        return stage_project_files(
            paths,
            snapshot,
            copy_id=copy_id,
            artifact_ids=target_ids,
            check=check,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        parallel = tuple(executor.map(worker, range(2)))

    sequential = stage_project_files(
        paths,
        snapshot,
        copy_id=copy_id,
        artifact_ids=target_ids,
        check=lambda: None,
    )
    staged = (*parallel, sequential)
    assert len({item.directory_relative_path for item in staged}) == 3
    assert len(_attempts(paths, copy_id)) == 3
    for item in staged:
        output = paths.managed_path(item.files[0].relative_path)
        assert output.read_bytes() == b"parallel-safe" * 200_000

    cleanup_staged_project_files(paths, parallel[0])
    assert paths.managed_path(parallel[1].directory_relative_path).is_dir()
    assert paths.managed_path(sequential.directory_relative_path).is_dir()
    cleanup_staged_project_files(paths, parallel[1])
    cleanup_staged_project_files(paths, sequential)
    assert _attempts(paths, copy_id) == []


def test_identity_map_readiness_and_canonical_fingerprint_validate_before_writes(
    tmp_path: Path,
) -> None:
    paths = DataPaths.from_value(tmp_path / "data").ensure()
    sources = (
        _source(paths, b"one", filename="one.bin"),
        _source(paths, b"two", filename="two.bin"),
    )
    snapshot = _snapshot(sources)
    targets = _artifact_ids(sources)
    first_id, second_id = (source.artifact_id for source in sources)
    copy_id = _id()
    cases: tuple[tuple[str, ProjectCopySnapshot, str, dict[str, str]], ...] = (
        ("unready", replace(snapshot, blockers=(("unsupported_state", "domain"),)), copy_id, targets),
        ("fingerprint", replace(snapshot, fingerprint="0" * 64), copy_id, targets),
        ("missing key", snapshot, copy_id, {first_id: targets[first_id]}),
        ("extra key", snapshot, copy_id, {**targets, _id(): _id()}),
        ("duplicate target", snapshot, copy_id, {first_id: targets[first_id], second_id: targets[first_id]}),
        ("source identity reused", snapshot, copy_id, {first_id: first_id, second_id: targets[second_id]}),
        ("bad copy id", snapshot, "not-a-uuid", targets),
        ("bad target id", snapshot, copy_id, {first_id: targets[first_id].upper(), second_id: targets[second_id]}),
    )

    for _case_name, candidate, candidate_copy_id, candidate_targets in cases:
        _assert_error(
            lambda candidate=candidate, candidate_copy_id=candidate_copy_id, candidate_targets=candidate_targets: stage_project_files(
                paths,
                candidate,
                copy_id=candidate_copy_id,
                artifact_ids=candidate_targets,
                check=lambda: None,
            ),
            "copy_graph_invalid",
        )
        assert not (paths.artifacts / "project-copies").exists()

    noncanonical_json = json.dumps(json.loads(snapshot.state_json), sort_keys=True)
    noncanonical = replace(
        snapshot,
        state_json=noncanonical_json,
        fingerprint=hashlib.sha256(noncanonical_json.encode("utf-8")).hexdigest(),
    )
    _assert_error(
        lambda: stage_project_files(
            paths,
            noncanonical,
            copy_id=copy_id,
            artifact_ids=targets,
            check=lambda: None,
        ),
        "copy_graph_invalid",
    )
    assert not (paths.artifacts / "project-copies").exists()


def test_cleanup_is_idempotent_and_refuses_replacement_or_symlink(
    tmp_path: Path,
) -> None:
    paths = DataPaths.from_value(tmp_path / "data").ensure()
    source = _source(paths, b"cleanup bytes", filename="source.bin")
    snapshot = _snapshot((source,))
    copy_id = _id()
    staged = stage_project_files(
        paths,
        snapshot,
        copy_id=copy_id,
        artifact_ids=_artifact_ids((source,)),
        check=lambda: None,
    )
    directory = paths.managed_path(staged.directory_relative_path)
    cleanup_staged_project_files(paths, staged)
    cleanup_staged_project_files(paths, staged)
    assert not directory.exists()
    staged = stage_project_files(
        paths,
        snapshot,
        copy_id=copy_id,
        artifact_ids=_artifact_ids((source,)),
        check=lambda: None,
    )
    directory = paths.managed_path(staged.directory_relative_path)
    retained_directory = directory.with_name(directory.name + "-retained")
    directory.rename(retained_directory)
    try:
        directory.mkdir(mode=0o700)
        replacement_marker = directory / "replacement.txt"
        replacement_marker.write_text("leave this replacement", encoding="utf-8")
        replacement_stat = directory.stat()
        assert (replacement_stat.st_dev, replacement_stat.st_ino) != (
            staged.directory_device,
            staged.directory_inode,
        )
        cleanup_staged_project_files(paths, staged)
        assert replacement_marker.read_text(encoding="utf-8") == "leave this replacement"
    finally:
        shutil.rmtree(retained_directory)

    second = stage_project_files(
        paths,
        snapshot,
        copy_id=copy_id,
        artifact_ids=_artifact_ids((source,)),
        check=lambda: None,
    )
    original_path = paths.managed_path(second.directory_relative_path)
    moved_path = original_path.with_name(original_path.name + "-held")
    external = tmp_path / "external"
    external.mkdir()
    external_marker = external / "preserve.txt"
    external_marker.write_text("do not follow", encoding="utf-8")
    original_path.rename(moved_path)
    original_path.symlink_to(external, target_is_directory=True)

    cleanup_staged_project_files(paths, second)

    assert original_path.is_symlink()
    assert external_marker.read_text(encoding="utf-8") == "do not follow"
    assert paths.managed_path(paths.relative_managed_path(moved_path)).is_dir()
    original_path.unlink()
    shutil.rmtree(moved_path)
    shutil.rmtree(directory)


def test_destination_io_errors_use_a_fixed_message(tmp_path: Path) -> None:
    paths = DataPaths.from_value(tmp_path / "data").ensure()
    source = _source(paths, b"destination failure", filename="source.bin")
    snapshot = _snapshot((source,))
    copy_id = _id()
    project_copy_root = paths.artifacts / "project-copies"
    project_copy_root.mkdir(parents=True)
    (project_copy_root / copy_id).write_text("not a directory", encoding="utf-8")

    failure = _assert_error(
        lambda: stage_project_files(
            paths,
            snapshot,
            copy_id=copy_id,
            artifact_ids=_artifact_ids((source,)),
            check=lambda: None,
        ),
        "copy_staging_failed",
    )

    assert str(failure) == "Project files could not be prepared for copying."
