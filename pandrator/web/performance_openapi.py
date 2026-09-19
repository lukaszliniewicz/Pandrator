"""Documented, scope-aware performance planning API for UI and MCP clients."""

from . import performance_schemas as s

PERFORMANCE_SCHEMAS = {
    model.__name__: model
    for model in (
        s.PerformancePlanCreateRequest,
        s.PerformanceEditRequest,
        s.PerformanceAdoptRequest,
        s.PerformanceLeaseRequest,
        s.PerformanceRenewRequest,
        s.PerformanceSubmitRequest,
        s.PerformancePreviewRequest,
    )
}


def performance_paths() -> dict:
    base = "/api/v1/sessions/{sessionId}/performance-plans"
    rows = (
        ("", "get", "listPerformancePlans", None, "app.read", "200"),
        (
            "",
            "post",
            "createPerformancePlan",
            "PerformancePlanCreateRequest",
            "app.run",
            "201",
        ),
        ("/{planId}", "get", "getPerformancePlan", None, "app.read", "200"),
        (
            "/{planId}",
            "patch",
            "editPerformancePlan",
            "PerformanceEditRequest",
            "app.write",
            "200",
        ),
        (
            "/{planId}/adopt",
            "post",
            "adoptPerformancePlan",
            "PerformanceAdoptRequest",
            "app.write",
            "200",
        ),
        ("/{planId}/analyse", "post", "analysePerformancePlan", None, "app.run", "202"),
        (
            "/{planId}/preview",
            "post",
            "previewPerformancePlan",
            "PerformancePreviewRequest",
            "app.read",
            "200",
        ),
        (
            "/{planId}/claim",
            "post",
            "claimPerformanceBatch",
            "PerformanceLeaseRequest",
            "app.run",
            "200",
        ),
        (
            "/{planId}/batches/{batchId}/submit",
            "post",
            "submitPerformanceBatch",
            "PerformanceSubmitRequest",
            "app.run",
            "200",
        ),
        (
            "/{planId}/batches/{batchId}/renew",
            "post",
            "renewPerformanceBatch",
            "PerformanceRenewRequest",
            "app.run",
            "200",
        ),
        (
            "/{planId}/batches/{batchId}/release",
            "post",
            "releasePerformanceBatch",
            "PerformanceRenewRequest",
            "app.run",
            "200",
        ),
    )
    result = {}
    for suffix, method, name, schema, scope, status in rows:
        parameters = []
        if scope != "app.read":
            parameters.append(
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "minLength": 8, "maxLength": 200},
                }
            )
        if method == "get":
            parameters.extend(
                [
                    {
                        "name": "offset",
                        "in": "query",
                        "schema": {"type": "integer", "minimum": 0, "default": 0},
                    },
                    {
                        "name": "limit",
                        "in": "query",
                        "schema": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100,
                            "default": 50,
                        },
                    },
                ]
            )
            if not suffix:
                parameters.append(
                    {
                        "name": "plan_revision_id",
                        "in": "query",
                        "schema": {"type": "string", "maxLength": 80},
                    }
                )
        operation = {
            "operationId": name,
            "summary": name,
            "description": "Performance metadata never rewrites accepted speech text or creates audio. Adoption affects future synthesis only. Leased batches contain read-only semantic context.",
            "security": [
                {"cookieAuth": []},
                {"bearerToken": []},
                {"nativeOAuth": [scope]},
            ],
            "parameters": parameters,
            "responses": {
                status: {
                    "description": "Validated performance state or compiled request; never audio.",
                    "content": {
                        "application/json": {
                            "schema": {"type": "object", "additionalProperties": True}
                        }
                    },
                },
                "400": {
                    "description": "A valid idempotency key is required for writes."
                },
                "401": {"description": "Authentication required."},
                "403": {
                    "description": "The authenticated actor lacks the required scope."
                },
                "404": {
                    "description": "The requested session, performance plan or batch does not exist."
                },
                "409": {
                    "description": "Stale source/version, invalid lease, active work, or immutable adopted plan."
                },
                "422": {
                    "description": "Invalid performance annotation, anchor or request."
                },
            },
        }
        if schema:
            operation["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": f"#/components/schemas/{schema}"}
                    }
                },
            }
        result.setdefault(base + suffix, {})[method] = operation
    return result
