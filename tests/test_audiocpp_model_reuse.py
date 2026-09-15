"""Reuse must save disk without permitting an install to mutate rollback models."""

import errno
import hashlib
import os
from dataclasses import replace
from pathlib import Path
from unittest import mock

import pytest

from pandrator_manager.components.audiocpp import AudioCppModelPackage
from pandrator_manager.components.audiocpp_reuse import reuse_verified_package


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def package(tmp_path):
    active = tmp_path / "active"
    payloads = (b"first model", b"second model")
    package = AudioCppModelPackage(
        id="fixture", family="fixture", target_directory="Fixture",
        files=("first.gguf", "embeddings/second.bin"),
        sha256=tuple(hashlib.sha256(value).hexdigest() for value in payloads), task="tts",
    )
    for path, value in zip(package.required_paths(active / "models"), payloads, strict=True):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    return active, tmp_path / "staged" / "models", package


def reuse(active, target, package):
    return reuse_verified_package(active, target, package, digest, lambda: None)


def test_verified_payloads_share_storage_but_not_mutable_metadata(package):
    active, target, definition = package
    definition.marker_path(active / "models").write_text('{"old": true}')
    assert reuse(active, target, definition) == "hardlink"
    for source, linked in zip(definition.required_paths(active / "models"),
                              definition.required_paths(target), strict=True):
        assert os.path.samefile(source, linked)
        linked.unlink()
        assert source.is_file()
        assert source.stat().st_nlink == 1
    assert not definition.marker_path(target).exists()


def test_partial_package_is_not_linked_before_downloading(package):
    active, target, definition = package
    definition.required_paths(active / "models")[1].unlink()
    assert reuse(active, target, definition) is None
    assert not target.exists()


def test_corrupt_payload_never_reaches_new_slot(package):
    active, target, definition = package
    definition.required_paths(active / "models")[1].write_bytes(b"corrupted")
    assert reuse(active, target, definition) is None
    assert not target.exists()


def test_new_pinned_digest_requires_a_fresh_install(package):
    active, target, definition = package
    newer = replace(definition, sha256=("0" * 64, definition.sha256[1]))
    assert reuse(active, target, newer) is None
    assert not target.exists()


def test_symlink_cannot_reuse_files_outside_managed_slot(package, tmp_path):
    active, target, definition = package
    source = definition.required_paths(active / "models")[0]
    outside = tmp_path / "outside.gguf"
    source.rename(outside)
    source.symlink_to(outside)
    assert reuse(active, target, definition) is None
    assert not target.exists()


def test_cross_device_fallback_is_a_private_copy(package):
    active, target, definition = package
    with mock.patch("pandrator_manager.components.audiocpp_reuse.os.link",
                    side_effect=OSError(errno.EXDEV, "cross device")):
        assert reuse(active, target, definition) == "copy"
    for source, copied in zip(definition.required_paths(active / "models"),
                             definition.required_paths(target), strict=True):
        assert not os.path.samefile(source, copied)
        assert digest(source) == digest(copied)


def test_unexpected_link_failure_is_not_silently_ignored(package):
    active, target, definition = package
    with mock.patch("pandrator_manager.components.audiocpp_reuse.os.link",
                    side_effect=OSError(errno.ENOSPC, "no space")):
        with pytest.raises(OSError):
            reuse(active, target, definition)


def test_cancelled_reuse_makes_no_changes(package):
    active, target, definition = package
    def cancel():
        raise RuntimeError("cancelled")
    with pytest.raises(RuntimeError, match="cancelled"):
        reuse_verified_package(active, target, definition, digest, cancel)
    assert not target.exists()


def test_first_install_does_not_try_to_reuse(package):
    _, target, definition = package
    assert reuse(None, target, definition) is None
    assert not target.exists()
