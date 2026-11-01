"""Validated, context-local provenance for synchronous execution paths."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias, cast

from .auth_contracts import PRINCIPAL_KINDS, PrincipalKind, normalize_scopes

_MANAGER_BROWSER_CLIENT_IDS = frozenset({"pandrator-manager-browser", "pandrator-mcp-local"})
_USER_CONTEXT_KEYS = frozenset(
    {
        "version",
        "source",
        "subject",
        "principal_kind",
        "scopes",
        "token_id",
        "browser_session_id",
        "client_id",
    }
)
_HOST_CONTEXT_KEYS = frozenset({"version", "source", "label"})


def _invalid_actor() -> ValueError:
    return ValueError("The execution actor is invalid.")


def _identifier(value: object, *, allow_manager_browser_id: bool = False) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise _invalid_actor()
    if len(value) <= 36:
        return value
    if allow_manager_browser_id and value in _MANAGER_BROWSER_CLIENT_IDS:
        return value
    raise _invalid_actor()


def _scope_set(value: object, *, allow_empty: bool = False) -> frozenset[str]:
    if type(value) not in (frozenset, set, tuple, list):
        raise _invalid_actor()
    values = list(cast(Iterable[object], value))
    if any(type(scope) is not str or not scope or scope != scope.strip() for scope in values):
        raise _invalid_actor()
    if len(values) != len(set(values)):
        raise _invalid_actor()
    try:
        return normalize_scopes(values, allow_empty=allow_empty)
    except ValueError as error:
        raise _invalid_actor() from error


def _validate_user_actor(actor: UserActor) -> None:
    _identifier(actor.user_id)
    if type(actor.auth_revision) is not int or actor.auth_revision < 1:
        raise _invalid_actor()
    if type(actor.subject) is not str or not actor.subject.strip() or len(actor.subject) > 200:
        raise _invalid_actor()
    if type(actor.principal_kind) is not str or actor.principal_kind not in PRINCIPAL_KINDS:
        raise _invalid_actor()
    if type(actor.scopes) is not frozenset:
        raise _invalid_actor()
    scopes = _scope_set(actor.scopes)
    if scopes != actor.scopes:
        raise _invalid_actor()
    if actor.principal_kind == "manager_bootstrap" and "app.admin" in scopes:
        raise _invalid_actor()

    has_token_id = actor.token_id is not None
    has_browser_session_id = actor.browser_session_id is not None
    if has_token_id == has_browser_session_id:
        raise _invalid_actor()
    if has_token_id:
        _identifier(actor.token_id)
    if has_browser_session_id:
        _identifier(actor.browser_session_id)
    if actor.client_id is not None:
        _identifier(
            actor.client_id,
            allow_manager_browser_id=has_browser_session_id,
        )


@dataclass(frozen=True, slots=True)
class UserActor:
    user_id: str
    auth_revision: int
    subject: str
    principal_kind: PrincipalKind
    scopes: frozenset[str]
    token_id: str | None = None
    browser_session_id: str | None = None
    client_id: str | None = None

    def __post_init__(self) -> None:
        _validate_user_actor(self)


@dataclass(frozen=True, slots=True)
class HostActor:
    label: str

    def __post_init__(self) -> None:
        if type(self.label) is not str or self.label != "pandrator-cli":
            raise _invalid_actor()


ExecutionActor: TypeAlias = UserActor | HostActor


class PrincipalProtocol(Protocol):
    @property
    def subject(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def scopes(self) -> object: ...

    @property
    def token_id(self) -> str | None: ...

    @property
    def browser_session_id(self) -> str | None: ...

    @property
    def client_id(self) -> str | None: ...

    @property
    def user_id(self) -> str | None: ...

    @property
    def auth_revision(self) -> int | None: ...


current_execution_actor: ContextVar[ExecutionActor | None] = ContextVar(
    "pandrator_execution_actor", default=None
)


@contextmanager
def execution_actor_scope(actor: ExecutionActor | None) -> Iterator[None]:
    """Bind even ``None`` for this scope, then restore the previous context."""
    token: Token[ExecutionActor | None] = current_execution_actor.set(actor)
    try:
        yield
    finally:
        current_execution_actor.reset(token)


def actor_from_principal(principal: PrincipalProtocol) -> UserActor | None:
    """Snapshot only the actual resolved account principal, never payload fields."""
    try:
        user_id = principal.user_id
        auth_revision = principal.auth_revision
    except (AttributeError, TypeError) as error:
        raise _invalid_actor() from error
    if user_id is None and auth_revision is None:
        return None
    if user_id is None or auth_revision is None:
        raise _invalid_actor()
    try:
        return UserActor(
            user_id=user_id,
            auth_revision=auth_revision,
            subject=principal.subject,
            principal_kind=cast(PrincipalKind, principal.kind),
            scopes=_scope_set(principal.scopes),
            token_id=principal.token_id,
            browser_session_id=principal.browser_session_id,
            client_id=principal.client_id,
        )
    except (AttributeError, TypeError) as error:
        raise _invalid_actor() from error


def actor_to_storage(actor: ExecutionActor | None) -> dict[str, Any] | None:
    if actor is None:
        return None
    if type(actor) is HostActor:
        if actor.label != "pandrator-cli":
            raise _invalid_actor()
        return {
            "user_id": None,
            "auth_revision": None,
            "context_json": {"version": 1, "source": "host", "label": actor.label},
        }
    if type(actor) is not UserActor:
        raise _invalid_actor()
    _validate_user_actor(actor)
    return {
        "user_id": actor.user_id,
        "auth_revision": actor.auth_revision,
        "context_json": {
            "version": 1,
            "source": "user",
            "subject": actor.subject,
            "principal_kind": actor.principal_kind,
            "scopes": sorted(actor.scopes),
            "token_id": actor.token_id,
            "browser_session_id": actor.browser_session_id,
            "client_id": actor.client_id,
        },
    }


def actor_from_storage(
    user_id: object,
    auth_revision: object,
    context_json: object,
) -> ExecutionActor | None:
    if user_id is None and auth_revision is None and context_json is None:
        return None
    if type(context_json) is not dict:
        raise _invalid_actor()
    context = context_json
    if type(context.get("version")) is not int or context.get("version") != 1:
        raise _invalid_actor()
    source = context.get("source")
    if source == "host":
        if (
            context.keys() != _HOST_CONTEXT_KEYS
            or user_id is not None
            or auth_revision is not None
            or context.get("label") != "pandrator-cli"
        ):
            raise _invalid_actor()
        return HostActor("pandrator-cli")
    if source != "user" or context.keys() != _USER_CONTEXT_KEYS:
        raise _invalid_actor()
    if type(auth_revision) is not int or type(user_id) is not str:
        raise _invalid_actor()
    scopes_value = context.get("scopes")
    if type(scopes_value) is not list:
        raise _invalid_actor()
    scopes = _scope_set(scopes_value)
    if scopes_value != sorted(scopes):
        raise _invalid_actor()
    actor = UserActor(
        user_id=user_id,
        auth_revision=auth_revision,
        subject=cast(str, context.get("subject")),
        principal_kind=cast(PrincipalKind, context.get("principal_kind")),
        scopes=scopes,
        token_id=context.get("token_id"),
        browser_session_id=context.get("browser_session_id"),
        client_id=context.get("client_id"),
    )
    if actor_to_storage(actor) != {
        "user_id": user_id,
        "auth_revision": auth_revision,
        "context_json": context,
    }:
        raise _invalid_actor()
    return actor
