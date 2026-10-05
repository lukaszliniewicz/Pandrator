"""Application work manager domain request builders."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .application_requests import ApplicationRequests


class ApplicationWorkManagerMethods(ApplicationRequests):
    def list_work(
        self,
        *,
        session_id: str | None = None,
        kinds: tuple[str, ...] = (),
        states: tuple[str, ...] = (),
        limit: int = 50,
    ) -> dict[str, Any]:
        parameters: dict[str, Any] = {"limit": max(1, min(int(limit), 100))}
        if session_id:
            parameters["session_id"] = session_id
        if kinds:
            parameters["kind"] = list(kinds)
        if states:
            parameters["state"] = list(states)
        return self._request_json("/api/v1/work", parameters=parameters)

    def get_work(self, work_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/work/{quote(work_id, safe='')}")

    def get_work_events(
        self,
        work_id: str,
        *,
        after: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/work/{quote(work_id, safe='')}/events",
            parameters={
                "after": max(0, int(after)),
                "limit": max(1, min(int(limit), 200)),
            },
        )

    def create_workflow_plan(
        self,
        session_id: str,
        *,
        target_stage: str,
        overrides: dict[str, Any],
        expires_in_minutes: int,
        continuation: bool = True,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/workflow-plans",
            method="POST",
            body={
                "target_stage": target_stage,
                "overrides": overrides,
                "continuation": bool(continuation),
                "expires_in_minutes": max(
                    1,
                    min(int(expires_in_minutes), 60),
                ),
            },
        )

    def get_workflow_plan(self, plan_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/workflow-plans/{quote(plan_id, safe='')}")

    def execute_workflow_plan(
        self,
        plan_id: str,
        *,
        plan_digest: str,
        accepted_confirmations: tuple[str, ...],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/workflow-plans/{quote(plan_id, safe='')}/execute",
            method="POST",
            body={
                "plan_digest": plan_digest,
                "accepted_confirmations": list(accepted_confirmations),
            },
            idempotency_key=idempotency_key,
        )

    def cancel_work(
        self,
        work_id: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/work/{quote(work_id, safe='')}/cancel",
            method="POST",
            body={},
            idempotency_key=idempotency_key,
        )

    def manager_read(
        self,
        resource: str,
        *,
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        allowed = {
            "status": "/api/v1/manager/status",
            "components": "/api/v1/manager/components",
            "doctor": "/api/v1/manager/doctor",
            "services": "/api/v1/manager/services",
            "releases": "/api/v1/manager/releases",
        }
        try:
            path = allowed[resource]
        except KeyError as error:
            raise ValueError("The Manager proxy resource is not allowlisted.") from error
        return self._request_json(path, parameters=parameters)

    def manager_operation(self, operation_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/manager/operations/{quote(operation_id, safe='')}")

    def manager_operation_tasks(
        self,
        operation_id: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/manager/operations/{quote(operation_id, safe='')}/tasks"
        )

    def manager_create_plan(
        self,
        *,
        kind: str,
        desired: dict[str, dict[str, Any]],
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            "/api/v1/manager/plans",
            method="POST",
            body={
                "kind": kind,
                "desired": desired,
                "expected_revision": expected_revision,
            },
            idempotency_key=idempotency_key,
        )

    def manager_execute_plan(
        self,
        *,
        plan_id: str,
        plan_digest: str,
        accepted_confirmations: tuple[str, ...],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            "/api/v1/manager/operations",
            method="POST",
            body={
                "plan_id": plan_id,
                "plan_digest": plan_digest,
                "accepted_confirmations": list(accepted_confirmations),
            },
            idempotency_key=idempotency_key,
        )

    def manager_runtime(
        self,
        *,
        action: str,
        service_ids: tuple[str, ...],
        idempotency_key: str,
    ) -> dict[str, Any]:
        if action not in {"start", "stop", "restart"}:
            raise ValueError("The Manager runtime action is invalid.")
        return self._request_json(
            f"/api/v1/manager/runtime/{action}",
            method="POST",
            body={"service_ids": list(service_ids)},
            idempotency_key=idempotency_key,
        )

    def manager_cancel_operation(
        self,
        operation_id: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/manager/operations/{quote(operation_id, safe='')}/cancel",
            method="POST",
            body={},
            idempotency_key=idempotency_key,
        )
