"""Approved credential backends with non-serializable secret values."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .errors import CredentialResolutionError

APPROVED_CREDENTIAL_BACKENDS = frozenset({"environment", "keyring"})
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CredentialReference(BaseModel):
    """Non-secret handle persisted in a target profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: str = Field(min_length=1, max_length=40)
    reference: str = Field(min_length=1, max_length=512)
    audience: str = Field(pattern=r"^(application|manager_recovery)$")


class SecretValue:
    """A deliberately non-Pydantic wrapper that hides its value from repr."""

    __slots__ = ("__value",)

    def __init__(self, value: str):
        normalized = str(value)
        if not normalized:
            raise ValueError("A resolved credential cannot be empty.")
        self.__value = normalized

    def reveal(self) -> str:
        """Reveal only at the final authenticated HTTP boundary."""

        return self.__value

    def __repr__(self) -> str:
        return "SecretValue([REDACTED])"

    def __str__(self) -> str:
        return "[REDACTED]"


class CredentialBackend(Protocol):
    name: str

    def resolve(self, reference: CredentialReference) -> SecretValue: ...


@dataclass(slots=True)
class EnvironmentCredentialBackend:
    name: str = "environment"
    environment: Mapping[str, str] | None = None

    def resolve(self, reference: CredentialReference) -> SecretValue:
        if not _ENVIRONMENT_NAME.fullmatch(reference.reference):
            raise CredentialResolutionError(
                "The credential environment-variable reference is invalid."
            )
        environment = os.environ if self.environment is None else self.environment
        value = str(environment.get(reference.reference) or "")
        if not value:
            raise CredentialResolutionError(
                "The configured credential environment variable is unavailable.",
                details={"reference": reference.reference},
            )
        return SecretValue(value)

    def store(
        self,
        reference: CredentialReference,
        value: SecretValue,
    ) -> None:
        _ = (reference, value)
        raise CredentialResolutionError(
            "Generated credentials cannot be written into a parent process "
            "environment. Select the native keyring backend."
        )

    def delete(self, reference: CredentialReference) -> None:
        _ = reference
        raise CredentialResolutionError(
            "Environment credentials must be removed by their owner."
        )


@dataclass(slots=True)
class KeyringCredentialBackend:
    name: str = "keyring"
    service_name: str = "pandrator-mcp"

    def resolve(self, reference: CredentialReference) -> SecretValue:
        try:
            import keyring
        except ImportError as error:
            raise CredentialResolutionError(
                "The native credential-store extra is not installed."
            ) from error
        try:
            value = keyring.get_password(self.service_name, reference.reference)
        except Exception as error:
            raise CredentialResolutionError(
                "The native credential store could not be read."
            ) from error
        if not value:
            raise CredentialResolutionError(
                "The configured native credential is unavailable.",
                details={"reference": reference.reference},
            )
        return SecretValue(value)

    def store(
        self,
        reference: CredentialReference,
        value: SecretValue,
    ) -> None:
        try:
            import keyring
        except ImportError as error:
            raise CredentialResolutionError(
                "The native credential-store extra is not installed."
            ) from error
        try:
            keyring.set_password(
                self.service_name,
                reference.reference,
                value.reveal(),
            )
        except Exception as error:
            raise CredentialResolutionError(
                "The native credential store could not save the credential."
            ) from error

    def delete(self, reference: CredentialReference) -> None:
        try:
            import keyring
        except ImportError as error:
            raise CredentialResolutionError(
                "The native credential-store extra is not installed."
            ) from error
        try:
            keyring.delete_password(
                self.service_name,
                reference.reference,
            )
        except keyring.errors.PasswordDeleteError:
            # Logout and cleanup are intentionally retry-safe when a user or
            # credential manager already removed the native secret.
            return
        except Exception as error:
            raise CredentialResolutionError(
                "The native credential store could not remove the credential."
            ) from error


class CredentialResolver:
    """The sole boundary allowed to turn credential handles into secrets."""

    def __init__(
        self,
        backends: tuple[CredentialBackend, ...] | None = None,
    ) -> None:
        selected = backends or (
            EnvironmentCredentialBackend(),
            KeyringCredentialBackend(),
        )
        self._backends = {backend.name: backend for backend in selected}

    def resolve(
        self,
        reference: CredentialReference,
        *,
        audience: Literal["application", "manager_recovery"],
    ) -> SecretValue:
        if reference.audience != audience:
            raise CredentialResolutionError(
                "The credential audience does not match the downstream service.",
                details={
                    "configured_audience": reference.audience,
                    "required_audience": audience,
                },
            )
        if reference.backend not in APPROVED_CREDENTIAL_BACKENDS:
            raise CredentialResolutionError(
                "The target uses an unapproved credential backend.",
                details={"backend": reference.backend},
            )
        backend = self._backends.get(reference.backend)
        if backend is None:
            raise CredentialResolutionError(
                "The target's approved credential backend is not configured.",
                details={"backend": reference.backend},
            )
        return backend.resolve(reference)

    def store(
        self,
        reference: CredentialReference,
        value: SecretValue,
        *,
        audience: Literal["application", "manager_recovery"],
    ) -> None:
        """Persist a generated secret without exposing it as plain text."""

        if reference.audience != audience:
            raise CredentialResolutionError(
                "The credential audience does not match the downstream service."
            )
        if reference.backend not in APPROVED_CREDENTIAL_BACKENDS:
            raise CredentialResolutionError(
                "The target uses an unapproved credential backend."
            )
        backend = self._backends.get(reference.backend)
        store = getattr(backend, "store", None)
        if backend is None or store is None:
            raise CredentialResolutionError(
                "The selected credential backend is read-only."
            )
        store(reference, value)

    def delete(
        self,
        reference: CredentialReference,
        *,
        audience: Literal["application", "manager_recovery"],
    ) -> None:
        if reference.audience != audience:
            raise CredentialResolutionError(
                "The credential audience does not match the downstream service."
            )
        backend = self._backends.get(reference.backend)
        delete = getattr(backend, "delete", None)
        if backend is None or delete is None:
            raise CredentialResolutionError(
                "The selected credential backend cannot remove credentials."
            )
        delete(reference)
