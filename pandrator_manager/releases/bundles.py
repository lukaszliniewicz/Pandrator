"""Validation and loading of private-runtime release bundles."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from ..context import WorkspaceLayout
from ..errors import ManagerError
from .models import ReleaseArtifact, ReleaseBundleMetadata

BUNDLE_METADATA_NAME = "pandrator-release.json"
_MAXIMUM_METADATA_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class ValidatedReleaseBundle:
    root: Path
    metadata: ReleaseBundleMetadata
    application_root: Path
    python: Path


def release_cache_path(
    layout: WorkspaceLayout,
    artifact: ReleaseArtifact,
) -> Path:
    target = (
        layout.cache
        / "releases"
        / artifact.sha256
        / artifact.filename
    )
    return layout.require_within(target, roots=(layout.cache,))


def _contained(root: Path, relative: str) -> Path:
    root = root.expanduser().resolve(strict=False)
    candidate = root.joinpath(*relative.split("/")).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ManagerError(
            "invalid_release_bundle",
            "Release bundle metadata points outside the extracted archive.",
            {"path": relative},
            409,
        ) from None
    current = root
    for part in relative.split("/"):
        current = current / part
        if current.is_symlink():
            raise ManagerError(
                "invalid_release_bundle",
                "Release bundle paths may not traverse symbolic links.",
                {"path": relative},
                409,
            )
    return candidate


def validate_release_bundle(
    root: Path,
    *,
    product: str,
    version: str,
) -> ValidatedReleaseBundle:
    selected_root = root.expanduser().resolve(strict=False)
    metadata_path = selected_root / BUNDLE_METADATA_NAME
    try:
        size = metadata_path.stat().st_size
        if size > _MAXIMUM_METADATA_BYTES:
            raise ValueError("metadata exceeds the size limit")
        raw = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata = ReleaseBundleMetadata.model_validate(raw)
    except Exception as error:
        raise ManagerError(
            "invalid_release_bundle",
            "The extracted release bundle metadata is missing or invalid.",
            {"reason": str(error)},
            409,
        ) from error
    if metadata.product != product or metadata.version != version:
        raise ManagerError(
            "invalid_release_bundle",
            "The extracted bundle identity does not match the signed release.",
            {
                "signed_product": product,
                "bundle_product": metadata.product,
                "signed_version": version,
                "bundle_version": metadata.version,
            },
            409,
        )
    application_root = _contained(
        selected_root,
        metadata.application_root,
    )
    python = _contained(selected_root, metadata.python)
    if not application_root.is_dir():
        raise ManagerError(
            "invalid_release_bundle",
            "The release application root is missing.",
            {"path": metadata.application_root},
            409,
        )
    if not python.is_file():
        raise ManagerError(
            "invalid_release_bundle",
            "The release private Python runtime is missing.",
            {"path": metadata.python},
            409,
        )
    if os.name != "nt" and not os.access(python, os.X_OK):
        raise ManagerError(
            "invalid_release_bundle",
            "The release private Python runtime is not executable.",
            {"path": metadata.python},
            409,
        )
    return ValidatedReleaseBundle(
        root=selected_root,
        metadata=metadata,
        application_root=application_root,
        python=python,
    )


def _release_location(
    layout: WorkspaceLayout,
    *,
    product: str,
) -> tuple[Path, Path]:
    if product == "pandrator":
        return layout.app_versions, layout.root / "app" / "current.json"
    if product == "pandrator-manager":
        return (
            layout.manager_versions,
            layout.root / "manager" / "current.json",
        )
    raise ValueError(f"Unsupported release product: {product}")


def active_release_bundle(
    layout: WorkspaceLayout,
    *,
    product: str = "pandrator",
) -> ValidatedReleaseBundle | None:
    versions, pointer_path = _release_location(layout, product=product)
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        version = str(pointer["version"])
        active = layout.require_within(
            str(pointer["path"]),
            roots=(versions,),
        )
    except Exception:
        return None
    if not active.is_dir() or not (
        active / BUNDLE_METADATA_NAME
    ).is_file():
        return None
    try:
        return validate_release_bundle(
            active,
            product=product,
            version=version,
        )
    except ManagerError:
        return None


def active_manager_bundle(
    layout: WorkspaceLayout,
) -> ValidatedReleaseBundle | None:
    return active_release_bundle(layout, product="pandrator-manager")
