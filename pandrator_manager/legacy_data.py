"""Conservative reconciliation of mutable data embedded in legacy installs.

Legacy Qt-era workspaces mixed source trees, environments, configuration,
databases, models, voices, and outputs.  The manager copies known mutable
paths into the dedicated data root while the application is stopped.  It
never overwrites an existing destination and never deletes a legacy source;
component removal or whole-product uninstall happens only after this task has
completed.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .context import WorkspaceLayout
from .errors import ManagerError

LEGACY_DATA_PATHS: tuple[tuple[str, str], ...] = (
    ("Outputs", "Outputs"),
    ("voices", "voices"),
    ("models", "models"),
    ("rvc_models", "models/rvc"),
    ("sessions", "sessions"),
    ("artifacts", "artifacts"),
    ("uploads", "uploads"),
    ("pandrator_state.sqlite3", "pandrator_state.sqlite3"),
    ("pandrator.sqlite3", "pandrator.sqlite3"),
    ("global_settings.json", "global_settings.json"),
    ("config.json", "config.json"),
    ("secrets.json", "secrets.json"),
    (".flask-secret", ".flask-secret"),
    ("migration-web-v1.json", "migration-web-v1.json"),
)
LEGACY_DATA_TOP_LEVEL_NAMES = frozenset(
    Path(source).parts[0] for source, _destination in LEGACY_DATA_PATHS
)
_SQLITE_NAMES = frozenset(
    {"pandrator_state.sqlite3", "pandrator.sqlite3"}
)


@dataclass(frozen=True, slots=True)
class LegacyDataItem:
    source: Path
    destination: Path
    origin: str
    relative_name: str
    size_bytes: int
    file_count: int
    revision_digest: str


@dataclass(frozen=True, slots=True)
class LegacyDataInventory:
    items: tuple[LegacyDataItem, ...]
    size_bytes: int
    file_count: int
    revision_digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "size_bytes": self.size_bytes,
            "file_count": self.file_count,
            "revision_digest": self.revision_digest,
            "items": [
                {
                    "source": str(item.source),
                    "destination": str(item.destination),
                    "origin": item.origin,
                    "relative_name": item.relative_name,
                    "size_bytes": item.size_bytes,
                    "file_count": item.file_count,
                    "revision_digest": item.revision_digest,
                }
                for item in self.items
            ],
        }


def _is_link_or_junction(path: Path) -> bool:
    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(
        junction is not None and junction()
    )


def _metadata_fingerprint(
    digest: "hashlib._Hash",
    path: Path,
    relative: str,
) -> int:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as error:
        raise ManagerError(
            "legacy_data_inspection_failed",
            "A legacy data file could not be inspected.",
            {
                "path": str(path),
                "error_type": type(error).__name__,
            },
            409,
        ) from error
    digest.update(
        (
            f"{relative}\0{metadata.st_size}\0{metadata.st_mtime_ns}\0"
            f"{metadata.st_ctime_ns}\0{stat_mode(metadata.st_mode)}\n"
        ).encode("utf-8", errors="surrogatepass")
    )
    return metadata.st_size


def stat_mode(value: int) -> int:
    """Retain only portable type and user permission bits in revisions."""

    return value & 0o170700


def _inspect_tree(path: Path) -> tuple[int, int, str]:
    if _is_link_or_junction(path):
        raise ManagerError(
            "unsafe_legacy_data",
            "Legacy data reconciliation refuses symbolic links and junctions.",
            {"path": str(path)},
            409,
        )
    if path.is_file():
        revision = hashlib.sha256()
        size = _metadata_fingerprint(revision, path, path.name)
        return size, 1, revision.hexdigest()
    if not path.is_dir():
        return 0, 0, hashlib.sha256(b"missing").hexdigest()
    total = 0
    files = 0
    revision = hashlib.sha256()
    for directory, names, filenames in os.walk(path, followlinks=False):
        names.sort()
        filenames.sort()
        current = Path(directory)
        for name in (*names, *filenames):
            selected = current / name
            if _is_link_or_junction(selected):
                raise ManagerError(
                    "unsafe_legacy_data",
                    "Legacy data reconciliation refuses symbolic links and junctions.",
                    {"path": str(selected)},
                    409,
                )
        for name in filenames:
            selected = current / name
            relative = selected.relative_to(path).as_posix()
            total += _metadata_fingerprint(
                revision,
                selected,
                relative,
            )
            files += 1
    return total, files, revision.hexdigest()


def legacy_data_inventory(layout: WorkspaceLayout) -> LegacyDataInventory:
    """Discover workspace-local legacy data without changing the host."""

    roots = (
        ("workspace", layout.root),
        ("repository", layout.root / "Pandrator"),
    )
    items: list[LegacyDataItem] = []
    seen_sources: set[Path] = set()
    for origin, source_root in roots:
        for source_name, destination_name in LEGACY_DATA_PATHS:
            source = source_root / source_name
            if not os.path.lexists(source):
                continue
            resolved = source.resolve(strict=False)
            if resolved in seen_sources:
                continue
            if (
                resolved == layout.data.resolve(strict=False)
                or layout.contains(layout.data, resolved)
            ):
                continue
            if not layout.contains(layout.root, resolved):
                raise ManagerError(
                    "unsafe_legacy_data",
                    "A legacy data candidate resolves outside the workspace.",
                    {"path": str(source)},
                    409,
                )
            destination = (
                layout.data / Path(destination_name)
            ).resolve(strict=False)
            if not layout.contains(layout.data, destination):
                raise ManagerError(
                    "unsafe_legacy_data",
                    "A legacy data destination escapes the data root.",
                    {"path": str(destination)},
                    500,
                )
            size_bytes, file_count, revision_digest = _inspect_tree(source)
            items.append(
                LegacyDataItem(
                    source=source,
                    destination=destination,
                    origin=origin,
                    relative_name=source_name,
                    size_bytes=size_bytes,
                    file_count=file_count,
                    revision_digest=revision_digest,
                )
            )
            seen_sources.add(resolved)
    inventory_revision = hashlib.sha256()
    for item in items:
        inventory_revision.update(
            (
                f"{item.source}\0{item.destination}\0{item.size_bytes}\0"
                f"{item.file_count}\0{item.revision_digest}\n"
            ).encode("utf-8", errors="surrogatepass")
        )
    return LegacyDataInventory(
        items=tuple(items),
        size_bytes=sum(item.size_bytes for item in items),
        file_count=sum(item.file_count for item in items),
        revision_digest=inventory_revision.hexdigest(),
    )


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def _same_file(first: Path, second: Path) -> bool:
    try:
        first_stat = first.stat()
        second_stat = second.stat()
    except OSError:
        return False
    return (
        first_stat.st_size == second_stat.st_size
        and _digest(first) == _digest(second)
    )


def _copy_sqlite(source: Path, destination: Path) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.unlink()
        source_uri = source.resolve(strict=True).as_uri() + "?mode=ro"
        # sqlite3.Connection's context manager commits or rolls back but does
        # not close the handle.  Explicit closing is required before the
        # atomic replace on Windows.
        with (
            closing(
                sqlite3.connect(source_uri, uri=True, timeout=30)
            ) as source_db,
            closing(sqlite3.connect(temporary)) as destination_db,
        ):
            source_db.backup(destination_db)
            check = destination_db.execute("PRAGMA quick_check").fetchone()
            if check is None or str(check[0]) != "ok":
                raise sqlite3.DatabaseError(
                    "copied database failed PRAGMA quick_check"
                )
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.name in _SQLITE_NAMES:
        _copy_sqlite(source, destination)
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with (
            source.open("rb") as source_handle,
            os.fdopen(descriptor, "wb") as destination_handle,
        ):
            shutil.copyfileobj(
                source_handle,
                destination_handle,
                length=1024 * 1024,
            )
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
        if not _same_file(source, temporary):
            raise RuntimeError(
                f"Legacy data copy verification failed: {source}"
            )
        os.replace(temporary, destination)
        shutil.copystat(source, destination, follow_symlinks=False)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _conflict_destination(
    layout: WorkspaceLayout,
    item: LegacyDataItem,
    relative: Path,
    source: Path,
) -> Path:
    base = (
        layout.data
        / "legacy-conflicts"
        / item.origin
        / item.relative_name
        / relative
    ).resolve(strict=False)
    if not layout.contains(layout.data, base):
        raise ManagerError(
            "unsafe_legacy_data",
            "A legacy conflict destination escapes the data root.",
            {"path": str(base)},
            500,
        )
    if not base.exists():
        return base
    if base.is_file() and _same_file(source, base):
        return base
    digest = _digest(source)[:12]
    return base.with_name(f"{base.name}.legacy-{digest}")


def reconcile_legacy_data(
    layout: WorkspaceLayout,
    *,
    inventory: LegacyDataInventory | None = None,
) -> dict[str, Any]:
    """Copy known legacy data into ``layout.data`` without overwrites."""

    selected = inventory or legacy_data_inventory(layout)
    layout.data.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    preserved: list[str] = []
    conflicts: list[dict[str, str]] = []
    try:
        for item in selected.items:
            files: list[tuple[Path, Path]] = []
            if item.source.is_file():
                files.append((item.source, Path()))
            else:
                for directory, _names, filenames in os.walk(
                    item.source,
                    followlinks=False,
                ):
                    current = Path(directory)
                    for name in filenames:
                        source = current / name
                        relative = source.relative_to(item.source)
                        files.append((source, relative))
            for source, relative in files:
                if _is_link_or_junction(source):
                    raise ManagerError(
                        "unsafe_legacy_data",
                        "Legacy data reconciliation refuses symbolic links and junctions.",
                        {"path": str(source)},
                        409,
                    )
                destination = (
                    item.destination
                    if item.source.is_file()
                    else item.destination / relative
                )
                destination = destination.resolve(strict=False)
                if not layout.contains(layout.data, destination):
                    raise ManagerError(
                        "unsafe_legacy_data",
                        "A legacy data copy destination escapes the data root.",
                        {"path": str(destination)},
                        500,
                    )
                if destination.exists():
                    if destination.is_file() and _same_file(
                        source,
                        destination,
                    ):
                        preserved.append(str(destination))
                        continue
                    conflict = _conflict_destination(
                        layout,
                        item,
                        relative,
                        source,
                    )
                    if conflict.exists() and _same_file(source, conflict):
                        preserved.append(str(conflict))
                        continue
                    _copy_file(source, conflict)
                    created.append(str(conflict))
                    conflicts.append(
                        {
                            "source": str(source),
                            "existing": str(destination),
                            "preserved_as": str(conflict),
                        }
                    )
                    continue
                _copy_file(source, destination)
                created.append(str(destination))
        return {
            "created": created,
            "preserved": preserved,
            "conflicts": conflicts,
            "source_bytes": selected.size_bytes,
            "source_files": selected.file_count,
            "sources_retained": True,
        }
    except Exception:
        rollback_legacy_data(layout, {"created": created})
        raise


def rollback_legacy_data(
    layout: WorkspaceLayout,
    result: dict[str, Any],
) -> None:
    """Remove only paths created by the current reconciliation attempt."""

    data = layout.data.resolve(strict=False)
    parents: set[Path] = set()
    for raw in reversed(list(result.get("created") or [])):
        path = Path(str(raw)).resolve(strict=False)
        if not layout.contains(data, path):
            raise ManagerError(
                "unsafe_legacy_data",
                "Legacy data rollback encountered a path outside the data root.",
                {"path": str(path)},
                500,
            )
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        parents.update(path.parents)
    for parent in sorted(
        (path for path in parents if path != data and layout.contains(data, path)),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        try:
            parent.rmdir()
        except OSError:
            pass
