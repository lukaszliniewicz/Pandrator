"""Read-only installation diagnostics with typed repair targets."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .autostart import autostart_adapter
from .components import ComponentRegistry
from .context import ManagerContext
from .launcher import (
    external_cleanup_runtime,
    installed_launcher,
    launcher_metadata_path,
    stable_launcher_path,
)
from .models import (
    ComponentState,
    DoctorCheck,
    DoctorReport,
    HealthState,
)
from .planning import Planner
from .releases.bundles import validate_release_bundle
from .state import ManagerStore
from .state.migrations import MIGRATIONS
from .supervisor import ProcessSupervisor

_CA_ENVIRONMENT_KEYS = (
    "PANDRATOR_CA_BUNDLE",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
)
_STALE_TRANSACTION_SECONDS = 24 * 60 * 60
_STALE_OPERATION_SECONDS = 60 * 60


def _check(
    check_id: str,
    category: str,
    status: str,
    message: str,
    *,
    repairable: bool = False,
    repair_target: str | None = None,
    **details: Any,
) -> DoctorCheck:
    return DoctorCheck(
        id=check_id,
        category=category,
        status=status,
        message=message,
        repairable=repairable,
        repair_target=repair_target,
        details=details,
    )


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "missing"
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        return None, f"{type(error).__name__}: {error}"
    if not isinstance(value, dict):
        return None, "JSON root is not an object"
    return value, None


def _sqlite_health(path: Path) -> tuple[str, dict[str, Any]]:
    """Inspect an existing database through SQLite's read-only URI."""

    if not path.is_file():
        return "missing", {"path": str(path)}
    uri = path.resolve(strict=False).as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
            quick_check = [
                str(row[0])
                for row in connection.execute("PRAGMA quick_check").fetchall()
            ]
            user_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
    except (OSError, sqlite3.Error) as error:
        return "error", {
            "path": str(path),
            "error_type": type(error).__name__,
            "message": str(error),
        }
    return (
        ("ok" if quick_check == ["ok"] else "error"),
        {
            "path": str(path),
            "quick_check": quick_check,
            "user_version": user_version,
        },
    )


class ManagerDoctor:
    """Produce one bounded diagnostic snapshot without persisting observations."""

    def __init__(
        self,
        context: ManagerContext,
        store: ManagerStore,
        registry: ComponentRegistry,
        *,
        supervisor: ProcessSupervisor | None = None,
    ) -> None:
        self.context = context
        self.store = store
        self.registry = registry
        self.supervisor = supervisor
        self.planner = Planner(context, registry)

    def inspect(self) -> DoctorReport:
        checks: list[DoctorCheck] = []
        checks.extend(self._workspace_checks())
        checks.extend(self._database_checks())
        checks.extend(self._release_checks())
        checks.extend(self._component_checks())
        checks.extend(self._service_checks())
        checks.extend(self._ownership_checks())
        checks.extend(self._integration_checks())
        checks.extend(self._transaction_checks())
        summary = {
            status: sum(check.status == status for check in checks)
            for status in ("pass", "warning", "error")
        }
        return DoctorReport(
            healthy=summary["error"] == 0,
            checks=tuple(checks),
            summary=summary,
            generated_at=datetime.now(timezone.utc),
        )

    def _workspace_checks(self) -> Iterable[DoctorCheck]:
        layout = self.context.layout
        try:
            usage = shutil.disk_usage(layout.root)
        except OSError as error:
            yield _check(
                "workspace.disk",
                "workspace",
                "error",
                "Workspace disk usage could not be inspected.",
                error_type=type(error).__name__,
            )
        else:
            reserve = 512 * 1024 * 1024
            yield _check(
                "workspace.disk",
                "workspace",
                "pass" if usage.free >= reserve else "warning",
                (
                    "Workspace disk headroom is available."
                    if usage.free >= reserve
                    else "Workspace disk headroom is below the safety reserve."
                ),
                free_bytes=usage.free,
                reserved_bytes=reserve,
            )
        writable = os.access(layout.root, os.W_OK) and os.access(
            layout.state,
            os.W_OK,
        )
        yield _check(
            "workspace.permissions",
            "workspace",
            "pass" if writable else "error",
            (
                "Workspace and manager state are writable by the current user."
                if writable
                else "Workspace or manager state is not writable by the current user."
            ),
            path=str(layout.root),
        )
        projected = layout.services / ("component-" + "x" * 80) / "versions"
        length = len(str(projected))
        status = "warning" if os.name == "nt" and length >= 230 else "pass"
        yield _check(
            "workspace.path_length",
            "workspace",
            status,
            (
                "Workspace is close to the conservative Windows path limit."
                if status == "warning"
                else "Workspace path length is within the conservative budget."
            ),
            projected_length=length,
        )
        for key in _CA_ENVIRONMENT_KEYS:
            value = str(self.context.environment.get(key) or "").strip()
            if not value:
                continue
            candidate = Path(value).expanduser().resolve(strict=False)
            yield _check(
                f"network.ca.{key.lower()}",
                "network",
                "pass" if candidate.is_file() else "error",
                (
                    f"{key} points to an existing CA bundle."
                    if candidate.is_file()
                    else f"{key} points to a missing CA bundle."
                ),
                path=str(candidate),
            )

    def _database_checks(self) -> Iterable[DoctorCheck]:
        layout = self.context.layout
        state, details = _sqlite_health(layout.database)
        schema_version: int | None = None
        if state == "ok":
            try:
                schema_version = self.store.schema_version()
            except (sqlite3.Error, ValueError) as error:
                state = "error"
                details["schema_error"] = str(error)
        expected = MIGRATIONS[-1][0] if MIGRATIONS else 0
        schema_ok = schema_version == expected
        yield _check(
            "database.manager",
            "database",
            "pass" if state == "ok" and schema_ok else "error",
            (
                "Manager state database is healthy and current."
                if state == "ok" and schema_ok
                else "Manager state database is missing, corrupt, or has an unexpected schema."
            ),
            repairable=False,
            schema_version=schema_version,
            expected_schema_version=expected,
            **details,
        )
        app_database = layout.data / "pandrator.sqlite3"
        app_state, app_details = _sqlite_health(app_database)
        if app_state == "missing":
            status = "warning"
            message = "Pandrator application database is not present."
        elif app_state == "ok":
            status = "pass"
            message = "Pandrator application database passed SQLite integrity checks."
        else:
            status = "error"
            message = "Pandrator application database failed SQLite integrity checks."
        yield _check(
            "database.pandrator",
            "database",
            status,
            message,
            repairable=False,
            **app_details,
        )

    def _release_checks(self) -> Iterable[DoctorCheck]:
        layout = self.context.layout
        products = (
            (
                "pandrator",
                layout.app_versions,
                layout.root / "app" / "current.json",
            ),
            (
                "pandrator-manager",
                layout.manager_versions,
                layout.root / "manager" / "current.json",
            ),
        )
        for product, versions, pointer_path in products:
            accepted = self.store.accepted_release(product)
            slots = self.store.release_slots(product)
            pointer, error = _read_json(pointer_path)
            if pointer is None:
                status = "error" if accepted or slots else "warning"
                yield _check(
                    f"release.{product}.pointer",
                    "release",
                    status,
                    (
                        f"The active {product} release pointer is invalid."
                        if status == "error"
                        else f"No managed {product} release is installed."
                    ),
                    repairable=status == "error",
                    repair_target=f"release:{product}",
                    reason=error,
                    path=str(pointer_path),
                )
                continue
            try:
                version = str(pointer["version"])
                slot = layout.require_within(
                    str(pointer["path"]),
                    roots=(versions,),
                )
                bundle = validate_release_bundle(
                    slot,
                    product=product,
                    version=version,
                )
                digest = str(pointer.get("manifest_digest") or "")
                active_slots = [item for item in slots if item["active"]]
                accepted_matches = bool(
                    accepted
                    and accepted["version"] == version
                    and accepted["manifest_digest"] == digest
                )
                slot_matches = bool(
                    len(active_slots) == 1
                    and Path(active_slots[0]["slot_path"]).resolve(strict=False)
                    == bundle.root
                    and active_slots[0]["version"] == version
                    and active_slots[0]["manifest_digest"] == digest
                    and active_slots[0]["healthy"]
                )
                if accepted is None and not slots:
                    # A legacy/imported pointer can be structurally sound while
                    # still lacking signed-release provenance.
                    status = "warning"
                    message = (
                        f"The {product} bundle is valid but has no accepted "
                        "signed release record."
                    )
                elif accepted_matches and slot_matches:
                    status = "pass"
                    message = f"The active {product} release is consistent."
                else:
                    status = "error"
                    message = (
                        f"The {product} pointer, slot, and accepted release "
                        "records disagree."
                    )
                yield _check(
                    f"release.{product}.pointer",
                    "release",
                    status,
                    message,
                    repairable=status != "pass",
                    repair_target=f"release:{product}",
                    version=version,
                    slot_path=str(bundle.root),
                    manifest_digest=digest,
                    accepted_matches=accepted_matches,
                    slot_matches=slot_matches,
                )
            except Exception as release_error:
                yield _check(
                    f"release.{product}.pointer",
                    "release",
                    "error",
                    f"The active {product} release pointer or bundle is invalid.",
                    repairable=True,
                    repair_target=f"release:{product}",
                    path=str(pointer_path),
                    reason=str(release_error),
                )

    def _component_checks(self) -> Iterable[DoctorCheck]:
        stored = self.store.component_records()
        for definition in self.registry.definitions():
            desired = stored.get(definition.id, (None, None))[0]
            try:
                inspection = self.planner.inspect(definition.id, desired)
            except Exception as error:
                yield _check(
                    f"component.{definition.id}",
                    "component",
                    "error",
                    f"{definition.label} could not be inspected.",
                    repairable=True,
                    repair_target=f"component:{definition.id}",
                    error_type=type(error).__name__,
                    reason=str(error),
                )
                continue
            wanted = desired is not None and desired.present
            if wanted and inspection.state != ComponentState.PRESENT:
                status = "error"
                repairable = inspection.state != ComponentState.UNSUPPORTED
            elif not wanted and inspection.state in {
                ComponentState.PRESENT,
                ComponentState.DEGRADED,
            }:
                status = "warning"
                repairable = True
            elif inspection.state in {
                ComponentState.DEGRADED,
                ComponentState.UNKNOWN,
            }:
                status = "warning"
                repairable = True
            else:
                status = "pass"
                repairable = False
            yield _check(
                f"component.{definition.id}",
                "component",
                status,
                (
                    f"{definition.label} matches its recorded desired state."
                    if status == "pass"
                    else f"{definition.label} does not fully match its recorded desired state."
                ),
                repairable=repairable,
                repair_target=(
                    f"component:{definition.id}" if repairable else None
                ),
                desired_present=wanted,
                state=inspection.state.value,
                problems=list(inspection.problems),
                evidence=list(inspection.evidence),
            )

    def _service_checks(self) -> Iterable[DoctorCheck]:
        if self.supervisor is None:
            yield _check(
                "service.supervisor",
                "service",
                "warning",
                "Live supervisor diagnostics are unavailable in offline mode.",
            )
            return
        services = self.supervisor.snapshot()
        if not services:
            yield _check(
                "service.supervisor",
                "service",
                "pass",
                "No managed services are currently registered.",
            )
            return
        for service in services:
            health = service.health
            if service.desired_running and (
                health is None or health.state != HealthState.HEALTHY
            ):
                status = "error"
            elif health is not None and health.state in {
                HealthState.DEGRADED,
                HealthState.UNHEALTHY,
                HealthState.FAILED,
            }:
                status = "warning"
            else:
                status = "pass"
            log_path = (
                self.context.layout.logs
                / "services"
                / f"{service.id}.log"
            )
            yield _check(
                f"service.{service.id}",
                "service",
                status,
                (
                    f"Managed service {service.id} is consistent with its desired state."
                    if status == "pass"
                    else f"Managed service {service.id} is unhealthy or unexpectedly stopped."
                ),
                repairable=status != "pass",
                repair_target=(
                    f"component:{service.component_id}"
                    if status != "pass"
                    else None
                ),
                desired_running=service.desired_running,
                health=(
                    health.model_dump(mode="json") if health is not None else None
                ),
                process=(
                    service.process.model_dump(mode="json")
                    if service.process is not None
                    else None
                ),
                log_available=log_path.is_file(),
            )

    def _ownership_checks(self) -> Iterable[DoctorCheck]:
        layout = self.context.layout
        records = self.store.owned_paths()
        allowed_roots = (
            layout.root / "app",
            layout.root / "manager",
            layout.services,
            layout.environments,
            layout.bin,
            layout.logs,
            layout.cache,
            layout.state,
        )
        if not records:
            yield _check(
                "ownership.manifest",
                "ownership",
                "warning",
                "No manager ownership records have been committed.",
            )
            return
        unsafe: list[str] = []
        missing: list[str] = []
        legacy: list[str] = []
        for record in records:
            selected = Path(str(record["path"])).resolve(strict=False)
            standard = any(
                selected == root.resolve(strict=False)
                or layout.contains(root, selected)
                for root in allowed_roots
            )
            evidence = record.get("evidence")
            markers = (
                evidence.get("markers")
                if isinstance(evidence, dict)
                else None
            )
            legacy_owned = bool(
                record.get("owner_kind")
                in {"legacy_component", "legacy_shared"}
                and selected != layout.root.resolve(strict=False)
                and layout.contains(layout.root, selected)
                and not (
                    selected == layout.data.resolve(strict=False)
                    or layout.contains(layout.data, selected)
                )
                and isinstance(markers, list)
                and markers
            )
            if not standard and not legacy_owned:
                unsafe.append(str(selected))
            elif not selected.exists():
                missing.append(str(selected))
            elif legacy_owned:
                legacy.append(str(selected))
        status = (
            "error"
            if unsafe
            else ("warning" if missing or legacy else "pass")
        )
        yield _check(
            "ownership.manifest",
            "ownership",
            status,
            (
                "Ownership records are contained and present."
                if status == "pass"
                else (
                    "Ownership records contain paths outside manager-owned roots."
                    if unsafe
                    else (
                        "Legacy positively identified paths remain outside the "
                        "versioned manager layout."
                        if legacy
                        else "Some ownership records refer to missing paths."
                    )
                )
            ),
            repairable=bool(missing or legacy) and not unsafe,
            repair_target="ownership",
            record_count=len(records),
            unsafe_paths=unsafe,
            missing_paths=missing,
            legacy_paths=legacy,
        )

    def _integration_checks(self) -> Iterable[DoctorCheck]:
        layout = self.context.layout
        launcher_present = (
            os.path.lexists(stable_launcher_path(layout))
            or os.path.lexists(launcher_metadata_path(layout))
        )
        try:
            launcher = installed_launcher(
                layout,
                strict=launcher_present,
            )
            cleanup = external_cleanup_runtime(layout)
        except Exception as error:
            yield _check(
                "integration.launcher",
                "integration",
                "error",
                "The installed stable launcher is corrupt or redirected.",
                repairable=True,
                repair_target="stable-launcher",
                reason=str(error),
            )
        else:
            if launcher is not None:
                launcher_status = "pass"
                launcher_message = (
                    "The digest-verified stable launcher is available."
                )
            elif cleanup is not None and cleanup.mode == "python":
                launcher_status = "pass"
                launcher_message = (
                    "This PyPI/tool installation has an external cleanup "
                    "Python runtime."
                )
            else:
                launcher_status = "error"
                launcher_message = (
                    "No cleanup runtime can survive removal of the managed "
                    "installation."
                )
            yield _check(
                "integration.launcher",
                "integration",
                launcher_status,
                launcher_message,
                repairable=launcher_status == "error",
                repair_target=(
                    "stable-launcher"
                    if launcher_status == "error"
                    else None
                ),
                mode=cleanup.mode if cleanup is not None else None,
                path=(
                    str(cleanup.executable)
                    if cleanup is not None
                    else None
                ),
            )
        try:
            status = autostart_adapter(layout).status()
        except Exception as error:
            yield _check(
                "integration.autostart",
                "integration",
                "warning",
                "Per-user manager autostart is unavailable on this platform.",
                reason=str(error),
            )
            return
        yield _check(
            "integration.autostart",
            "integration",
            "pass" if status.installed else "warning",
            (
                "Per-user manager autostart is installed."
                if status.installed
                else "Per-user manager autostart is not installed."
            ),
            repairable=not status.installed and status.supported,
            repair_target="autostart",
            supported=status.supported,
            installed=status.installed,
            enabled=status.enabled,
            active=status.active,
            path=status.path,
            platform_message=status.message,
        )

    def _transaction_checks(self) -> Iterable[DoctorCheck]:
        layout = self.context.layout
        now = datetime.now(timezone.utc).timestamp()
        active = self.store.list_operations(active_only=True, limit=500)
        stale_operations = [
            operation.id
            for operation in active
            if now - operation.updated_at.timestamp() > _STALE_OPERATION_SECONDS
        ]
        active_ids = {operation.id for operation in active}
        orphaned: list[str] = []
        for root in (layout.staging, layout.backups):
            try:
                children = tuple(root.iterdir())
            except OSError:
                children = ()
            for child in children:
                try:
                    age = now - child.stat().st_mtime
                except OSError:
                    continue
                if child.name not in active_ids and age > _STALE_TRANSACTION_SECONDS:
                    orphaned.append(str(child))
        status = (
            "warning" if stale_operations or orphaned else "pass"
        )
        yield _check(
            "transaction.residue",
            "transaction",
            status,
            (
                "No stale operations or transactional residue were found."
                if status == "pass"
                else "Stale operations or transactional residue need review."
            ),
            repairable=bool(orphaned),
            repair_target="transaction-residue",
            active_operation_ids=sorted(active_ids),
            stale_operation_ids=stale_operations,
            orphaned_paths=orphaned,
        )
