"""Non-secret target profiles and their sole endpoint-resolution boundary."""

from __future__ import annotations

import ipaddress
import json
import os
import tempfile
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .credentials import CredentialReference
from .errors import TargetResolutionError
from .network_policy import (
    NetworkPolicy,
    ResolvedEndpoint,
    TargetMode,
    normalize_origin,
)

APPLICATION_SCOPES = frozenset(
    {
        "app.read",
        "app.write",
        "app.run",
        "app.cancel",
        "app.credentials.read",
        "app.credentials.write",
        "manager.read",
        "manager.runtime",
        "manager.mutate",
    }
)
MANAGER_RECOVERY_SCOPES = frozenset(
    {
        "manager.read",
        "manager.runtime",
        "manager.mutate",
    }
)


class TargetIdentityExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    application_instance_id: str | None = None
    canonical_application_origin: str | None = None
    manager_instance_id: str | None = None

    @field_validator("canonical_application_origin", mode="before")
    @classmethod
    def validate_origin(cls, value: object) -> str | None:
        return normalize_origin(str(value)) if value else None


class TargetProfile(BaseModel):
    """Persisted target configuration; credential fields are handles only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
    mode: TargetMode
    workspace: str | None = None
    application_origin: str | None = None
    manager_recovery_origin: str | None = None
    allowed_private_cidrs: tuple[str, ...] = ()
    allow_insecure_private_network: bool = False
    ca_bundle: str | None = None
    proxy_origin: str | None = None
    automation_client_id: str | None = None
    automation_client_name: str = "Pandrator MCP"
    requested_application_scopes: tuple[str, ...] = ("app.read",)
    enrolled_subject: str | None = None
    credential_expires_at: str | None = None
    manager_automation_client_id: str | None = None
    manager_automation_client_name: str = "Pandrator MCP recovery"
    manager_requested_scopes: tuple[str, ...] = ("manager.read",)
    manager_enrolled_subject: str | None = None
    manager_credential_expires_at: str | None = None
    application_credential: CredentialReference | None = None
    manager_recovery_credential: CredentialReference | None = None
    expected_identity: TargetIdentityExpectation = Field(default_factory=TargetIdentityExpectation)

    @field_validator(
        "application_origin",
        "manager_recovery_origin",
        "proxy_origin",
        mode="before",
    )
    @classmethod
    def validate_origins(cls, value: object) -> str | None:
        return normalize_origin(str(value)) if value else None

    @field_validator("ca_bundle", mode="before")
    @classmethod
    def validate_ca_bundle(cls, value: object) -> str | None:
        if not value:
            return None
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            raise ValueError("A configured CA bundle path must be absolute.")
        return str(path)

    @field_validator(
        "automation_client_id",
        "manager_automation_client_id",
        mode="before",
    )
    @classmethod
    def validate_automation_client_id(
        cls,
        value: object,
    ) -> str | None:
        if not value:
            return None
        try:
            parsed = uuid.UUID(str(value))
        except ValueError as error:
            raise ValueError(
                "The automation client ID must be a UUID."
            ) from error
        if parsed.version != 4:
            raise ValueError(
                "The automation client ID must be a random UUID."
            )
        return str(parsed)

    @field_validator("requested_application_scopes", mode="before")
    @classmethod
    def validate_application_scopes(
        cls,
        value: object,
    ) -> tuple[str, ...]:
        values = tuple(str(item) for item in (value or ()))
        selected = tuple(dict.fromkeys(values))
        unknown = set(selected) - APPLICATION_SCOPES
        if unknown:
            raise ValueError(
                "Unknown application scope(s): "
                + ", ".join(sorted(unknown))
            )
        if not selected:
            raise ValueError(
                "At least one application scope must be requested."
            )
        return selected

    @field_validator("manager_requested_scopes", mode="before")
    @classmethod
    def validate_manager_scopes(
        cls,
        value: object,
    ) -> tuple[str, ...]:
        values = tuple(str(item) for item in (value or ()))
        selected = tuple(dict.fromkeys(values))
        unknown = set(selected) - MANAGER_RECOVERY_SCOPES
        if unknown:
            raise ValueError(
                "Unknown Manager recovery scope(s): "
                + ", ".join(sorted(unknown))
            )
        if not selected:
            raise ValueError(
                "At least one Manager recovery scope must be requested."
            )
        return selected

    @field_validator("allowed_private_cidrs", mode="before")
    @classmethod
    def validate_private_cidrs(cls, value: object) -> tuple[str, ...]:
        values = tuple(value or ())
        normalized: list[str] = []
        for supplied in values:
            try:
                network = ipaddress.ip_network(str(supplied), strict=False)
            except ValueError as error:
                raise ValueError(f"Invalid private CIDR: {supplied!s}.") from error
            shared_vpn = isinstance(network, ipaddress.IPv4Network) and network.subnet_of(
                ipaddress.ip_network("100.64.0.0/10")
            )
            if not (network.is_private or network.is_loopback or shared_vpn):
                raise ValueError("Allowed LAN/VPN CIDRs must be private or loopback ranges.")
            if network.is_link_local or network.is_multicast or network.is_unspecified:
                raise ValueError("Link-local, multicast, and unspecified CIDRs are forbidden.")
            normalized.append(str(network))
        return tuple(dict.fromkeys(normalized))

    @model_validator(mode="after")
    def validate_mode(self) -> "TargetProfile":
        if self.mode == TargetMode.LOCAL_MANAGED:
            if not self.workspace:
                raise ValueError("A local-managed target requires a Manager workspace.")
            if self.application_origin:
                raise ValueError("A local-managed application origin is discovered from Manager.")
            if self.proxy_origin:
                raise ValueError("A local-managed target cannot use an HTTP proxy.")
        elif not self.application_origin:
            raise ValueError("A remote target requires an application origin.")

        if self.mode == TargetMode.EXTERNAL_HTTPS:
            if not str(self.application_origin).startswith("https://"):
                raise ValueError("External targets require HTTPS.")
            if self.allowed_private_cidrs:
                raise ValueError("External HTTPS targets cannot opt into private CIDRs.")
        if self.mode == TargetMode.PRIVATE_NETWORK and not self.allowed_private_cidrs:
            raise ValueError("A LAN/VPN target requires at least one allowed private CIDR.")
        if (
            self.application_origin
            and self.application_origin.startswith("http://")
            and self.mode != TargetMode.LOCAL_MANAGED
            and not self.allow_insecure_private_network
        ):
            raise ValueError("Remote private HTTP requires explicit insecure-transport consent.")
        if self.manager_recovery_origin and not self.manager_recovery_origin.startswith("https://"):
            raise ValueError("Direct Manager recovery always requires HTTPS.")
        if self.mode == TargetMode.EXTERNAL_APPLICATION and (
            self.workspace or self.manager_recovery_origin or self.manager_recovery_credential
        ):
            raise ValueError("An externally managed application cannot configure Manager access.")
        if self.manager_recovery_credential is not None and self.manager_recovery_origin is None:
            raise ValueError("A Manager recovery credential requires a recovery origin.")
        if (
            self.application_credential is not None
            and self.application_credential.audience != "application"
        ):
            raise ValueError("The application credential must use the application audience.")
        if (
            self.manager_recovery_credential is not None
            and self.manager_recovery_credential.audience != "manager_recovery"
        ):
            raise ValueError("The recovery credential must use the manager_recovery audience.")
        return self


class ResolvedTarget(BaseModel):
    """Runtime-only endpoint set produced exclusively by TargetRegistry."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    profile_name: str
    mode: TargetMode
    workspace: str | None = None
    application: ResolvedEndpoint
    manager_recovery: ResolvedEndpoint | None = None
    application_credential: CredentialReference | None = None
    manager_recovery_credential: CredentialReference | None = None
    expected_identity: TargetIdentityExpectation
    discovered_manager_instance_id: str | None = None


LocalDiscovery = Callable[[TargetProfile], tuple[str, str | None]]


class TargetBinding:
    """Opaque process-selected target handle passed to downstream clients."""

    __slots__ = ("__registry", "name")

    def __init__(self, registry: "TargetRegistry", name: str) -> None:
        self.__registry = registry
        self.name = name

    def resolve(self) -> ResolvedTarget:
        """Re-resolve through policy before each downstream request."""

        return self.__registry.resolve(self.name)

    def __repr__(self) -> str:
        return f"TargetBinding(name={self.name!r})"


class TargetRegistry:
    """Load non-secret profiles and resolve every downstream endpoint."""

    def __init__(
        self,
        profiles: Iterable[TargetProfile],
        *,
        network_policy: NetworkPolicy | None = None,
        local_discovery: LocalDiscovery | None = None,
    ) -> None:
        selected = tuple(profiles)
        self._profiles = {profile.name: profile for profile in selected}
        if len(self._profiles) != len(selected):
            raise ValueError("Target profile names must be unique.")
        self._network_policy = network_policy or NetworkPolicy()
        self._local_discovery = local_discovery

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        network_policy: NetworkPolicy | None = None,
        local_discovery: LocalDiscovery | None = None,
    ) -> "TargetRegistry":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise TargetResolutionError(
                "The Pandrator MCP target configuration does not exist."
            ) from error
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise TargetResolutionError(
                "The Pandrator MCP target configuration is invalid."
            ) from error
        values = payload.get("targets") if isinstance(payload, dict) else None
        if not isinstance(values, list):
            raise TargetResolutionError("The target configuration must contain a targets list.")
        try:
            profiles = tuple(TargetProfile.model_validate(value) for value in values)
        except (TypeError, ValueError) as error:
            raise TargetResolutionError(
                "One or more Pandrator MCP target profiles are invalid."
            ) from error
        return cls(
            profiles,
            network_policy=network_policy,
            local_discovery=local_discovery,
        )

    def list(self) -> tuple[TargetProfile, ...]:
        return tuple(self._profiles[name] for name in sorted(self._profiles))

    def get(self, name: str) -> TargetProfile:
        try:
            return self._profiles[name]
        except KeyError as error:
            raise TargetResolutionError(
                "The selected Pandrator MCP target does not exist.",
                details={"target": name},
            ) from error

    def bind(self, name: str) -> TargetBinding:
        self.get(name)
        return TargetBinding(self, name)

    def resolve(self, name: str) -> ResolvedTarget:
        profile = self.get(name)
        discovered_manager_instance_id = None
        if profile.mode == TargetMode.LOCAL_MANAGED:
            if self._local_discovery is None:
                raise TargetResolutionError(
                    "Local Manager discovery is unavailable in this installation."
                )
            application_origin, discovered_manager_instance_id = self._local_discovery(profile)
        else:
            application_origin = str(profile.application_origin)
        application = self._network_policy.resolve(
            application_origin,
            mode=profile.mode,
            allowed_private_cidrs=profile.allowed_private_cidrs,
            allow_insecure_private_network=profile.allow_insecure_private_network,
            ca_bundle=profile.ca_bundle,
            proxy_origin=profile.proxy_origin,
        )
        manager_recovery = None
        if profile.manager_recovery_origin:
            recovery_mode = (
                TargetMode.PRIVATE_NETWORK
                if profile.allowed_private_cidrs
                else TargetMode.EXTERNAL_HTTPS
            )
            manager_recovery = self._network_policy.resolve(
                profile.manager_recovery_origin,
                mode=recovery_mode,
                allowed_private_cidrs=profile.allowed_private_cidrs,
                ca_bundle=profile.ca_bundle,
                proxy_origin=profile.proxy_origin,
            )
            if manager_recovery.scheme != "https":
                raise TargetResolutionError("Direct Manager recovery must use authenticated HTTPS.")
        return ResolvedTarget(
            profile_name=profile.name,
            mode=profile.mode,
            workspace=profile.workspace,
            application=application,
            manager_recovery=manager_recovery,
            application_credential=profile.application_credential,
            manager_recovery_credential=profile.manager_recovery_credential,
            expected_identity=profile.expected_identity,
            discovered_manager_instance_id=discovered_manager_instance_id,
        )


class TargetStore:
    """Atomic persistence for non-secret target profiles."""

    MAXIMUM_CONFIGURATION_BYTES = 1024 * 1024

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()

    def load(self, *, missing_ok: bool = True) -> tuple[TargetProfile, ...]:
        try:
            if self.path.stat().st_size > self.MAXIMUM_CONFIGURATION_BYTES:
                raise TargetResolutionError("The target configuration exceeds the size limit.")
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            if missing_ok:
                return ()
            raise TargetResolutionError(
                "The Pandrator MCP target configuration does not exist."
            ) from error
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise TargetResolutionError(
                "The Pandrator MCP target configuration is invalid."
            ) from error
        values = payload.get("targets") if isinstance(payload, dict) else None
        if not isinstance(values, list):
            raise TargetResolutionError("The target configuration must contain a targets list.")
        try:
            profiles = tuple(TargetProfile.model_validate(value) for value in values)
        except (TypeError, ValueError) as error:
            raise TargetResolutionError(
                "One or more Pandrator MCP target profiles are invalid."
            ) from error
        if len({profile.name for profile in profiles}) != len(profiles):
            raise TargetResolutionError("Target profile names must be unique.")
        return profiles

    def save(self, profiles: Iterable[TargetProfile]) -> None:
        selected = tuple(sorted(profiles, key=lambda item: item.name))
        if len({profile.name for profile in selected}) != len(selected):
            raise ValueError("Target profile names must be unique.")
        payload = {
            "schema_version": 1,
            "targets": [profile.model_dump(mode="json", exclude_none=True) for profile in selected],
        }
        serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        if len(serialized.encode("utf-8")) > self.MAXIMUM_CONFIGURATION_BYTES:
            raise ValueError("The target configuration exceeds the size limit.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary, self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def put(self, profile: TargetProfile, *, replace: bool = False) -> None:
        profiles = {item.name: item for item in self.load()}
        if profile.name in profiles and not replace:
            raise ValueError(f"Target {profile.name!r} already exists; use --replace explicitly.")
        profiles[profile.name] = profile
        self.save(profiles.values())

    def remove(self, name: str) -> TargetProfile:
        profiles = {item.name: item for item in self.load(missing_ok=False)}
        try:
            removed = profiles.pop(name)
        except KeyError as error:
            raise TargetResolutionError(
                "The selected Pandrator MCP target does not exist.",
                details={"target": name},
            ) from error
        self.save(profiles.values())
        return removed

    def update_identity(
        self,
        name: str,
        identity: TargetIdentityExpectation,
    ) -> TargetProfile:
        profiles = {item.name: item for item in self.load(missing_ok=False)}
        try:
            current = profiles[name]
        except KeyError as error:
            raise TargetResolutionError(
                "The selected Pandrator MCP target does not exist.",
                details={"target": name},
            ) from error
        updated = current.model_copy(update={"expected_identity": identity})
        profiles[name] = updated
        self.save(profiles.values())
        return updated

    def configure_manager_recovery(
        self,
        name: str,
        *,
        origin: str,
        requested_scopes: tuple[str, ...],
        client_name: str,
        client_id: str | None = None,
    ) -> TargetProfile:
        """Add an unenrolled recovery endpoint without replacing the target."""

        profiles = {
            item.name: item for item in self.load(missing_ok=False)
        }
        try:
            current = profiles[name]
        except KeyError as error:
            raise TargetResolutionError(
                "The selected Pandrator MCP target does not exist.",
                details={"target": name},
            ) from error
        if current.manager_recovery_credential is not None:
            raise ValueError(
                "This target already has an enrolled Manager recovery "
                "credential. Revoke it before changing recovery identity."
            )
        identity = current.expected_identity.model_copy(
            update={"manager_instance_id": None}
        )
        updated = current.model_copy(
            update={
                "manager_recovery_origin": origin,
                "manager_automation_client_id": str(
                    client_id
                    or current.manager_automation_client_id
                    or current.automation_client_id
                    or uuid.uuid4()
                ),
                "manager_automation_client_name": client_name,
                "manager_requested_scopes": requested_scopes,
                "manager_enrolled_subject": None,
                "manager_credential_expires_at": None,
                "expected_identity": identity,
            }
        )
        profiles[name] = TargetProfile.model_validate(
            updated.model_dump(mode="python")
        )
        self.save(profiles.values())
        return profiles[name]

    def update_enrollment(
        self,
        name: str,
        *,
        identity: TargetIdentityExpectation,
        application_credential: CredentialReference,
        automation_client_id: str,
        requested_scopes: tuple[str, ...],
        enrolled_subject: str,
        credential_expires_at: str | None,
    ) -> TargetProfile:
        """Atomically persist only non-secret enrollment metadata."""

        profiles = {
            item.name: item for item in self.load(missing_ok=False)
        }
        try:
            current = profiles[name]
        except KeyError as error:
            raise TargetResolutionError(
                "The selected Pandrator MCP target does not exist.",
                details={"target": name},
            ) from error
        updated = current.model_copy(
            update={
                "expected_identity": identity,
                "application_credential": application_credential,
                "automation_client_id": automation_client_id,
                "requested_application_scopes": requested_scopes,
                "enrolled_subject": enrolled_subject,
                "credential_expires_at": credential_expires_at,
            }
        )
        profiles[name] = TargetProfile.model_validate(
            updated.model_dump(mode="python")
        )
        self.save(profiles.values())
        return profiles[name]

    def update_manager_enrollment(
        self,
        name: str,
        *,
        identity: TargetIdentityExpectation,
        manager_recovery_credential: CredentialReference,
        automation_client_id: str,
        requested_scopes: tuple[str, ...],
        enrolled_subject: str,
        credential_expires_at: str | None,
    ) -> TargetProfile:
        """Persist recovery enrollment metadata without its credential value."""

        profiles = {
            item.name: item for item in self.load(missing_ok=False)
        }
        try:
            current = profiles[name]
        except KeyError as error:
            raise TargetResolutionError(
                "The selected Pandrator MCP target does not exist.",
                details={"target": name},
            ) from error
        updated = current.model_copy(
            update={
                "expected_identity": identity,
                "manager_recovery_credential": (
                    manager_recovery_credential
                ),
                "manager_automation_client_id": automation_client_id,
                "manager_requested_scopes": requested_scopes,
                "manager_enrolled_subject": enrolled_subject,
                "manager_credential_expires_at": (
                    credential_expires_at
                ),
            }
        )
        profiles[name] = TargetProfile.model_validate(
            updated.model_dump(mode="python")
        )
        self.save(profiles.values())
        return profiles[name]

    def clear_enrollment(
        self,
        name: str,
        *,
        audience: Literal["application", "manager_recovery"],
    ) -> TargetProfile:
        """Forget one local credential handle without changing target trust."""

        profiles = {
            item.name: item for item in self.load(missing_ok=False)
        }
        try:
            current = profiles[name]
        except KeyError as error:
            raise TargetResolutionError(
                "The selected Pandrator MCP target does not exist.",
                details={"target": name},
            ) from error
        if audience == "application":
            changes = {
                "application_credential": None,
                "enrolled_subject": None,
                "credential_expires_at": None,
            }
        else:
            changes = {
                "manager_recovery_credential": None,
                "manager_enrolled_subject": None,
                "manager_credential_expires_at": None,
            }
        updated = current.model_copy(update=changes)
        profiles[name] = TargetProfile.model_validate(
            updated.model_dump(mode="python")
        )
        self.save(profiles.values())
        return profiles[name]
