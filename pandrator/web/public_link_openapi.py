"""OpenAPI paths for the bounded public-link HTTP adapter."""

from __future__ import annotations

from .public_link_schemas import PUBLIC_LINK_SCHEMAS as PUBLIC_LINK_SCHEMAS


def public_link_paths() -> dict:
    def path_id(name: str) -> dict:
        return {
            "name": name,
            "in": "path",
            "required": True,
            "schema": {"type": "string", "format": "uuid"},
        }

    def query(name: str, *, minimum: int, maximum: int | None = None, default: int | None = None):
        schema = {"type": "integer", "minimum": minimum}
        if maximum is not None:
            schema["maximum"] = maximum
        if default is not None:
            schema["default"] = default
        return {"name": name, "in": "query", "schema": schema}

    def operation(
        operation_id: str,
        scopes: list[str] | None,
        response_schema: str,
        *,
        request_schema: str | None = None,
        parameters: list[dict] | None = None,
        description: str = "",
        max_body_bytes: int | None = None,
        responses: dict | None = None,
        visitor: bool = False,
    ) -> dict:
        result = {
            "operationId": operation_id,
            "description": description,
            "security": (
                [
                    {"cookieAuth": []},
                    {"bearerToken": []},
                    {"nativeOAuth": scopes},
                ]
                if scopes is not None
                else []
            ),
            "parameters": parameters or [],
            "responses": {
                "200": {
                    "description": "Current bounded result",
                    "content": {
                        "application/json": {
                            "schema": {"$ref": f"#/components/schemas/{response_schema}"}
                        }
                    },
                },
                "401": {"description": "Authentication required"},
                "403": {"description": "Current resource authority required"},
                "404": {"description": "Unavailable or not found"},
                "409": {"description": "Current revision or source conflict"},
                "413": {"description": "Request body exceeds its bounded limit"},
                "422": {"description": "Invalid bounded request"},
            },
            "x-required-scopes": scopes or [],
        }
        if request_schema is not None:
            result["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": f"#/components/schemas/{request_schema}"}
                    }
                },
            }
        if max_body_bytes is not None:
            result["x-max-content-length"] = max_body_bytes
        if responses:
            result["responses"].update(responses)
        if visitor:
            result["security"] = [{"projectLinkSession": []}]
            result["parameters"].append(
                {
                    "name": "X-Pandrator-Project-Link",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "format": "uuid"},
                    "description": "Select the matching HttpOnly visitor cookie; this ID alone grants no access.",
                }
            )
            result["x-project-link-cookie"] = "pandrator_link_<linkId without hyphens>"
            if request_schema is not None or operation_id == "leaveProjectLink":
                result["parameters"].append(
                    {
                        "name": "X-CSRF-Token",
                        "in": "header",
                        "required": True,
                        "schema": {"type": "string"},
                        "description": "The CSRF token from this visitor session's redemption response.",
                    }
                )
        return result

    path = "/api/v1/admin/public-sharing"
    paths = {
        path: {
            "get": operation(
                "getPublicSharingSettings",
                ["app.admin"],
                "PublicSharingSettingsResponse",
                description="Read public-sharing configuration and effective hosting boundaries.",
            ),
            "put": operation(
                "configurePublicSharing",
                ["app.admin"],
                "PublicSharingSettingsResponse",
                request_schema="PublicSharingSettingsRequest",
                description="Change link-sharing configuration. This does not mutate listener or proxy configuration.",
                max_body_bytes=8192,
            ),
        },
        "/api/v1/projects/{projectId}/links": {
            "get": operation(
                "listProjectLinks",
                ["app.read"],
                "ProjectLinkListResponse",
                parameters=[path_id("projectId")],
                description="List links only when the current principal has project-link governance authority.",
            ),
            "post": operation(
                "createProjectLink",
                ["app.read", "app.write"],
                "ProjectLinkCreateResponse",
                request_schema="ProjectLinkCreateRequest",
                parameters=[path_id("projectId")],
                description="Create a project capability. The returned bearer URL is shown only at creation; a repeated request returns no URL.",
                max_body_bytes=8192,
            ),
        },
        "/api/v1/project-links/{linkId}/revoke": {
            "post": operation(
                "revokeProjectLink",
                ["app.read", "app.write"],
                "ProjectLinkResponse",
                request_schema="ProjectLinkRevokeRequest",
                parameters=[path_id("linkId")],
                description="Revoke the project link and its associated browser sessions at the expected revision.",
                max_body_bytes=8192,
            )
        },
        "/api/v1/public-links/{linkId}/redeem": {
            "post": operation(
                "redeemProjectLink",
                None,
                "PublicLinkRedeemResponse",
                request_schema="PublicLinkRedeemRequest",
                parameters=[path_id("linkId")],
                description="Exchange a bearer secret for a link-bound HttpOnly cookie. The raw browser credential is never returned in JSON.",
                max_body_bytes=4096,
                responses={
                    "429": {"description": "Per-client redemption backoff; see Retry-After"},
                },
            )
        },
        "/api/v1/public-links/{linkId}/review": {
            "get": operation(
                "getPublicTranslationReview",
                None,
                "PublicTranslationReviewResponse",
                visitor=True,
                parameters=[
                    path_id("linkId"),
                    query("offset", minimum=0, default=0),
                    query("limit", minimum=1, maximum=100, default=50),
                ],
                description="Read the pinned translation through the matching link-bound browser cookie. This operation never falls back to an account session.",
            )
        },
        "/api/v1/public-links/{linkId}/submissions": {
            "post": operation(
                "submitPublicTranslationReview",
                None,
                "PublicTranslationSubmissionResponse",
                visitor=True,
                request_schema="PublicTranslationSubmissionRequest",
                parameters=[
                    path_id("linkId"),
                    {
                        "name": "Idempotency-Key",
                        "in": "header",
                        "required": True,
                        "schema": {"type": "string", "format": "uuid"},
                    },
                ],
                description="Save bounded text corrections through the matching link-bound cookie. The reviewer label is supplied attribution, not verified identity.",
                max_body_bytes=262144,
            )
        },
        "/api/v1/public-links/{linkId}/logout": {
            "post": operation(
                "leaveProjectLink",
                None,
                "PublicLinkRedeemResponse",
                visitor=True,
                parameters=[path_id("linkId")],
                description="Revoke only this link visitor session and clear its cookie. An ordinary account session is preserved.",
            ),
        },
        "/api/v1/public-links/{linkId}/services/{kind}": {
            "get": operation(
                "getProjectLinkServices",
                None,
                "ProjectLinkServicesResponse",
                visitor=True,
                parameters=[
                    path_id("linkId"),
                    {
                        "name": "kind",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string", "enum": ["tts", "stt"]},
                    },
                    {
                        "name": "session_id",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "string", "format": "uuid"},
                    },
                ],
                description="Project-management links only: read the current project language's configured service and model without provider discovery or workspace library access.",
            ),
        },
        "/api/v1/projects/{projectId}/link-submissions": {
            "get": operation(
                "listLinkSubmissions",
                ["app.read", "app.write"],
                "LinkSubmissionsResponse",
                parameters=[
                    path_id("projectId"),
                    {
                        "name": "status",
                        "in": "query",
                        "schema": {
                            "type": "string",
                            "enum": ["pending", "accepted", "rejected"],
                            "default": "pending",
                        },
                    },
                    query("offset", minimum=0, default=0),
                    query("limit", minimum=1, maximum=100, default=25),
                ],
                description="List review submissions using ordinary project review authority; instance administration does not grant project access.",
            )
        },
        "/api/v1/link-submissions/{submissionId}": {
            "get": operation(
                "getLinkSubmission",
                ["app.read", "app.write"],
                "LinkSubmissionDetailResponse",
                parameters=[
                    path_id("submissionId"),
                    query("offset", minimum=0, default=0),
                    query("limit", minimum=1, maximum=100, default=50),
                ],
                description="Read a submission and its before/after text using ordinary project review authority.",
            )
        },
        "/api/v1/link-submissions/{submissionId}/accept": {
            "post": operation(
                "acceptLinkSubmission",
                ["app.read", "app.write"],
                "LinkSubmissionSummary",
                request_schema="SubmissionAcceptRequest",
                parameters=[
                    path_id("submissionId"),
                    {
                        "name": "Idempotency-Key",
                        "in": "header",
                        "required": True,
                        "schema": {"type": "string", "format": "uuid"},
                    },
                ],
                description="Accept at the expected source and document revisions through the immutable subtitle-review publication path.",
                max_body_bytes=8192,
            )
        },
        "/api/v1/link-submissions/{submissionId}/reject": {
            "post": operation(
                "rejectLinkSubmission",
                ["app.read", "app.write"],
                "LinkSubmissionSummary",
                request_schema="SubmissionRejectRequest",
                parameters=[
                    path_id("submissionId"),
                    {
                        "name": "Idempotency-Key",
                        "in": "header",
                        "required": True,
                        "schema": {"type": "string", "format": "uuid"},
                    },
                ],
                description="Reject a pending submission at its expected revision.",
                max_body_bytes=8192,
            )
        },
    }
    logout = paths["/api/v1/public-links/{linkId}/logout"]["post"]["responses"]
    del logout["200"]
    logout["204"] = {"description": "Visitor session revoked and cookie removed"}
    return paths
