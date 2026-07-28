"""Manager composition root and transport-independent use cases."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from packaging.version import Version

from . import __version__
from .components import ComponentRegistry, builtin_registry
from .components.host import compute_choices, detect_compute
from .context import ManagerContext, WorkspaceLayout
from .doctor import ManagerDoctor
from .errors import ConflictError, ManagerError
from .legacy import LegacyImporter
from .lifecycle import LifecyclePlanner
from .models import (
    ComponentInspection,
    DesiredComponentState,
    DoctorReport,
    LegacyImportReport,
    ManagerStatus,
    OperationKind,
    OperationPlan,
    OperationRecord,
    OperationState,
)
from .planning import Planner
from .releases import ReleaseAuthority, ReleasePlanner, TrustStore
from .releases.discovery import fetch_manager_manifest, manager_manifest_url
from .state import ManagerStore, StoreEventSink


class OperationQueue(Protocol):
    def enqueue(self, operation_id: str) -> None: ...


class ManagerApplication:
    def __init__(
        self,
        context: ManagerContext,
        store: ManagerStore,
        registry: ComponentRegistry,
    ) -> None:
        self.store = store
        self.registry = registry
        self.context = context.with_event_sink(StoreEventSink(store))
        self.planner = Planner(self.context, registry)
        self.lifecycle_planner = LifecyclePlanner(self.context)
        self.release_authority = ReleaseAuthority(
            self.context,
            store,
        )
        self.release_planner = ReleasePlanner(
            self.context,
            registry,
            self.release_authority,
        )
        self.instance_id: str | None = None
        self.operation_queue: OperationQueue | None = None

    def configure_release_trust(self, trust_root: TrustStore) -> None:
        """Inject a test/release-qualification root at the composition boundary."""

        self.release_authority = ReleaseAuthority(
            self.context,
            self.store,
            trust_root=trust_root,
        )
        self.release_planner = ReleasePlanner(
            self.context,
            self.registry,
            self.release_authority,
        )

    def attach_operation_queue(self, operation_queue: OperationQueue) -> None:
        """Attach the daemon-owned durable executor.

        Keeping this as a small protocol lets the application remain usable for
        read-only/offline planning tests without constructing process services.
        """
        self.operation_queue = operation_queue

    def pandrator_runtime_environment(self) -> dict[str, str]:
        """Project manager-owned installation choices into the application.

        These are defaults, not user workflow settings.  Keeping the projection
        here means both initial daemon composition and later spec refreshes use
        the same persisted desired state.
        """

        desired = self.store.component_records().get("crispasr", (None, None))[0]
        if desired is None or not desired.present:
            return {}
        options = desired.options or {}
        environment: dict[str, str] = {}
        engine = str(options.get("engine") or "").strip()
        quantization = str(
            desired.quantization or options.get("quantization") or ""
        ).strip()
        if engine:
            environment["CRISPASR_DEFAULT_ENGINE"] = engine
        if quantization:
            environment["CRISPASR_DEFAULT_QUANTIZATION"] = quantization
        return environment

    def status(self) -> ManagerStatus:
        active = self.store.list_operations(active_only=True, limit=1)
        return ManagerStatus(
            manager_version=__version__,
            instance_id=self.instance_id,
            workspace=str(self.context.layout.workspace),
            configuration_revision=self.store.configuration_revision(),
            ready=True,
            capabilities=(
                "components",
                "planning",
                "durable_operations",
                "event_replay",
                "doctor",
                "legacy_import",
                "uninstall_planning",
                "signed_release_plans",
                "side_by_side_application_activation",
            ),
            active_operation_id=active[0].id if active else None,
        )

    def doctor(self, *, supervisor=None) -> DoctorReport:
        return ManagerDoctor(
            self.context,
            self.store,
            self.registry,
            supervisor=supervisor,
        ).inspect()

    def legacy_report(self) -> LegacyImportReport | None:
        return LegacyImporter(
            self.context,
            self.store,
            self.registry,
        ).inspect()

    def import_legacy(
        self,
        *,
        source_digest: str,
        confirmed: bool,
    ) -> dict[str, Any]:
        if not confirmed:
            raise ManagerError(
                "confirmation_required",
                "Legacy import requires explicit confirmation.",
                http_status=409,
            )
        if self.store.list_operations(active_only=True, limit=1):
            raise ConflictError(
                "Legacy import cannot run while an operation is active."
            )
        importer = LegacyImporter(
            self.context,
            self.store,
            self.registry,
        )
        report = importer.inspect()
        if report is None:
            raise ManagerError(
                "legacy_workspace_not_found",
                "No legacy installer configuration was found.",
                http_status=404,
            )
        if report.source_digest != source_digest:
            raise ConflictError(
                "Legacy configuration changed after review.",
                {
                    "reviewed_digest": source_digest,
                    "current_digest": report.source_digest,
                },
            )
        revision, data_reconciliation = importer.apply_with_result(
            report,
            confirmed=True,
        )
        return {
            "status": (
                "already_imported"
                if report.already_imported
                else "imported"
            ),
            "configuration_revision": revision,
            "data_reconciliation": data_reconciliation,
            "restart_manager_required": bool(
                report.positively_identified
            ),
            "report": report.model_dump(mode="json"),
        }

    def list_components(self) -> list[dict[str, Any]]:
        stored = self.store.component_records()
        detected = detect_compute(self.context)
        result: list[dict[str, Any]] = []
        for definition in self.registry.definitions():
            desired, persisted_inspection = stored.get(
                definition.id,
                (None, None),
            )
            inspection = self.planner.inspect(definition.id, desired)
            result.append(
                {
                    "definition": definition.model_dump(mode="json"),
                    "desired": (
                        desired.model_dump(mode="json")
                        if desired is not None
                        else None
                    ),
                    "inspection": inspection.model_dump(mode="json"),
                    "compute_choices": list(
                        compute_choices(
                            self.context,
                            definition,
                            detected=detected,
                        )
                    ),
                    "previous_inspection": (
                        persisted_inspection.model_dump(mode="json")
                        if persisted_inspection is not None
                        else None
                    ),
                }
            )
        return result

    def probe(
        self,
        component_ids: tuple[str, ...] | None = None,
        *,
        persist: bool = True,
    ) -> dict[str, ComponentInspection]:
        stored = self.store.component_records()
        selected = component_ids or tuple(
            definition.id for definition in self.registry.definitions()
        )
        inspections: dict[str, ComponentInspection] = {}
        for component_id in selected:
            desired = stored.get(component_id, (None, None))[0]
            inspection = self.planner.inspect(component_id, desired)
            inspections[component_id] = inspection
            if persist:
                self.store.save_component(inspection, desired=desired)
        return inspections

    def plan(
        self,
        *,
        kind: OperationKind,
        desired: dict[str, DesiredComponentState],
        expected_revision: int | None = None,
        persist: bool = True,
    ) -> OperationPlan:
        revision = self.store.configuration_revision()
        plan = self.planner.create_plan(
            kind=kind,
            desired=desired,
            expected_revision=revision if expected_revision is None else expected_revision,
            actual_revision=revision,
        )
        if persist:
            self.store.save_plan(plan)
            self.context.event_sink.emit(
                "plan.created",
                {
                    "plan_id": plan.id,
                    "kind": plan.kind.value,
                    "digest": plan.digest,
                    "expires_at": plan.expires_at.isoformat(),
                },
            )
        return plan

    def release_plan(
        self,
        manifest: dict[str, Any],
        *,
        expected_revision: int | None = None,
        offline: bool = False,
        start_after_activation: bool = True,
        persist: bool = True,
    ) -> OperationPlan:
        revision = self.store.configuration_revision()
        plan = self.release_planner.create_plan(
            manifest,
            expected_revision=(
                revision
                if expected_revision is None
                else expected_revision
            ),
            actual_revision=revision,
            offline=offline,
            start_after_activation=start_after_activation,
        )
        if persist:
            self.store.save_plan(plan)
            release = dict(plan.impacts.get("release") or {})
            self.context.event_sink.emit(
                "release.plan_created",
                {
                    "plan_id": plan.id,
                    "digest": plan.digest,
                    "expires_at": plan.expires_at.isoformat(),
                    "product": release.get("product"),
                    "version": release.get("version"),
                    "sequence": release.get("sequence"),
                },
            )
        return plan

    def manager_update(self) -> dict[str, Any]:
        """Discover and verify the canonical Manager update without activating it."""

        manifest = fetch_manager_manifest(self.context)
        release = self.release_authority.verify(
            manifest,
            expected_product="pandrator-manager",
        )
        available = Version(release.manifest.payload.version) > Version(__version__)
        return {
            "status": "available" if available else "current",
            "current_version": __version__,
            "version": release.manifest.payload.version,
            "channel": release.manifest.payload.channel,
            "published_at": release.manifest.payload.published_at.isoformat(),
            "manifest_url": manager_manifest_url(self.context),
            "manifest": manifest if available else None,
        }

    def uninstall_plan(
        self,
        *,
        expected_revision: int | None = None,
        purge_data: bool = False,
        export_data: str | Path | None = None,
        persist: bool = True,
    ) -> OperationPlan:
        revision = self.store.configuration_revision()
        plan = self.lifecycle_planner.create_uninstall_plan(
            expected_revision=(
                revision if expected_revision is None else expected_revision
            ),
            actual_revision=revision,
            purge_data=purge_data,
            export_data=export_data,
        )
        if persist:
            self.store.save_plan(plan)
            self.context.event_sink.emit(
                "uninstall.plan_created",
                {
                    "plan_id": plan.id,
                    "digest": plan.digest,
                    "expires_at": plan.expires_at.isoformat(),
                    **dict(plan.impacts.get("uninstall") or {}),
                },
            )
        return plan

    def submit_operation(
        self,
        *,
        plan_id: str,
        plan_digest: str,
        accepted_confirmations: tuple[str, ...],
        idempotency_key: str,
    ) -> tuple[OperationRecord, bool]:
        plan = self.store.get_plan(plan_id)
        if plan.digest != plan_digest:
            from .errors import ConflictError

            raise ConflictError("Operation plan digest does not match.")
        required = {confirmation.key for confirmation in plan.confirmations}
        missing = required.difference(accepted_confirmations)
        if missing:
            from .errors import ConflictError

            raise ConflictError(
                "Operation plan confirmations are incomplete.",
                {"missing_confirmations": sorted(missing)},
            )
        now = datetime.fromtimestamp(
            self.context.clock.time(),
            timezone.utc,
        )
        record = OperationRecord(
            id=str(uuid.uuid4()),
            plan_id=plan.id,
            kind=plan.kind,
            state=OperationState.QUEUED,
            created_at=now,
            updated_at=now,
        )
        request_payload = {
            "plan_id": plan_id,
            "plan_digest": plan_digest,
            "accepted_confirmations": sorted(set(accepted_confirmations)),
        }
        result, created = self.store.begin_operation(
            plan,
            record,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            now=now,
            actual_revision=self.store.configuration_revision(),
        )
        if created:
            self.context.event_sink.emit(
                "operation.queued",
                {
                    "operation_id": result.id,
                    "plan_id": plan.id,
                    "kind": plan.kind.value,
                },
                operation_id=result.id,
            )
            if self.operation_queue is not None:
                self.operation_queue.enqueue(result.id)
        return result, created


def create_application(
    workspace: str | Path,
    *,
    ensure_layout: bool = True,
    registry: ComponentRegistry | None = None,
    release_trust_root: TrustStore | None = None,
) -> ManagerApplication:
    layout = WorkspaceLayout.from_value(workspace)
    if ensure_layout:
        layout.ensure_base_directories()
    store = ManagerStore(layout.database)
    context = ManagerContext(layout=layout)
    application = ManagerApplication(
        context=context,
        store=store,
        registry=registry or builtin_registry(),
    )
    if release_trust_root is not None:
        application.configure_release_trust(release_trust_root)
    return application
