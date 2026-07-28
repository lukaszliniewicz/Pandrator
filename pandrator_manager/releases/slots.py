"""Side-by-side release activation with database and pointer rollback."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from ..context import WorkspaceLayout
from ..state import ManagerStore
from .trust import VerifiedReleaseManifest


class ReleaseActivationError(RuntimeError):
    pass


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _snapshot_sqlite(source: Path, destination: Path) -> bool:
    if not source.is_file():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source)) as incoming, closing(
        sqlite3.connect(destination)
    ) as backup:
        incoming.backup(backup)
    return True


def _restore_sqlite(snapshot: Path, destination: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        target = Path(f"{destination}{suffix}")
        try:
            target.unlink()
        except FileNotFoundError:
            pass
    if snapshot.is_file():
        shutil.copy2(snapshot, destination)


class ReleaseSlotManager:
    def __init__(self, layout: WorkspaceLayout, store: ManagerStore) -> None:
        self.layout = layout
        self.store = store

    def _target(self, product: str) -> tuple[Path, Path]:
        if product == "pandrator":
            return self.layout.app_versions, self.layout.root / "app" / "current.json"
        if product == "pandrator-manager":
            return (
                self.layout.manager_versions,
                self.layout.root / "manager" / "current.json",
            )
        raise ValueError(f"Unsupported release product: {product}")

    def activation_journal(
        self,
        operation_id: str,
        product: str,
    ) -> Path:
        if product not in {"pandrator", "pandrator-manager"}:
            raise ValueError(f"Unsupported release product: {product}")
        path = (
            self.layout.staging
            / operation_id
            / "release"
            / f"{product}-activation.json"
        )
        return self.layout.require_within(
            path,
            roots=(self.layout.staging,),
        )

    def prepare_activation(
        self,
        manifest: VerifiedReleaseManifest,
        staged_directory: Path,
        *,
        operation_id: str,
        database: Path | None = None,
    ) -> dict:
        """Move a verified slot and publish its pointer with a crash journal."""

        versions, pointer = self._target(manifest.payload.product)
        staged = self.layout.require_within(
            staged_directory,
            roots=(self.layout.staging,),
        )
        versions.mkdir(parents=True, exist_ok=True)
        destination = versions / manifest.payload.version
        self.layout.require_within(destination, roots=(versions,))
        journal_path = self.activation_journal(
            operation_id,
            manifest.payload.product,
        )
        if journal_path.is_file():
            try:
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                raise ReleaseActivationError(
                    "Release activation journal is invalid."
                ) from error
            expected = {
                "product": manifest.payload.product,
                "version": manifest.payload.version,
                "manifest_digest": manifest.digest,
                "destination": str(destination),
            }
            if not isinstance(journal, dict) or any(
                journal.get(key) != value for key, value in expected.items()
            ):
                raise ReleaseActivationError(
                    "Release activation journal does not match the signed release."
                )
        else:
            if not staged.is_dir():
                raise ReleaseActivationError(
                    "Staged release directory is missing."
                )
            if destination.exists():
                raise ReleaseActivationError(
                    "The release version already has a staged slot."
                )
            try:
                previous = json.loads(pointer.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                previous = None
            backup = (
                self.layout.backups
                / operation_id
                / "release"
                / "database.sqlite3"
            )
            database_snapshotted = (
                _snapshot_sqlite(database, backup)
                if database is not None
                else False
            )
            journal = {
                "product": manifest.payload.product,
                "version": manifest.payload.version,
                "sequence": manifest.payload.sequence,
                "manifest_digest": manifest.digest,
                "destination": str(destination),
                "pointer": str(pointer),
                "previous_pointer": (
                    previous if isinstance(previous, dict) else None
                ),
                "database": str(database) if database is not None else None,
                "database_backup": str(backup),
                "database_snapshotted": database_snapshotted,
                "created_slot": True,
            }
            _atomic_json(journal_path, journal)
        if destination.is_dir():
            if staged.exists():
                raise ReleaseActivationError(
                    "Both release staging and its destination slot exist."
                )
        else:
            if not staged.is_dir():
                raise ReleaseActivationError(
                    "Neither release staging nor its destination slot exists."
                )
            os.replace(staged, destination)
        _atomic_json(
            pointer,
            {
                "product": manifest.payload.product,
                "version": manifest.payload.version,
                "path": str(destination),
                "manifest_digest": manifest.digest,
                "sequence": manifest.payload.sequence,
                "activated_by": operation_id,
            },
        )
        return dict(journal)

    def rollback_activation(
        self,
        *,
        operation_id: str,
        product: str,
        result: dict | None = None,
    ) -> None:
        journal = dict(result or {})
        if not journal:
            path = self.activation_journal(operation_id, product)
            if not path.is_file():
                return
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                raise ReleaseActivationError(
                    "Release activation rollback journal is invalid."
                ) from error
            if not isinstance(loaded, dict):
                raise ReleaseActivationError(
                    "Release activation rollback journal is invalid."
                )
            journal = loaded
        if journal.get("product") != product:
            raise ReleaseActivationError(
                "Release activation rollback journal has another product."
            )
        _, pointer = self._target(product)
        previous = journal.get("previous_pointer")
        if isinstance(previous, dict):
            _atomic_json(pointer, previous)
        else:
            try:
                pointer.unlink()
            except FileNotFoundError:
                pass
        database_value = journal.get("database")
        backup_value = journal.get("database_backup")
        if (
            journal.get("database_snapshotted")
            and isinstance(database_value, str)
            and isinstance(backup_value, str)
        ):
            database = Path(database_value).expanduser().resolve(strict=False)
            backup = self.layout.require_within(
                backup_value,
                roots=(self.layout.backups,),
            )
            _restore_sqlite(backup, database)
        if journal.get("created_slot") and isinstance(
            journal.get("destination"),
            str,
        ):
            versions, _ = self._target(product)
            destination = self.layout.require_within(
                journal["destination"],
                roots=(versions,),
            )
            if destination.exists():
                shutil.rmtree(destination)

    def activate(
        self,
        manifest: VerifiedReleaseManifest,
        staged_directory: Path,
        *,
        health_check: Callable[[Path], None],
        migrate: Callable[[Path], None] | None = None,
        database: Path | None = None,
    ) -> Path:
        operation_id = (
            f"direct-{manifest.payload.product}-{manifest.payload.sequence}"
        )
        journal = self.prepare_activation(
            manifest,
            staged_directory,
            operation_id=operation_id,
            database=database,
        )
        destination = Path(journal["destination"])
        self.store.save_release_slot(
            product=manifest.payload.product,
            version=manifest.payload.version,
            slot_path=destination,
            manifest_digest=manifest.digest,
            active=False,
            healthy=False,
        )
        try:
            if migrate is not None:
                migrate(destination)
            health_check(destination)
        except Exception as error:
            self.rollback_activation(
                operation_id=operation_id,
                product=manifest.payload.product,
                result=journal,
            )
            self.store.save_release_slot(
                product=manifest.payload.product,
                version=manifest.payload.version,
                slot_path=destination,
                manifest_digest=manifest.digest,
                active=False,
                healthy=False,
            )
            raise ReleaseActivationError(
                "Release activation failed and the previous pointer/database "
                "were restored."
            ) from error
        self.store.activate_release_slot(
            product=manifest.payload.product,
            version=manifest.payload.version,
        )
        return destination
