"""Validated local Manager discovery, diagnostics, and app authentication."""

from __future__ import annotations

import hmac
from typing import Any
from urllib.parse import quote

import requests

from ..errors import PandratorMcpError
from ..targets import ResolvedTarget, TargetProfile


def _manager_client(workspace: str):
    try:
        from pandrator_manager.client import ManagerClient, ManagerUnavailable
    except ImportError as error:
        raise PandratorMcpError(
            "manager_unavailable",
            "The optional pandrator-manager integration is not installed.",
        ) from error
    try:
        return ManagerClient.discover(workspace)
    except ManagerUnavailable as error:
        raise PandratorMcpError(
            "manager_unavailable",
            "The local Pandrator Manager is unavailable or failed identity validation.",
            retryable=True,
        ) from error


def discover_local_application(profile: TargetProfile) -> tuple[str, str | None]:
    if not profile.workspace:
        raise PandratorMcpError(
            "manager_unavailable",
            "The local target has no Manager workspace.",
        )
    client = _manager_client(profile.workspace)
    try:
        payload = client.request("GET", "/v1/application").json()
    except Exception as error:
        raise PandratorMcpError(
            "manager_unavailable",
            "Manager could not report the local Pandrator application.",
            retryable=True,
        ) from error
    endpoint = str(payload.get("endpoint") or "").strip().rstrip("/")
    if not payload.get("running") or not endpoint:
        raise PandratorMcpError(
            "application_unavailable",
            "The local Pandrator application is not running.",
            details={
                "installed": bool(payload.get("installed")),
                "running": bool(payload.get("running")),
            },
            retryable=True,
        )
    return endpoint, str(client.descriptor.instance_id)


def bootstrap_local_application(
    target: ResolvedTarget,
    session: requests.Session,
) -> str:
    """Exchange validated Manager authority for a short-lived app session."""

    if not target.workspace:
        raise PandratorMcpError(
            "manager_unavailable",
            "The local target has no Manager workspace.",
        )
    client = _manager_client(target.workspace)
    if target.discovered_manager_instance_id and not hmac.compare_digest(
        target.discovered_manager_instance_id,
        str(client.descriptor.instance_id),
    ):
        raise PandratorMcpError(
            "target_identity_mismatch",
            "The local Manager instance changed during authentication.",
        )
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {client.secret}",
    }
    grant_response: requests.Response | None = None
    exchange_response: requests.Response | None = None
    try:
        grant_response = session.post(
            f"{target.application.origin}/api/v1/auth/manager-bootstrap",
            headers=headers,
            json={
                "scopes": [
                    "app.read",
                    "app.write",
                    "app.run",
                    "app.cancel",
                    "manager.read",
                    "manager.runtime",
                    "manager.mutate",
                ]
            },
            timeout=(3, 10),
            allow_redirects=False,
        )
        if grant_response.status_code != 200:
            raise PandratorMcpError(
                "authentication_required",
                "Manager could not authorize the local MCP application session.",
            )
        grant = grant_response.json()
        token = str(grant.get("token") or "")
        if not token:
            raise PandratorMcpError(
                "authentication_required",
                "The local application bootstrap grant was invalid.",
            )
        exchange_response = session.post(
            f"{target.application.origin}/api/v1/auth/bootstrap",
            headers={"Accept": "application/json"},
            json={"token": token},
            timeout=(3, 10),
            allow_redirects=False,
        )
        if exchange_response.status_code != 200:
            raise PandratorMcpError(
                "authentication_required",
                "The local application rejected its Manager bootstrap grant.",
            )
        exchange = exchange_response.json()
        csrf_token = str(exchange.get("csrf_token") or "")
        if not csrf_token:
            raise PandratorMcpError(
                "authentication_required",
                "The local application bootstrap omitted its CSRF token.",
            )
        return csrf_token
    except PandratorMcpError:
        raise
    except (requests.RequestException, ValueError) as error:
        raise PandratorMcpError(
            "application_unavailable",
            "The local application authentication exchange failed.",
            retryable=True,
        ) from error
    finally:
        if grant_response is not None:
            grant_response.close()
        if exchange_response is not None:
            exchange_response.close()


class LocalManagerGateway:
    """Read-only gateway that remains usable while the application is down."""

    def __init__(self, workspace: str) -> None:
        self.workspace = workspace

    def _client(self):
        return _manager_client(self.workspace)

    def _call(self, method: str) -> Any:
        client = self._client()
        try:
            return getattr(client, method)()
        except PandratorMcpError:
            raise
        except Exception as error:
            raise PandratorMcpError(
                "manager_unavailable",
                "The local Pandrator Manager diagnostic request failed.",
                retryable=True,
            ) from error

    def status(self) -> dict[str, Any]:
        return self._call("status")

    def components(self) -> dict[str, Any]:
        return {"items": self._call("components")}

    def doctor(self) -> dict[str, Any]:
        result = self._call("doctor")
        return result.model_dump(mode="json")

    def services(self) -> dict[str, Any]:
        return {"items": self._call("services")}

    def releases(self) -> dict[str, Any]:
        return self._call("releases")

    def operation(self, operation_id: str) -> dict[str, Any]:
        client = self._client()
        try:
            return client.request(
                "GET",
                f"/v1/operations/{quote(operation_id, safe='')}",
            ).json()
        except Exception as error:
            raise PandratorMcpError(
                "manager_unavailable",
                "The local Manager operation could not be inspected.",
                retryable=True,
            ) from error

    def operation_tasks(self, operation_id: str) -> dict[str, Any]:
        client = self._client()
        try:
            return client.request(
                "GET",
                f"/v1/operations/{quote(operation_id, safe='')}/tasks",
            ).json()
        except Exception as error:
            raise PandratorMcpError(
                "manager_unavailable",
                "The local Manager operation log could not be inspected.",
                retryable=True,
            ) from error

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        timeout: float = 30,
    ) -> dict[str, Any]:
        client = self._client()
        try:
            payload = client.request(
                method,
                path,
                json_payload=body,
                idempotency_key=idempotency_key,
                timeout=timeout,
            ).json()
        except Exception as error:
            raise PandratorMcpError(
                "manager_unavailable",
                "The local Pandrator Manager request failed.",
                retryable=True,
            ) from error
        if not isinstance(payload, dict):
            raise PandratorMcpError(
                "downstream_unavailable",
                "The local Manager returned an unexpected response.",
            )
        return payload

    def create_plan(
        self,
        *,
        kind: str,
        desired: dict[str, dict[str, Any]],
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            "POST",
            "/v1/plans",
            body={
                "kind": kind,
                "desired": desired,
                "expected_revision": expected_revision,
            },
            idempotency_key=idempotency_key,
        )

    def execute_plan(
        self,
        *,
        plan_id: str,
        plan_digest: str,
        accepted_confirmations: tuple[str, ...],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            "POST",
            "/v1/operations",
            body={
                "plan_id": plan_id,
                "plan_digest": plan_digest,
                "accepted_confirmations": list(
                    accepted_confirmations
                ),
            },
            idempotency_key=idempotency_key,
        )

    def control_runtime(
        self,
        *,
        action: str,
        target: str,
        service_ids: tuple[str, ...],
        idempotency_key: str,
    ) -> dict[str, Any]:
        if action not in {"start", "stop", "restart"}:
            raise ValueError("The Manager runtime action is invalid.")
        if target == "application":
            path = f"/v1/application/{action}"
            body: dict[str, Any] = {}
        elif target == "managed_services":
            path = f"/v1/runtime/{action}"
            body = {"service_ids": list(service_ids)}
        else:
            raise ValueError("The Manager runtime target is invalid.")
        return self._request_json(
            "POST",
            path,
            body=body,
            idempotency_key=idempotency_key,
            timeout=10 * 60,
        )

    def cancel_operation(
        self,
        operation_id: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            "POST",
            f"/v1/operations/{quote(operation_id, safe='')}/cancel",
            body={},
            idempotency_key=idempotency_key,
        )
