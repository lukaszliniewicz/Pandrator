"""Project-authorized ledger reads and explicit, atomic editorial writes."""

from __future__ import annotations

import re
from functools import wraps
from typing import Any, Callable

from flask import g, jsonify, request
from pydantic import ValidationError

from .access.session_policy import SessionNotFound
from .domain_blueprints import DomainBlueprints
from .knowledge import KnowledgeValidationError
from .knowledge_access import (
    KnowledgeLedgerNotFound,
    KnowledgeLedgerRevisionConflict,
    ensure_ledger,
    read_ledger,
    replace_ledger,
)
from .knowledge_schemas import (
    KnowledgeLedgerResponse,
    KnowledgeScope,
    KnowledgeUpdateRequest,
    KnowledgeUpdateResponse,
)
from .route_context import RouteContext


def register_knowledge_routes(app: DomainBlueprints, context: RouteContext) -> None:
    services, guards = context.services, context.guards

    def errors(function: Callable[..., Any]):
        @wraps(function)
        def wrapped(*args: Any, **kwargs: Any):
            try:
                return function(*args, **kwargs)
            except (KnowledgeLedgerNotFound, SessionNotFound):
                return guards.error_response("not_found", "Knowledge ledger not found.", 404)
            except KnowledgeLedgerRevisionConflict:
                return guards.error_response(
                    "revision_conflict",
                    "The knowledge ledger changed in another client.",
                    409,
                )
            except ValidationError:
                raise
            except (KnowledgeValidationError, ValueError):
                return guards.error_response(
                    "validation_error",
                    "The knowledge ledger request is invalid.",
                    422,
                )

        return wrapped

    @app.get("/api/v1/sessions/<session_id>/knowledge/<kind>")
    @guards.require_scope("app.read")
    @errors
    def knowledge_get(session_id: str, kind: str):
        scope = KnowledgeScope.model_validate(request.args.to_dict())
        with services.database.snapshot_session() as session:
            result = read_ledger(
                session, guards.access_context(), session_id, kind, **scope.model_dump()
            )
            return jsonify(KnowledgeLedgerResponse.model_validate(result).model_dump(mode="json"))

    @app.post("/api/v1/sessions/<session_id>/knowledge/<kind>")
    @guards.require_scope("app.write")
    @errors
    def knowledge_initialize(session_id: str, kind: str):
        request.max_content_length = 8 * 1024
        body = {} if not request.is_json and not request.get_data() else request.get_json()
        scope = KnowledgeScope.model_validate(body)
        access = guards.access_context()
        with services.database.immediate_session() as session:
            result, created = ensure_ledger(session, access, session_id, kind, **scope.model_dump())
            if created:
                services.audit.record_command_in_session(
                    session,
                    access,
                    action="knowledge.create",
                    resource_kind="knowledge_ledger",
                    workspace_id=result["id"],
                    metadata={"revision": result["revision"]},
                )
            g.audit_resource_kind, g.audit_resource_id = "knowledge_ledger", result["id"]
            return jsonify(
                KnowledgeLedgerResponse.model_validate(result).model_dump(mode="json")
            ), 201 if created else 200

    @app.patch("/api/v1/knowledge/<ledger_id>")
    @guards.require_scope("app.write")
    @errors
    def knowledge_update(ledger_id: str):
        request.max_content_length = 4 * 1024 * 1024
        precondition = re.fullmatch(
            r'(?:"([1-9][0-9]{0,9})"|([1-9][0-9]{0,9}))',
            request.headers.get("If-Match", "").strip(),
        )
        if precondition is None:
            return guards.error_response(
                "precondition_required",
                "If-Match must contain the current ledger revision.",
                428,
            )
        revision = int(precondition.group(1) or precondition.group(2))
        payload = KnowledgeUpdateRequest.model_validate(request.get_json())
        access = guards.access_context()
        with services.database.immediate_session() as session:
            result = replace_ledger(
                session, access, ledger_id, expected_revision=revision, payload=payload.payload
            )
            services.audit.record_command_in_session(
                session,
                access,
                action="knowledge.update",
                resource_kind="knowledge_ledger",
                workspace_id=ledger_id,
                metadata={"revision": result["revision"], "expected_revision": revision},
            )
            g.audit_resource_kind, g.audit_resource_id = "knowledge_ledger", ledger_id
            response = jsonify(
                KnowledgeUpdateResponse.model_validate(result).model_dump(mode="json")
            )
            response.headers["ETag"] = f'"{result["revision"]}"'
            return response
