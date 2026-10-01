"""Contracts for bounded workflow inspections and guarded controls."""

from .schemas import ReviewSplitInspection, WorkflowInputSelection

WORKFLOW_IMPROVEMENT_SCHEMAS = {
    "ReviewSplitInspection": ReviewSplitInspection,
    "WorkflowInputSelection": WorkflowInputSelection,
}


def workflow_improvements_paths():
    def operation(identifier, scope, *, model=None, write=False, parameters=None):
        result = {
            "operationId": identifier,
            "security": [{"cookieAuth": []}, {"bearerToken": []}, {"nativeOAuth": [scope]}],
            "responses": {
                "200": {"description": "Bounded workflow result"},
                "404": {"description": "Resource not found"},
                "409": {"description": "Revision or lifecycle conflict"},
                "422": {"description": "Invalid request"},
            },
        }
        result["parameters"] = list(parameters or [])
        if write:
            result["parameters"].append({
                "name": "Idempotency-Key", "in": "header", "required": True,
                "schema": {"type": "string", "minLength": 8, "maxLength": 200},
            })
        if model:
            result["requestBody"] = {"required": True, "content": {
                "application/json": {"schema": {"$ref": f"#/components/schemas/{model}"}}
            }}
        return result

    def query(name, schema):
        return {"name": name, "in": "query", "schema": schema}

    return {
        "/api/v1/sessions/{sessionId}/workflow-inputs": {
            "get": operation("getWorkflowInputs", "app.read"),
            "put": operation("selectWorkflowInput", "app.write", model="WorkflowInputSelection", write=True),
        },
        "/api/v1/subtitle-evidence/routes": {
            "get": operation("listSubtitleEvidenceRoutes", "app.read", parameters=[
                query("language", {"type": "string", "maxLength": 40}),
                query("include_languages", {"type": "boolean", "default": False}),
            ]),
        },
        "/api/v1/sessions/{sessionId}/subtitles/split-boundaries": {
            "post": operation("inspectReviewSplitBoundaries", "app.read", model="ReviewSplitInspection"),
        },
        "/api/v1/dispatch-runs/{runId}/terminate": {
            "post": operation("terminateDispatchRun", "app.run", model="DispatchRunTerminationRequest", write=True),
        },
        "/api/v1/dispatch-runs/{runId}/preview": {
            "get": operation("getDispatchPreview", "app.read", parameters=[
                query("batch_ordinal", {"type": "integer", "minimum": 1}),
                query("offset", {"type": "integer", "minimum": 0, "default": 0}),
                query("limit", {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}),
            ]),
        },
    }
