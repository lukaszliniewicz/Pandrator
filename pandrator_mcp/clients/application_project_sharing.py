"""Validated fixed-route requests for project viewing grants."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from ..project_sharing_actions import PROJECT_SHARING_OPERATION_BY_ACTION
from .application_requests import ApplicationRequests


class ApplicationProjectSharingMethods(ApplicationRequests):
    def project_sharing_request(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        operation = PROJECT_SHARING_OPERATION_BY_ACTION.get(action)
        if operation is None:
            raise ValueError("Unknown project sharing action.")

        values = operation.model.model_validate(arguments).model_dump(mode="json")
        route = operation.path
        for field in re.findall(r"\{([^}]+)\}", route):
            route = route.replace("{" + field + "}", quote(str(values.pop(field)), safe=""))

        is_read = operation.method == "GET"
        parameters = values if is_read else None
        body = None if is_read else {"expected_revision": values["expected_revision"]}
        return self._request_json(
            route,
            method=operation.method,
            read_only_operation=is_read,
            parameters=parameters,
            body=body,
            maximum_body_bytes=8 * 1024,
        )


__all__ = ["ApplicationProjectSharingMethods"]
