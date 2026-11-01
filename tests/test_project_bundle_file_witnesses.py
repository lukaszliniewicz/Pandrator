"""In-process file witnesses shorten publication without weakening fresh checks."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import threading
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from pandrator.web import models as m
from pandrator.web import project_export_bundles as bundle_module
from pandrator.web.access.contracts import AccessDenied, ProjectNotFound
from pandrator.web.accounts.actor_authorization import ActorAuthorizationDenied
from pandrator.web.project_export_bundles import ProjectExportBundleError
from tests.test_project_export_bundles import (
    _claim_bundle_job,
    _complete_exports,
    _generate,
)
from tests.test_project_export_bundles import (
    bundle_case as bundle_case,
)


def _requested(case):
    operation = _complete_exports(case)
    manifest = case.bundles.manifest(operation["id"], case.principal, case.target)
    request, status = case.bundles.request_bundle(
        operation["id"],
        manifest["manifest_digest"],
        principal=case.principal,
        target_identity=case.target,
        idempotency_key="file-witness-bundle",
    )
    assert status == 202
    _job_id, payload = _claim_bundle_job(case, request["job_id"])
    return payload


def _output(case, payload):
    return (
        case.services.paths.artifacts
        / "project-exports"
        / payload["project_id"]
        / payload["input_digest"]
    )


def _observe_writer(case, monkeypatch):
    observed = SimpleNamespace(active=False, calls=[], outside=[], versions=[], holds=0)
    original = case.database.immediate_session

    @contextmanager
    def writer(*args, **kwargs):
        with original(*args, **kwargs) as db:
            observed.active = True
            observed.holds += 1
            try:
                yield db
            finally:
                observed.active = False

    monkeypatch.setattr(case.database, "immediate_session", writer)
    for owner, name in (
        (case.bundles, "_hash_file"),
        (case.bundles, "_verify_published_directory"),
        (case.services.artifacts, "prepare_registration"),
    ):
        implementation = getattr(owner, name)

        def tracked(*args, _name=name, _implementation=implementation, **kwargs):
            if observed.active:
                observed.calls.append(_name)
            else:
                observed.outside.append(_name)
            return _implementation(*args, **kwargs)

        monkeypatch.setattr(owner, name, tracked)
    original_version = case.bundles._file_version

    def version(relative_path):
        if observed.active:
            observed.versions.append(relative_path)
        return original_version(relative_path)

    monkeypatch.setattr(case.bundles, "_file_version", version)
    return observed


def _after_preparation(case, monkeypatch, effect):
    original = case.bundles._prepare_publication
    observed = []

    def prepared(directory, *args, **kwargs):
        proof = original(directory, *args, **kwargs)
        assert proof is not None
        observed.append(proof)
        effect(case.services.paths.root / proof.relative_directory)
        return proof

    monkeypatch.setattr(case.bundles, "_prepare_publication", prepared)
    return observed


def _assert_no_output(case, payload):
    assert not _output(case, payload).exists()
    assert not list(_output(case, payload).parent.glob(".bundle-stage-*"))
    with case.database.snapshot_session() as db:
        assert not list(
            db.scalars(
                select(m.Artifact).where(
                    m.Artifact.role.in_(("project_export_bundle", "project_export_manifest"))
                )
            )
        )


def test_unchanged_writer_uses_versions_and_preserves_exact_pair(bundle_case, monkeypatch):
    case = bundle_case
    payload = _requested(case)
    observed = _observe_writer(case, monkeypatch)
    result = _generate(case, payload, lambda *_: None, threading.Event())
    assert observed.holds == 1
    assert observed.calls == []
    sources = [item for child in payload["languages"] for item in child["files"]]
    assert {item["relative_path"] for item in sources} <= set(observed.versions)
    published = _output(case, payload)
    assert {
        case.services.paths.relative_managed_path(published / name)
        for name in ("bundle.zip", "manifest.json")
    } <= set(observed.versions)
    with zipfile.ZipFile(published / "bundle.zip") as archive:
        assert archive.read("manifest.json") == (published / "manifest.json").read_bytes()
        for item in sources:
            content = archive.read(item["filename"])
            assert len(content) == item["size_bytes"]
            assert hashlib.sha256(content).hexdigest() == item["sha256"]
    with case.database.snapshot_session() as db:
        pair = [
            db.get_one(m.Artifact, result[key]) for key in ("artifact_id", "manifest_artifact_id")
        ]
        for artifact in pair:
            content = case.services.paths.managed_path(artifact.relative_path).read_bytes()
            assert artifact.size_bytes == len(content)
            assert artifact.content_hash == hashlib.sha256(content).hexdigest()
    assert result["publication_receipt"]["source_artifact_ids"] == sorted(
        item["artifact_id"] for item in sources
    )


@pytest.mark.parametrize("change", ["bytes", "metadata"])
def test_changed_source_takes_full_fallback(bundle_case, monkeypatch, change):
    case = bundle_case
    payload = _requested(case)
    source = case.services.paths.managed_path(payload["languages"][0]["files"][0]["relative_path"])
    original = source.read_bytes()

    def mutate(_directory):
        if change == "bytes":
            source.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        else:
            info = source.stat()
            os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))

    _after_preparation(case, monkeypatch, mutate)
    observed = _observe_writer(case, monkeypatch)
    if change == "bytes":
        with pytest.raises(ProjectExportBundleError) as rejected:
            _generate(case, payload, lambda *_: None, threading.Event())
        assert rejected.value.code == "bundle_inputs_changed"
        _assert_no_output(case, payload)
    else:
        result = _generate(case, payload, lambda *_: None, threading.Event())
        assert (
            result["sha256"]
            == hashlib.sha256((_output(case, payload) / "bundle.zip").read_bytes()).hexdigest()
        )
        assert source.read_bytes() == original
    assert "_hash_file" in observed.calls


@pytest.mark.parametrize("filename", ["bundle.zip", "manifest.json"])
def test_changed_publication_bytes_reject_and_clean(bundle_case, monkeypatch, filename):
    case = bundle_case
    payload = _requested(case)

    def mutate(directory):
        path = directory / filename
        content = path.read_bytes()
        path.write_bytes(bytes([content[0] ^ 1]) + content[1:])

    _after_preparation(case, monkeypatch, mutate)
    observed = _observe_writer(case, monkeypatch)
    with pytest.raises(ProjectExportBundleError) as rejected:
        _generate(case, payload, lambda *_: None, threading.Event())
    assert rejected.value.code == "bundle_publication_invalid"
    assert "_verify_published_directory" in observed.calls
    _assert_no_output(case, payload)


@pytest.mark.parametrize("substitution", ["source", "bundle.zip", "manifest.json"])
def test_symlink_substitution_never_reads_foreign_bytes(bundle_case, monkeypatch, substitution):
    case = bundle_case
    payload = _requested(case)
    foreign = case.root / "foreign-file"
    foreign.write_bytes(b"foreign bytes must never be read")
    substituted = []
    original_open = io.open
    original_os_open = os.open

    def mutate(directory):
        path = (
            case.services.paths.managed_path(payload["languages"][0]["files"][0]["relative_path"])
            if substitution == "source"
            else directory / substitution
        )
        path.unlink()
        path.symlink_to(foreign)
        substituted.append(path)
        # Rename moves a staging link to its deterministic publication path.
        forbidden = {foreign, path, _output(case, payload) / path.name}

        def guarded_open(file, *args, **kwargs):
            if isinstance(file, (str, bytes, os.PathLike)):
                assert Path(os.fsdecode(file)) not in forbidden, "followed substituted file"
            return original_open(file, *args, **kwargs)

        def guarded_os_open(file, *args, **kwargs):
            if isinstance(file, (str, bytes, os.PathLike)):
                assert Path(os.fsdecode(file)) not in forbidden, "opened substituted file"
            return original_os_open(file, *args, **kwargs)

        monkeypatch.setattr(io, "open", guarded_open)
        monkeypatch.setattr(os, "open", guarded_os_open)

    _after_preparation(case, monkeypatch, mutate)
    with pytest.raises(ProjectExportBundleError) as rejected:
        _generate(case, payload, lambda *_: None, threading.Event())
    assert substituted
    assert rejected.value.code == (
        "bundle_inputs_changed" if substitution == "source" else "bundle_publication_invalid"
    )
    _assert_no_output(case, payload)


@pytest.mark.parametrize("loss", ["membership", "lease"])
def test_prepared_files_do_not_outlive_final_authority(bundle_case, monkeypatch, loss):
    case = bundle_case
    payload = _requested(case)

    def revoke(_directory):
        with case.database.immediate_session() as db:
            job = db.get_one(m.Job, payload["bundle_job_id"])
            if loss == "lease":
                job.lease_generation += 1
            else:
                membership = db.get_one(
                    m.WorkspaceMembership, (job.workspace_id, case.principal.user_id)
                )
                db.delete(membership)

    proofs = _after_preparation(case, monkeypatch, revoke)
    with pytest.raises(
        (AccessDenied, ProjectNotFound, ProjectExportBundleError, ActorAuthorizationDenied)
    ):
        _generate(case, payload, lambda *_: None, threading.Event())
    assert len(proofs) == 1
    _assert_no_output(case, payload)


def test_identical_publication_replacement_uses_old_path(bundle_case, monkeypatch):
    case = bundle_case
    payload = _requested(case)
    published = _output(case, payload)
    original_prepare = case.bundles._prepare_publication

    def existing_candidate(staging, *args, **kwargs):
        # Model an unregistered publication left by a prior worker process death.
        shutil.copytree(staging, published)
        return original_prepare(published, *args, **kwargs)

    monkeypatch.setattr(case.bundles, "_prepare_publication", existing_candidate)
    old_identity = []

    def replace_directory(directory):
        old_identity.append((directory.stat().st_dev, directory.stat().st_ino))
        replacement = directory.parent / ".identical-replacement"
        shutil.copytree(directory, replacement)
        shutil.rmtree(directory)
        os.replace(replacement, directory)
        assert (directory.stat().st_dev, directory.stat().st_ino) != old_identity[0]

    _after_preparation(case, monkeypatch, replace_directory)
    observed = _observe_writer(case, monkeypatch)
    result = _generate(case, payload, lambda *_: None, threading.Event())
    assert observed.calls.count("_verify_published_directory") == 1
    assert observed.calls.count("prepare_registration") == 2
    assert result["sha256"] == hashlib.sha256((published / "bundle.zip").read_bytes()).hexdigest()
    assert not list(published.parent.glob(".bundle-stage-*"))


def test_absent_publication_proof_uses_old_path(bundle_case, monkeypatch):
    case = bundle_case
    payload = _requested(case)
    monkeypatch.setattr(case.bundles, "_prepare_publication", lambda *_args, **_kwargs: None)
    observed = _observe_writer(case, monkeypatch)
    result = _generate(case, payload, lambda *_: None, threading.Event())
    assert observed.calls.count("_verify_published_directory") == 1
    assert observed.calls.count("prepare_registration") == 2
    assert (
        result["sha256"]
        == hashlib.sha256((_output(case, payload) / "bundle.zip").read_bytes()).hexdigest()
    )


def test_zero_inode_versions_use_full_fallback(bundle_case, monkeypatch):
    case = bundle_case
    payload = _requested(case)
    original_version = bundle_module._stat_version

    def zero_inode(info):
        version = original_version(info)
        return (version[0], 0, *version[2:])

    monkeypatch.setattr(bundle_module, "_stat_version", zero_inode)
    observed = _observe_writer(case, monkeypatch)
    result = _generate(case, payload, lambda *_: None, threading.Event())
    source_count = sum(len(child["files"]) for child in payload["languages"])
    assert observed.holds == 1
    assert observed.calls.count("_hash_file") == source_count
    assert observed.calls.count("_verify_published_directory") == 1
    assert observed.calls.count("prepare_registration") == 2
    assert not {"_verify_published_directory", "prepare_registration"}.intersection(
        observed.outside
    )
    with case.database.snapshot_session() as db:
        for artifact_key, hash_key, size_key in (
            ("artifact_id", "sha256", "size_bytes"),
            ("manifest_artifact_id", "manifest_sha256", "manifest_size_bytes"),
        ):
            artifact = db.get_one(m.Artifact, result[artifact_key])
            content = case.services.paths.managed_path(artifact.relative_path).read_bytes()
            assert artifact.content_hash == result[hash_key] == hashlib.sha256(content).hexdigest()
            assert artifact.size_bytes == result[size_key] == len(content)
