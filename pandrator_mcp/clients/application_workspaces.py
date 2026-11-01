"""Validated fixed-route requests for workspace collaboration."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from ..workspace_actions import WORKSPACE_OPERATION_BY_ACTION
from .application_requests import ApplicationRequests


class ApplicationWorkspaceMethods(ApplicationRequests):
    def workspace_request(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        operation = WORKSPACE_OPERATION_BY_ACTION.get(action)
        if operation is None:
            raise ValueError("Unknown workspace action.")
        values = operation.model.model_validate(arguments).model_dump(mode="json")
        route = operation.path
        for field in re.findall(r"\{([^}]+)\}", route):
            route = route.replace("{" + field + "}", quote(str(values.pop(field)), safe=""))
        key = values.pop("idempotency_key", None)
        parameters = None
        if operation.method == "GET":
            parameters = values
            if "include_archived" in values:
                parameters["include_archived"] = "true" if values["include_archived"] else "false"
            if "query" in parameters:
                parameters["q"] = parameters.pop("query")
        return self._request_json(
            route, method=operation.method,
            read_only_operation=operation.method == "GET",
            parameters=parameters,
            body=values if operation.method != "GET" else None,
            idempotency_key=key, maximum_body_bytes=16 * 1024,
        )
