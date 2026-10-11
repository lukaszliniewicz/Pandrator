"""OpenAPI additions for durable purge maintenance."""

from .session_purge_job_schemas import SESSION_PURGE_JOB_SCHEMAS


def session_purge_job_paths() -> dict:
    session = {"name": "sessionId", "in": "path", "required": True,
               "schema": {"type": "string"}}
    job = {"name": "jobId", "in": "path", "required": True,
           "schema": {"type": "string"}}
    key = {"name": "Idempotency-Key", "in": "header", "required": True,
           "schema": {"type": "string", "minLength": 8, "maxLength": 200}}

    def operation(name: str, scope: str, params: list[dict], status: str,
                  schema: str | None = None) -> dict:
        response_schema = "PurgeJobReceipt" if schema else "PurgeInspectionStatus"
        success_response = {"description": "Durable purge work or inspection result.",
                            "content": {"application/json": {"schema": {
                                "$ref": f"#/components/schemas/{response_schema}"}}}}
        result = {
            "operationId": name,
            "summary": name,
            "security": [{"cookieAuth": []}, {"bearerToken": []},
                         {"nativeOAuth": [scope]}],
            "parameters": params,
            "responses": {status: success_response,
                          "400": {"description": "Invalid request or missing idempotency key."},
                          "401": {"description": "Authentication required."},
                          "403": {"description": "Insufficient scope."},
                          "404": {"description": "Target or inspection not found."},
                          "409": {"description": "Revision, idempotency, or purge state conflict."}},
        }
        if schema:
            result["requestBody"] = {"required": True, "content": {"application/json": {
                "schema": {"$ref": f"#/components/schemas/{schema}"}}}}
        else:
            result["responses"]["202"] = success_response
        return result

    return {
        "/api/v1/sessions/{sessionId}/purge-inspections": {
            "post": operation("admitSessionPurgeInspection", "app.read", [session, key],
                              "202", "PurgeInspectionRequest")},
        "/api/v1/sessions/{sessionId}/purge-admissions": {
            "post": operation("admitSessionPurge", "app.write", [session, key],
                              "202", "PurgeAdmissionRequest")},
        "/api/v1/purge-inspections/{jobId}": {
            "get": operation("getPurgeInspection", "app.read", [job], "200")},
    }


__all__ = ["SESSION_PURGE_JOB_SCHEMAS", "session_purge_job_paths"]
