"""Current Administrator authorization for explicit privileged mutations."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import cast

from ..database import Database
from .actor_authorization import ActorAuthorizationDenied, require_actor_in_session
from .auth_contracts import normalize_scopes
from .auth_state import AccountState
from .execution_actor import current_execution_actor
from .job_authorization import require_execution_actor_in_session

_ADMINISTRATOR_REQUIRED = "An active Administrator credential with the required scopes is required."


class AdministratorRequired(ActorAuthorizationDenied):
    """A scoped administrative operation lacks current delegated authority."""

    def __init__(self) -> None:
        super().__init__(_ADMINISTRATOR_REQUIRED)


PRIVILEGED_JOB_SCOPES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "rvc.model.upload": frozenset({"app.admin", "app.write"}),
        "training.xtts": frozenset({"app.admin", "app.run"}),
    }
)


def _requested_scope_set(value: object) -> frozenset[str]:
    if isinstance(value, str):
        if not value.strip():
            raise AdministratorRequired()
        values: object = value
    elif type(value) in (list, tuple, set, frozenset):
        entries = list(cast(Iterable[object], value))
        if any(type(scope) is not str or not scope.strip() for scope in entries):
            raise AdministratorRequired()
        values = entries
    else:
        raise AdministratorRequired()
    try:
        return normalize_scopes(values, allow_empty=True)
    except ValueError as error:
        raise AdministratorRequired() from error


def require_administrator_actor(
    database: Database,
    *,
    required_scopes: object = (),
    allow_host: bool = False,
) -> AccountState | None:
    """Require current Administrator role and explicitly held credential scopes."""
    if type(allow_host) is not bool:
        raise AdministratorRequired()
    scopes = _requested_scope_set(required_scopes) | frozenset({"app.admin"})
    try:
        with database.immediate_session() as session:
            return require_actor_in_session(
                session,
                current_execution_actor.get(),
                required_scopes=scopes,
                require_admin=True,
                allow_host=allow_host,
            )
    except PermissionError:
        raise AdministratorRequired() from None


def require_execution_administrator_actor(
    database: Database,
    *,
    required_scopes: object = (),
    allow_host: bool = False,
) -> AccountState | None:
    """Require Administrator authority, using a proved worker lease if present."""
    if type(allow_host) is not bool:
        raise AdministratorRequired()
    scopes = _requested_scope_set(required_scopes) | frozenset({"app.admin"})
    try:
        with database.immediate_session() as session:
            return require_execution_actor_in_session(
                session,
                current_execution_actor.get(),
                required_scopes=scopes,
                require_admin=True,
                allow_host=allow_host,
            )
    except PermissionError:
        raise AdministratorRequired() from None


__all__ = [
    "AdministratorRequired",
    "PRIVILEGED_JOB_SCOPES",
    "require_administrator_actor",
    "require_execution_administrator_actor",
]
