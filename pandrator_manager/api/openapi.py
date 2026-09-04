"""Small canonical OpenAPI document for the loopback contract."""

from __future__ import annotations

from .. import __version__


def build_openapi() -> dict:
    resources = {
        "/v1/status": ("get", "getManagerStatus"),
        "/v1/inventory": ("get", "getManagerInventory"),
        "/v1/capabilities": ("get", "getManagerCapabilities"),
        "/v1/components": ("get", "listManagerComponents"),
        "/v1/doctor": ("get", "getManagerDoctorReport"),
        "/v1/diagnostics/bundle": (
            "get",
            "downloadManagerDiagnosticBundle",
        ),
        "/v1/legacy": ("get", "getLegacyImportReport"),
        "/v1/legacy/import": ("post", "importLegacyWorkspace"),
        "/v1/services": ("get", "listManagedServices"),
        "/v1/application": ("get", "getManagedApplication"),
        "/v1/application/start": ("post", "startManagedApplication"),
        "/v1/application/stop": ("post", "stopManagedApplication"),
        "/v1/application/restart": ("post", "restartManagedApplication"),
        "/v1/application/launch": ("post", "launchManagedApplication"),
        "/v1/network": ("get", "getNetworkConfiguration"),
        "/v1/network/application": ("put", "updateApplicationNetwork"),
        "/v1/releases": ("get", "listProductReleases"),
        "/v1/releases/manager-update": ("get", "discoverManagerUpdate"),
        "/v1/releases/plans": ("post", "createProductReleasePlan"),
        "/v1/uninstall/plans": ("post", "createUninstallPlan"),
        "/v1/plans": ("post", "createManagerPlan"),
        "/v1/operations": ("get", "listManagerOperations"),
        "/v1/operations/{operation_id}": ("get", "getManagerOperation"),
        "/v1/operations/{operation_id}/tasks": ("get", "getManagerOperationTasks"),
        "/v1/operations/{operation_id}/cancel": ("post", "cancelManagerOperation"),
        "/v1/events": ("get", "streamManagerEvents"),
        "/v1/activity": ("get", "listManagerActivity"),
        "/v1/logs": ("get", "getManagerLogs"),
        "/v1/runtime/start": ("post", "startManagedServices"),
        "/v1/runtime/stop": ("post", "stopManagedServices"),
        "/v1/runtime/restart": ("post", "restartManagedServices"),
        "/v1/recovery-sessions": ("post", "createRecoverySession"),
        "/v1/recovery/exchange": ("post", "exchangeRecoveryToken"),
        "/v1/session": ("get", "getBrowserSession"),
        "/v1/browser-sessions": ("get", "listBrowserSessions"),
        "/v1/automation/identity": (
            "get",
            "getManagerAutomationIdentity",
        ),
        "/v1/automation/authorize": (
            "get",
            "authorizeManagerAutomationClient",
        ),
        "/v1/automation/enrollment-grants": (
            "post",
            "createManagerAutomationEnrollmentGrant",
        ),
        "/v1/automation/token": (
            "post",
            "exchangeManagerAutomationEnrollmentGrant",
        ),
        "/v1/automation/principal": (
            "get",
            "getManagerAutomationPrincipal",
        ),
        "/v1/automation/clients": (
            "get",
            "listManagerAutomationClients",
        ),
        "/v1/automation/clients/{client_id}": (
            "delete",
            "revokeManagerAutomationClient",
        ),
    }
    public_paths = {
        "/v1/health",
        "/v1/openapi.json",
        "/v1/recovery/exchange",
        "/v1/automation/identity",
        "/v1/automation/token",
    }
    automation_scopes = {
        ("get", "/v1/status"): "manager.read",
        ("get", "/v1/inventory"): "manager.read",
        ("get", "/v1/capabilities"): "manager.read",
        ("get", "/v1/components"): "manager.read",
        ("get", "/v1/doctor"): "manager.read",
        ("get", "/v1/services"): "manager.read",
        ("get", "/v1/application"): "manager.read",
        ("get", "/v1/releases"): "manager.read",
        ("get", "/v1/releases/manager-update"): "manager.read",
        ("post", "/v1/plans"): "manager.read",
        ("post", "/v1/releases/plans"): "manager.read",
        ("post", "/v1/uninstall/plans"): "manager.read",
        ("get", "/v1/operations"): "manager.read",
        ("get", "/v1/operations/{operation_id}"): "manager.read",
        (
            "get",
            "/v1/operations/{operation_id}/tasks",
        ): "manager.read",
        ("get", "/v1/activity"): "manager.read",
        ("post", "/v1/application/start"): "manager.runtime",
        ("post", "/v1/application/stop"): "manager.runtime",
        ("post", "/v1/application/restart"): "manager.runtime",
        ("post", "/v1/runtime/start"): "manager.runtime",
        ("post", "/v1/runtime/stop"): "manager.runtime",
        ("post", "/v1/runtime/restart"): "manager.runtime",
        ("post", "/v1/operations"): "manager.mutate",
        (
            "post",
            "/v1/operations/{operation_id}/cancel",
        ): "manager.mutate",
        (
            "get",
            "/v1/automation/principal",
        ): "manager.read",
    }
    browser_only_paths = {
        "/v1/automation/authorize",
        "/v1/automation/enrollment-grants",
        "/v1/session",
        "/v1/browser-sessions",
    }
    paths = {}
    for path, (method, operation_id) in resources.items():
        operation = {
            "operationId": operation_id,
            "responses": {
                "200": {"description": "Success"},
                "400": {"description": "Invalid request"},
                "401": {"description": "Authentication required"},
                "409": {"description": "Conflict"},
            },
        }
        if path in browser_only_paths:
            operation["security"] = [{"managerBrowserSession": []}]
        elif path not in public_paths:
            security = [
                {"managerLocalBearer": []},
                {"managerBrowserSession": []},
            ]
            scope = automation_scopes.get((method, path))
            if scope:
                security.append(
                    {"managerAutomationBearer": [scope]}
                )
                operation["responses"]["403"] = {
                    "description": "Scope or route denied"
                }
                operation["responses"]["429"] = {
                    "description": (
                        "Automation client request rate exceeded"
                    )
                }
            operation["security"] = security
        paths.setdefault(path, {})[method] = operation
    paths["/v1/automation/authorize"]["post"] = {
        "operationId": "completeManagerAutomationAuthorization",
        "security": [{"managerBrowserSession": []}],
        "responses": {
            "302": {"description": "Return the one-use result to the native client"},
            "400": {"description": "Invalid or expired authorization"},
            "401": {"description": "Browser session required"},
        },
    }
    paths["/v1/operations"]["post"] = {
        "operationId": "submitManagerOperation",
        "security": [
            {"managerLocalBearer": []},
            {"managerBrowserSession": []},
            {"managerAutomationBearer": ["manager.mutate"]},
        ],
        "responses": {
            "202": {"description": "Operation accepted"},
            "400": {"description": "Invalid request"},
            "401": {"description": "Authentication required"},
            "403": {"description": "Scope or route denied"},
            "409": {"description": "Plan or revision conflict"},
            "429": {
                "description": "Automation client request rate exceeded"
            },
        },
    }
    paths["/v1/session"]["delete"] = {
        "operationId": "signOutBrowserSession",
        "security": [{"managerBrowserSession": []}],
        "responses": {
            "200": {"description": "Browser session revoked"},
            "401": {"description": "Browser session required"},
        },
    }
    paths["/v1/browser-sessions"]["delete"] = {
        "operationId": "revokeBrowserSessions",
        "security": [{"managerBrowserSession": []}],
        "responses": {
            "200": {"description": "Browser sessions revoked"},
            "401": {"description": "Browser session required"},
        },
    }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Pandrator Manager API",
            "version": __version__,
        },
        "servers": [{"url": "/"}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "managerLocalBearer": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": (
                        "Permanent installation credential accepted only "
                        "on the Manager's loopback client boundary."
                    ),
                },
                "managerBrowserSession": {
                    "type": "apiKey",
                    "in": "cookie",
                    "name": "pandrator_manager_session",
                    "description": (
                        "Human recovery session; non-read operations also "
                        "require the session CSRF token."
                    ),
                },
                "managerAutomationBearer": {
                    "type": "oauth2",
                    "description": (
                        "Separate expiring recovery audience; never the "
                        "permanent local Manager credential."
                    ),
                    "flows": {
                        "authorizationCode": {
                            "authorizationUrl": (
                                "/v1/automation/authorize"
                            ),
                            "tokenUrl": "/v1/automation/token",
                            "scopes": {
                                "manager.read": (
                                    "Read Manager state and create "
                                    "immutable plans."
                                ),
                                "manager.runtime": (
                                    "Control approved application and "
                                    "managed-service runtime."
                                ),
                                "manager.mutate": (
                                    "Execute and cancel exact Manager "
                                    "operations."
                                ),
                            },
                        }
                    },
                },
            }
        },
    }
