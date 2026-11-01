"""Explicit content guards before route parsing, idempotency, and body reads."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from .access.contracts import ProjectAction
from .access.generation_resource_policy import (
    GenerationResource,
    require_generation_resource_access,
)
from .route_context import RouteContext

P = ParamSpec("P")
R = TypeVar("R")


def require_session_action(
    context: RouteContext,
    action: ProjectAction,
    *,
    resource: GenerationResource = "session",
) -> Callable[[Callable[P, R]], Callable[P, Any]]:
    """Use after require_auth; persistence leaves recheck in their own unit."""

    def decorate(view: Callable[P, R]) -> Callable[P, Any]:
        @wraps(view)
        def guarded(*args: P.args, **kwargs: P.kwargs) -> Any:
            identifier = kwargs.get(f"{resource}_id")
            if not isinstance(identifier, str):
                raise TypeError("An owning resource identifier is required.")
            try:
                with context.services.database.snapshot_session() as session:
                    require_generation_resource_access(session, resource, identifier, action)
            except KeyError:
                return context.guards.error_response("not_found", "Session not found.", 404)
            return view(*args, **kwargs)

        return guarded

    return decorate
