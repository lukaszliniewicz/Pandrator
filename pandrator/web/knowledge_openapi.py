"""Explicit ledger initialization and current project access contracts."""

from __future__ import annotations

from .knowledge_schemas import KNOWLEDGE_SCHEMAS as KNOWLEDGE_SCHEMAS


def knowledge_paths() -> dict:
    root = "/api/v1/sessions/{sessionId}/knowledge/{kind}"
    specifications = (
        (root, "get", "getKnowledgeLedger", "KnowledgeLedgerResponse", ["app.read"]),
        (
            root,
            "post",
            "initializeKnowledgeLedger",
            "KnowledgeLedgerResponse",
            ["app.read", "app.write"],
        ),
        (
            "/api/v1/knowledge/{ledgerId}",
            "patch",
            "updateKnowledgeLedger",
            "KnowledgeUpdateResponse",
            ["app.read", "app.write"],
        ),
    )
    paths = {}
    for path, method, operation_id, response, scopes in specifications:
        parameters = [
            {
                "name": name,
                "in": "path",
                "required": True,
                "schema": {"type": "string", "minLength": 1, "maxLength": 36},
            }
            for name in ("sessionId", "ledgerId")
            if "{" + name + "}" in path
        ]
        if "{kind}" in path:
            parameters.append(
                {
                    "name": "kind",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string", "enum": ["research", "glossary"]},
                }
            )
        if method == "get":
            parameters.extend(
                {
                    "name": name,
                    "in": "query",
                    "schema": {
                        "type": "string",
                        "maxLength": 40,
                        "default": default,
                    },
                }
                for name, default in (("source_language", "auto"), ("target_language", ""))
            )
        if method == "patch":
            parameters.append(
                {
                    "name": "If-Match",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string"},
                    "description": "Current ledger revision.",
                }
            )
        operation = {
            "operationId": operation_id,
            "description": (
                "Read saved knowledge under current project reading authority. An absent ledger "
                "returns empty content with null id and revision 0, without creating a row."
                if method == "get"
                else "Initialize an empty ledger once under current project editing authority. "
                "Repeating the same session/kind/language scope returns the existing row."
                if method == "post"
                else "Replace saved knowledge under current project editing authority and revision CAS."
            ),
            "security": [{"cookieAuth": []}, {"bearerToken": []}, {"nativeOAuth": scopes}],
            "x-required-scopes": scopes,
            "parameters": parameters,
            "responses": {
                "200": {
                    "description": "Current ledger state",
                    "content": {
                        "application/json": {"schema": {"$ref": f"#/components/schemas/{response}"}}
                    },
                },
                "401": {"description": "Authentication required"},
                "403": {"description": "Current action authority required"},
                "404": {"description": "Session or ledger unavailable"},
                "409": {"description": "Current ledger revision changed"},
                "422": {"description": "Invalid bounded request"},
            },
        }
        if method == "post":
            operation["responses"]["201"] = {
                "description": "Ledger initialized",
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/KnowledgeLedgerResponse"}
                    }
                },
            }
        if method == "patch":
            operation["responses"]["428"] = {"description": "Current revision required"}
        if method in {"post", "patch"}:
            operation["requestBody"] = {
                "required": method == "patch",
                "content": {
                    "application/json": {
                        "schema": {
                            "$ref": "#/components/schemas/"
                            + ("KnowledgeScope" if method == "post" else "KnowledgeUpdateRequest")
                        }
                    }
                },
            }
        paths.setdefault(path, {})[method] = operation
    return paths
