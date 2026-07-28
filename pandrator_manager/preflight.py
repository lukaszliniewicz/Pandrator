"""Read-only host checks repeated at plan and execution boundaries."""

from __future__ import annotations

import os
import shutil
import socket
from pathlib import Path
from urllib.parse import urlsplit

from .components import ComponentRegistry
from .components.host import (
    detect_compute,
    require_compute_available,
    resolve_auto_compute,
)
from .components.slots import component_container
from .context import ManagerContext
from .environments import pixi_asset_for
from .errors import ManagerError
from .models import (
    ComputeVariant,
    DesiredComponentState,
    PreflightCheck,
    SizeProvenance,
    TaskSpec,
)

_RESERVED_DISK_BYTES = 512 * 1024 * 1024
_CA_ENVIRONMENT_KEYS = (
    "PANDRATOR_CA_BUNDLE",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
)


class HostPreflight:
    def __init__(
        self,
        context: ManagerContext,
        registry: ComponentRegistry,
    ) -> None:
        self.context = context
        self.registry = registry

    @staticmethod
    def _port_available(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            if os.name == "nt":
                listener.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_EXCLUSIVEADDRUSE,
                    1,
                )
            try:
                listener.bind(("127.0.0.1", port))
            except OSError:
                return False
        return True

    def evaluate(
        self,
        *,
        desired: dict[str, DesiredComponentState],
        tasks: tuple[TaskSpec, ...],
    ) -> tuple[PreflightCheck, ...]:
        checks: list[PreflightCheck] = []
        detected_compute = detect_compute(self.context)
        mutating_components = {
            str(task.component_id)
            for task in tasks
            if task.component_id
        }
        estimated_disk = sum(task.estimated_disk_bytes for task in tasks)
        try:
            free_disk = shutil.disk_usage(self.context.layout.root).free
        except OSError as error:
            checks.append(
                PreflightCheck(
                    code="disk.inspect",
                    status="error",
                    message="Available workspace disk space could not be inspected.",
                    details={"error_type": type(error).__name__},
                )
            )
        else:
            required = estimated_disk + _RESERVED_DISK_BYTES
            approximate = any(
                self.registry.definition(component_id).size_provenance
                == SizeProvenance.ESTIMATE
                for component_id in mutating_components
            )
            sufficient = free_disk >= required
            checks.append(
                PreflightCheck(
                    code="disk.headroom",
                    status=(
                        "pass"
                        if sufficient
                        else ("warning" if approximate else "error")
                    ),
                    message=(
                        "Workspace disk headroom is sufficient."
                        if sufficient
                        else (
                            "Available disk space is below the approximate "
                            "installation estimate. The estimate includes "
                            "variant/model uncertainty, so review it before "
                            "continuing."
                            if approximate
                            else "The workspace does not have enough free disk "
                            "space for the operation and safety reserve."
                        )
                    ),
                    details={
                        "free_bytes": free_disk,
                        "estimated_operation_bytes": estimated_disk,
                        "reserved_bytes": _RESERVED_DISK_BYTES,
                        "estimate_is_approximate": approximate,
                    },
                )
            )
        if any(task.kind == "stage_component" for task in tasks) and not any(
            task.estimated_download_bytes or task.estimated_disk_bytes
            for task in tasks
        ):
            checks.append(
                PreflightCheck(
                    code="estimate.unavailable",
                    status="warning",
                    message=(
                        "This source-backed component does not yet publish a "
                        "complete download and installed-size estimate."
                    ),
                )
            )

        for key in _CA_ENVIRONMENT_KEYS:
            value = str(self.context.environment.get(key) or "").strip()
            if not value:
                continue
            candidate = Path(value).expanduser()
            checks.append(
                PreflightCheck(
                    code=f"tls.{key.lower()}",
                    status="pass" if candidate.is_file() else "error",
                    message=(
                        f"{key} points to a readable CA bundle."
                        if candidate.is_file()
                        else f"{key} points to a missing CA bundle."
                    ),
                    details={"path": str(candidate.resolve(strict=False))},
                )
            )

        stopped_components = {
            str(task.component_id)
            for task in tasks
            if task.kind == "stop_service" and task.component_id
        }
        checked_ports: set[int] = set()
        required_tools = {
            tool
            for component_id in mutating_components
            for tool in self.registry.definition(
                component_id
            ).required_runtime_tools
        }
        if "pixi" in required_tools:
            try:
                asset = pixi_asset_for(
                    self.context.system,
                    self.context.architecture,
                )
            except ManagerError as error:
                checks.append(
                    PreflightCheck(
                        code="runtime.pixi",
                        status="error",
                        message=str(error),
                        details=dict(error.details or {}),
                    )
                )
            else:
                checks.append(
                    PreflightCheck(
                        code="runtime.pixi",
                        status="pass",
                        message="A qualified Pixi bootstrap is available.",
                        details={
                            "system": asset.system,
                            "architecture": asset.architecture,
                        },
                    )
                )
        for component_id in sorted(mutating_components):
            definition = self.registry.definition(component_id)
            selected_desired = desired.get(component_id)
            if selected_desired is not None and selected_desired.present:
                effective_compute = (
                    resolve_auto_compute(
                        self.context,
                        definition,
                        detected=detected_compute,
                    )
                    if selected_desired.compute == ComputeVariant.AUTO
                    else selected_desired.compute
                )
                try:
                    require_compute_available(
                        self.context,
                        definition,
                        effective_compute,
                        detected=detected_compute,
                    )
                except ValueError as error:
                    checks.append(
                        PreflightCheck(
                            code=f"compute.{component_id}",
                            status="error",
                            message=str(error),
                            details={
                                "compute": effective_compute.value,
                                "requested_compute": selected_desired.compute.value,
                                "effective_compute": effective_compute.value,
                            },
                        )
                    )
                else:
                    checks.append(
                        PreflightCheck(
                            code=f"compute.{component_id}",
                            status="pass",
                            message=(
                                (
                                    f"{definition.label} automatic selection "
                                    f"resolved to {effective_compute.value.upper()}."
                                )
                                if selected_desired.compute == ComputeVariant.AUTO
                                else (
                                    f"{definition.label} can use "
                                    f"{effective_compute.value.upper()}."
                                )
                            ),
                            details={
                                "compute": effective_compute.value,
                                "requested_compute": selected_desired.compute.value,
                                "effective_compute": effective_compute.value,
                            },
                        )
                    )
            if os.name == "nt":
                projected = (
                    component_container(self.context.layout, component_id)
                    / "versions"
                    / ("r" * 80)
                )
                if len(str(projected)) >= 230:
                    checks.append(
                        PreflightCheck(
                            code=f"path.{component_id}",
                            status="warning",
                            message=(
                                f"{definition.label} is close to the conservative "
                                "Windows path-length budget; choose a shorter "
                                "workspace if dependency installation fails."
                            ),
                            details={"projected_length": len(str(projected))},
                        )
                    )
            if (
                definition.default_port
                and definition.default_port not in checked_ports
            ):
                checked_ports.add(definition.default_port)
                available = self._port_available(definition.default_port)
                checks.append(
                    PreflightCheck(
                        code=f"port.{definition.default_port}",
                        status=(
                            "pass"
                            if available
                            else "warning"
                        ),
                        message=(
                            f"Port {definition.default_port} is available."
                            if available
                            else (
                                f"Port {definition.default_port} is currently in "
                                "use. The operation will stop a positively owned "
                                "service first and will otherwise fail without "
                                "terminating the occupant."
                                if component_id in stopped_components
                                else (
                                    f"Port {definition.default_port} is currently "
                                    "in use; service validation will refuse to "
                                    "terminate an unrecognized occupant."
                                )
                            )
                        ),
                        details={"port": definition.default_port},
                    )
                )

        for task in tasks:
            if task.kind != "stage_component":
                continue
            source = str(task.inputs.get("repo_url") or "").strip()
            local_source = Path(source).expanduser()
            parsed = urlsplit(source)
            if source and local_source.is_dir():
                checks.append(
                    PreflightCheck(
                        code=f"source.{task.component_id}",
                        status="warning",
                        message=(
                            "This plan uses a local development repository and "
                            "is not a production release installation."
                        ),
                        details={"transport": "local"},
                    )
                )
            elif parsed.scheme.lower() == "https" and parsed.hostname:
                checks.append(
                    PreflightCheck(
                        code=f"source.{task.component_id}",
                        status="pass",
                        message=(
                            "The component will be cloned over HTTPS and "
                            "activated side-by-side at the exact retrieved commit."
                        ),
                        details={"transport": "https"},
                    )
                )
            else:
                checks.append(
                    PreflightCheck(
                        code=f"source.{task.component_id}",
                        status="error",
                        message=(
                            "Component sources must be an HTTPS repository or "
                            "an existing local development repository."
                        ),
                    )
                )

        for component_id, selected in desired.items():
            if (
                selected.present
                and bool(selected.options.get("offline"))
                and any(
                    task.kind == "stage_component"
                    and task.component_id == component_id
                    for task in tasks
                )
            ):
                checks.append(
                    PreflightCheck(
                        code=f"offline.{component_id}",
                        status="error",
                        message=(
                            "Offline installation is unavailable until this "
                            "component has a verified artifact in the manager cache."
                        ),
                    )
                )
        return tuple(checks)

    @staticmethod
    def require_success(checks: tuple[PreflightCheck, ...]) -> None:
        failures = [check for check in checks if check.status == "error"]
        if not failures:
            return
        raise ManagerError(
            "preflight_failed",
            "The host failed one or more operation preflight checks.",
            {
                "checks": [
                    check.model_dump(mode="json")
                    for check in failures
                ]
            },
            409,
        )
