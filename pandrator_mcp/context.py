"""Runtime composition for one MCP process and one selected target."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .clients import (
    ApplicationClient,
    ApplicationProxyManagerGateway,
    FallbackManagerGateway,
    LocalManagerGateway,
    ManagerGateway,
    ManagerUnavailableGateway,
    RecoveryManagerGateway,
    bootstrap_local_application,
    discover_local_application,
)
from .credentials import CredentialResolver
from .errors import PandratorMcpError
from .guide_registry import GuideRegistry
from .network_policy import TargetMode
from .settings import McpSettings
from .targets import LocalSourceRoot, TargetProfile, TargetRegistry

MANAGED_TARGET_NAME = "managed-local"


@dataclass(slots=True)
class McpRuntime:
    settings: McpSettings
    guides: GuideRegistry
    application: ApplicationClient | None
    manager: ManagerGateway
    profile: TargetProfile | None = None
    startup_error: PandratorMcpError | None = None

    def current_profile(self) -> TargetProfile | None:
        """Reload the non-secret target profile so path changes apply immediately."""

        if self.profile is None:
            return None
        try:
            from .targets import TargetStore

            profiles = TargetStore(self.settings.configuration_path).load(missing_ok=True)
            return next(
                (item for item in profiles if item.name == self.profile.name),
                self.profile,
            )
        except (OSError, ValueError):
            return self.profile

    def require_application(self) -> ApplicationClient:
        if self.startup_error is not None:
            raise self.startup_error
        if self.application is None:
            raise PandratorMcpError(
                "application_unavailable",
                "The selected Pandrator application target is unavailable.",
            )
        return self.application


def build_runtime(
    settings: McpSettings,
    *,
    registry: TargetRegistry | None = None,
    credentials: CredentialResolver | None = None,
) -> McpRuntime:
    guides = GuideRegistry()
    try:
        selected_registry = registry or TargetRegistry.from_file(
            settings.configuration_path,
            local_discovery=discover_local_application,
        )
        profile = selected_registry.get(settings.target_name)
        selected_credentials = credentials or CredentialResolver()
        application = ApplicationClient(
            selected_registry.bind(settings.target_name),
            selected_credentials,
            local_bootstrap=bootstrap_local_application,
            timeout_seconds=settings.request_timeout_seconds,
            maximum_response_bytes=settings.maximum_response_bytes,
        )
        if profile.mode == TargetMode.LOCAL_MANAGED:
            manager: ManagerGateway = LocalManagerGateway(str(profile.workspace))
        elif profile.mode == TargetMode.EXTERNAL_APPLICATION:
            manager = ManagerUnavailableGateway()
        else:
            primary_manager = ApplicationProxyManagerGateway(application)
            if profile.manager_recovery_origin:
                manager = FallbackManagerGateway(
                    primary_manager,
                    RecoveryManagerGateway(
                        selected_registry.bind(settings.target_name),
                        selected_credentials,
                        timeout_seconds=settings.request_timeout_seconds,
                        maximum_response_bytes=(settings.maximum_response_bytes),
                    ),
                )
            else:
                manager = primary_manager
        return McpRuntime(
            settings=settings,
            guides=guides,
            application=application,
            manager=manager,
            profile=profile,
        )
    except PandratorMcpError as error:
        return McpRuntime(
            settings=settings,
            guides=guides,
            application=None,
            manager=ManagerUnavailableGateway(),
            profile=None,
            startup_error=error,
        )


def build_managed_runtime(
    workspace: str | Path,
    *,
    configuration_path: str | Path,
    credentials: CredentialResolver | None = None,
) -> McpRuntime:
    """Build the Manager-owned local runtime used by the HTTP service.

    The generated target contains no credentials. New managed targets expose the
    current user's home directory under the opaque name ``home`` and materialize
    downloads beneath the managed workspace by default. Owners can narrow or add
    roots later through the UI/CLI while endpoints remain dynamically discovered.
    """

    selected_workspace = Path(workspace).expanduser().resolve(strict=False)
    selected_configuration = Path(configuration_path).expanduser().resolve(strict=False)
    from .targets import TargetStore

    store = TargetStore(selected_configuration)
    profiles = store.load(missing_ok=True)
    existing = next(
        (profile for profile in profiles if profile.name == MANAGED_TARGET_NAME),
        None,
    )
    if existing is None:
        store.put(
            TargetProfile(
                name=MANAGED_TARGET_NAME,
                mode=TargetMode.LOCAL_MANAGED,
                workspace=str(selected_workspace),
                local_source_roots=(
                    LocalSourceRoot(
                        name="home",
                        path=str(Path.home().resolve(strict=False)),
                    ),
                ),
                local_output_root=str((selected_workspace / "exports").resolve(strict=False)),
                requested_application_scopes=(
                    "app.read",
                    "app.write",
                    "app.run",
                    "app.cancel",
                    "manager.read",
                    "manager.runtime",
                    "manager.mutate",
                ),
            )
        )
        profiles = store.load(missing_ok=False)
    elif Path(str(existing.workspace)).resolve(strict=False) != selected_workspace:
        raise ValueError("The managed MCP target belongs to a different Manager workspace.")

    registry = TargetRegistry(
        profiles,
        local_discovery=discover_local_application,
    )
    return build_runtime(
        McpSettings(
            target_name=MANAGED_TARGET_NAME,
            configuration_path=selected_configuration,
        ),
        registry=registry,
        credentials=credentials,
    )
