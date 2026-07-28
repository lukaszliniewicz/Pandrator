"""Small canonical OpenAPI document for the loopback contract."""

from __future__ import annotations

from .. import __version__


def build_openapi() -> dict:
    resources = {
        "/v1/status": ("get", "getManagerStatus"),
        "/v1/capabilities": ("get", "getManagerCapabilities"),
        "/v1/components": ("get", "listManagerComponents"),
        "/v1/doctor": ("get", "getManagerDoctorReport"),
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
    }
    paths = {}
    for path, (method, operation_id) in resources.items():
        paths.setdefault(path, {})[method] = {
            "operationId": operation_id,
            "responses": {
                "200": {"description": "Success"},
                "400": {"description": "Invalid request"},
                "401": {"description": "Authentication required"},
                "409": {"description": "Conflict"},
            },
        }
    paths["/v1/operations"]["post"] = {
        "operationId": "submitManagerOperation",
        "responses": {
            "202": {"description": "Operation accepted"},
            "409": {"description": "Plan or revision conflict"},
        },
    }
    paths["/v1/session"]["delete"] = {
        "operationId": "signOutBrowserSession",
        "responses": {
            "200": {"description": "Browser session revoked"},
            "401": {"description": "Browser session required"},
        },
    }
    paths["/v1/browser-sessions"]["delete"] = {
        "operationId": "revokeBrowserSessions",
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
    }
