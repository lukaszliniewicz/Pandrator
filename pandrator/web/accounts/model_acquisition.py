"""Bind missing-model acquisition to the current request or durable job actor."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from pandrator.logic.model_acquisition import model_acquisition_scope

from ..database import Database
from .administration import AdministratorRequired, require_execution_administrator_actor
from .execution_actor import (
    ExecutionActor,
    HostActor,
    UserActor,
    current_execution_actor,
    execution_actor_scope,
)
from .job_authorization import require_execution_actor_in_session


def may_acquire_models(database: Database, actor: ExecutionActor | None) -> bool:
    """Whether a run by ``actor`` would pass the acquisition gate below.

    Plans and readiness checks use it to say "will download" only to someone
    whose run may actually download: an Administrator, or the host.
    """

    try:
        with execution_actor_scope(actor):
            require_execution_administrator_actor(database, allow_host=True)
    except AdministratorRequired:
        return False
    return True


@contextmanager
def model_acquisition_for_actor(database: Database) -> Iterator[None]:
    """Revalidate actual credentials for every acquisition, including host jobs."""

    def authorize() -> None:
        require_execution_administrator_actor(database, allow_host=True)

    def authorize_inference() -> bool:
        actor = current_execution_actor.get()
        with database.immediate_session() as session:
            account = require_execution_actor_in_session(
                session,
                actor,
                required_scopes=frozenset({"app.run"}),
                allow_host=True,
            )
        if account is None:
            return type(actor) is HostActor
        return (
            type(actor) is UserActor
            and account.is_instance_admin is True
            and "app.admin" in actor.scopes
        )

    with model_acquisition_scope(authorize, inference_authorizer=authorize_inference):
        yield
