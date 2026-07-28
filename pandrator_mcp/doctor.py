"""Layered, secret-free target diagnostics for operators and setup agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .clients import (
    ApplicationClient,
    ApplicationProxyManagerGateway,
    LocalManagerGateway,
    ManagerUnavailableGateway,
    bootstrap_local_application,
    discover_local_application,
)
from .compatibility import negotiate_compatibility
from .credentials import CredentialResolver
from .errors import PandratorMcpError
from .network_policy import TargetMode
from .settings import McpSettings
from .targets import TargetProfile, TargetRegistry

CheckState = Literal["pass", "warning", "fail", "skipped"]


class DoctorCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer: str
    state: CheckState
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class DoctorReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    target: str
    healthy: bool
    checks: list[DoctorCheck]


@dataclass(slots=True)
class _Probe:
    settings: McpSettings
    credentials: CredentialResolver
    checks: list[DoctorCheck] = field(default_factory=list)
    profile: TargetProfile | None = None
    registry: TargetRegistry | None = None
    application: ApplicationClient | None = None
    identity: dict[str, Any] | None = None
    openapi: dict[str, Any] | None = None
    capabilities: dict[str, Any] | None = None

    def add(
        self,
        layer: str,
        state: CheckState,
        message: str,
        **details: Any,
    ) -> None:
        self.checks.append(
            DoctorCheck(
                layer=layer,
                state=state,
                message=message,
                details=details,
            )
        )

    def load_configuration(self) -> bool:
        try:
            self.registry = TargetRegistry.from_file(
                self.settings.configuration_path,
                local_discovery=discover_local_application,
            )
            self.profile = self.registry.get(self.settings.target_name)
        except PandratorMcpError as error:
            self.add("configuration", "fail", str(error), code=error.code)
            return False
        self.add(
            "configuration",
            "pass",
            "The non-secret target profile is valid.",
            mode=self.profile.mode.value,
            application_credential_configured=(self.profile.application_credential is not None),
            manager_recovery_enrolled=(self.profile.manager_recovery_credential is not None),
        )
        return True

    def resolve_network(self) -> bool:
        assert self.registry is not None
        try:
            resolved = self.registry.resolve(self.settings.target_name)
        except PandratorMcpError as error:
            self.add("dns_route", "fail", str(error), code=error.code)
            return False
        self.add(
            "dns_route",
            "pass",
            "The target resolved inside its configured trust boundary.",
            zone=resolved.application.zone.value,
            address_count=len(resolved.application.addresses),
            tls=resolved.application.scheme == "https",
            explicit_proxy=bool(resolved.application.proxy_origin),
        )
        if resolved.application.scheme == "https":
            self.add(
                "tls",
                "pass",
                "HTTPS is required and certificate validation is enabled.",
                explicit_ca=bool(resolved.application.ca_bundle),
            )
        elif resolved.application.zone.value == "loopback":
            self.add("tls", "pass", "Loopback HTTP stays on the local host.")
        else:
            self.add(
                "tls",
                "warning",
                "Private-network HTTP was explicitly accepted; prefer HTTPS or a VPN.",
            )
        self.application = ApplicationClient(
            self.registry.bind(self.settings.target_name),
            self.credentials,
            local_bootstrap=bootstrap_local_application,
            timeout_seconds=self.settings.request_timeout_seconds,
            maximum_response_bytes=self.settings.maximum_response_bytes,
        )
        return True

    def probe_application(self) -> bool:
        assert self.application is not None
        try:
            health = self.application.health()
        except PandratorMcpError as error:
            self.add("application", "fail", str(error), code=error.code)
            return False
        state = "pass" if health.get("status") == "ok" else "warning"
        self.add(
            "application",
            state,
            "The Pandrator health endpoint responded.",
            service=health.get("service"),
            version=health.get("version"),
            migration=health.get("migration"),
        )
        try:
            self.openapi = self.application.openapi()
        except PandratorMcpError as error:
            self.add("api", "fail", str(error), code=error.code)
            return False
        self.add(
            "api",
            "pass",
            "The unauthenticated API contract is reachable.",
            openapi=self.openapi.get("openapi"),
            api_version=(self.openapi.get("info") or {}).get("version"),
        )
        return True

    def probe_identity_and_authentication(self) -> bool:
        assert self.application is not None
        try:
            self.identity = self.application.identity()
        except PandratorMcpError as error:
            layer = (
                "authentication"
                if error.code in {"authentication_required", "scope_denied"}
                else "identity"
            )
            self.add(layer, "fail", str(error), code=error.code)
            return False
        self.add(
            "authentication",
            "pass",
            "The configured principal authenticated successfully.",
        )
        self.add(
            "identity",
            "pass",
            "The authenticated application identity matches the target pin.",
            instance_id=self.identity.get("instance_id"),
            managed=bool(self.identity.get("managed")),
            manager_instance_id=self.identity.get("manager_instance_id"),
        )
        return True

    def probe_compatibility(self) -> bool:
        assert self.identity is not None
        assert self.openapi is not None
        assert self.profile is not None
        report = negotiate_compatibility(
            self.identity,
            self.openapi,
            manager_expected=self.profile.mode != TargetMode.EXTERNAL_APPLICATION,
        )
        self.add(
            "compatibility",
            "pass" if report["compatible"] else "fail",
            (
                "The application exposes the required read-only MCP contract."
                if report["compatible"]
                else "The application is not compatible with this MCP build."
            ),
            **report,
        )
        return bool(report["compatible"])

    def probe_manager(self) -> None:
        assert self.profile is not None
        if self.profile.mode == TargetMode.LOCAL_MANAGED:
            gateway = LocalManagerGateway(str(self.profile.workspace))
        elif self.profile.mode == TargetMode.EXTERNAL_APPLICATION:
            gateway = ManagerUnavailableGateway()
        else:
            if self.application is None:
                self.add(
                    "manager",
                    "warning",
                    "The application proxy is unavailable.",
                    direct_recovery_enrolled=(self.profile.manager_recovery_credential is not None),
                )
                return
            gateway = ApplicationProxyManagerGateway(self.application)
        try:
            status = gateway.status()
        except PandratorMcpError as error:
            state: CheckState = (
                "warning" if self.profile.manager_recovery_credential is None else "fail"
            )
            self.add(
                "manager",
                state,
                str(error),
                code=error.code,
                direct_recovery_enrolled=(self.profile.manager_recovery_credential is not None),
            )
            return
        available = status.get("available", True) is not False
        self.add(
            "manager",
            "pass" if available else "warning",
            (
                "Manager diagnostics are reachable."
                if available
                else "This target intentionally has no managed control plane."
            ),
            direct_recovery_enrolled=(self.profile.manager_recovery_credential is not None),
        )

    def probe_worker(self) -> None:
        assert self.application is not None
        try:
            self.capabilities = self.application.capabilities()
        except PandratorMcpError as error:
            self.add("worker", "warning", str(error), code=error.code)
            return
        self.add(
            "worker",
            "pass",
            "Authenticated capability probes completed; durable work can be inspected.",
            capability_snapshot_available=True,
        )


def diagnose_target(
    settings: McpSettings,
    *,
    credentials: CredentialResolver | None = None,
) -> DoctorReport:
    """Run ordered diagnostics and preserve partial results after failures."""

    probe = _Probe(
        settings=settings,
        credentials=credentials or CredentialResolver(),
    )
    if not probe.load_configuration():
        return DoctorReport(
            target=settings.target_name,
            healthy=False,
            checks=probe.checks,
        )
    if not probe.resolve_network():
        if probe.profile and probe.profile.mode == TargetMode.LOCAL_MANAGED:
            probe.probe_manager()
        return DoctorReport(
            target=settings.target_name,
            healthy=False,
            checks=probe.checks,
        )
    if not probe.probe_application():
        probe.probe_manager()
        return DoctorReport(
            target=settings.target_name,
            healthy=False,
            checks=probe.checks,
        )
    authenticated = probe.probe_identity_and_authentication()
    compatible = authenticated and probe.probe_compatibility()
    probe.probe_manager()
    if authenticated:
        probe.probe_worker()
    else:
        probe.add(
            "worker",
            "skipped",
            "Worker capability checks require application authentication.",
        )
    healthy = compatible and all(check.state != "fail" for check in probe.checks)
    return DoctorReport(
        target=settings.target_name,
        healthy=healthy,
        checks=probe.checks,
    )
