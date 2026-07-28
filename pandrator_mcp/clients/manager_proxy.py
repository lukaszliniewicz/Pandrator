"""Read-only Manager gateway through Pandrator's same-origin proxy."""

from __future__ import annotations

from typing import Any

from ..errors import PandratorMcpError
from .application import ApplicationClient


class ApplicationProxyManagerGateway:
    def __init__(self, application: ApplicationClient) -> None:
        self.application = application

    def status(self) -> dict[str, Any]:
        return self.application.manager_read("status")

    def components(self) -> dict[str, Any]:
        return self.application.manager_read("components")

    def doctor(self) -> dict[str, Any]:
        return self.application.manager_read("doctor")

    def services(self) -> dict[str, Any]:
        return self.application.manager_read("services")

    def releases(self) -> dict[str, Any]:
        return self.application.manager_read("releases")

    def operation(self, operation_id: str) -> dict[str, Any]:
        return self.application.manager_operation(operation_id)

    def operation_tasks(self, operation_id: str) -> dict[str, Any]:
        return self.application.manager_operation_tasks(operation_id)

    def create_plan(self, **kwargs) -> dict[str, Any]:
        return self.application.manager_create_plan(**kwargs)

    def execute_plan(self, **kwargs) -> dict[str, Any]:
        return self.application.manager_execute_plan(**kwargs)

    def control_runtime(
        self,
        *,
        action: str,
        target: str,
        service_ids: tuple[str, ...],
        idempotency_key: str,
    ) -> dict[str, Any]:
        if target == "application":
            raise PandratorMcpError(
                "manager_unavailable",
                "Stopping or restarting the application requires the independent Manager recovery gateway.",
            )
        return self.application.manager_runtime(
            action=action,
            service_ids=service_ids,
            idempotency_key=idempotency_key,
        )

    def cancel_operation(
        self,
        operation_id: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.application.manager_cancel_operation(
            operation_id,
            idempotency_key=idempotency_key,
        )
