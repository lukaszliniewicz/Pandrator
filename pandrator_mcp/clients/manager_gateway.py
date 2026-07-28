"""Manager access protocol shared by proxy and future recovery clients."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from ..errors import PandratorMcpError


class ManagerGateway(Protocol):
    def status(self) -> dict[str, Any]: ...

    def components(self) -> dict[str, Any]: ...

    def doctor(self) -> dict[str, Any]: ...

    def services(self) -> dict[str, Any]: ...

    def releases(self) -> dict[str, Any]: ...

    def operation(self, operation_id: str) -> dict[str, Any]: ...

    def operation_tasks(self, operation_id: str) -> dict[str, Any]: ...

    def create_plan(
        self,
        *,
        kind: str,
        desired: dict[str, dict[str, Any]],
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]: ...

    def execute_plan(
        self,
        *,
        plan_id: str,
        plan_digest: str,
        accepted_confirmations: tuple[str, ...],
        idempotency_key: str,
    ) -> dict[str, Any]: ...

    def control_runtime(
        self,
        *,
        action: str,
        target: str,
        service_ids: tuple[str, ...],
        idempotency_key: str,
    ) -> dict[str, Any]: ...

    def cancel_operation(
        self,
        operation_id: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]: ...


class ManagerUnavailableGateway:
    """Externally managed targets intentionally have no Manager plane."""

    def _unavailable(self) -> dict[str, Any]:
        return {
            "available": False,
            "error": {
                "code": "manager_unavailable",
                "message": "This Pandrator target is externally managed.",
            },
        }

    def status(self) -> dict[str, Any]:
        return self._unavailable()

    def components(self) -> dict[str, Any]:
        return self._unavailable()

    def doctor(self) -> dict[str, Any]:
        return self._unavailable()

    def services(self) -> dict[str, Any]:
        return self._unavailable()

    def releases(self) -> dict[str, Any]:
        return self._unavailable()

    def operation(self, operation_id: str) -> dict[str, Any]:
        _ = operation_id
        return self._unavailable()

    def operation_tasks(self, operation_id: str) -> dict[str, Any]:
        _ = operation_id
        return self._unavailable()

    def _mutation_unavailable(self) -> dict[str, Any]:
        raise PandratorMcpError(
            "manager_unavailable",
            "This target has no approved Pandrator Manager gateway.",
        )

    def create_plan(self, **_kwargs) -> dict[str, Any]:
        return self._mutation_unavailable()

    def execute_plan(self, **_kwargs) -> dict[str, Any]:
        return self._mutation_unavailable()

    def control_runtime(self, **_kwargs) -> dict[str, Any]:
        return self._mutation_unavailable()

    def cancel_operation(
        self,
        operation_id: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        _ = (operation_id, idempotency_key)
        return self._mutation_unavailable()


T = TypeVar("T")


class FallbackManagerGateway:
    """Use the app proxy normally and direct recovery only on outage."""

    def __init__(
        self,
        primary: ManagerGateway,
        recovery: ManagerGateway,
    ) -> None:
        self.primary = primary
        self.recovery = recovery

    def _call(
        self,
        function: Callable[[ManagerGateway], T],
    ) -> T:
        try:
            result = function(self.primary)
            if (
                isinstance(result, dict)
                and result.get("available") is False
            ):
                return function(self.recovery)
            return result
        except PandratorMcpError as error:
            if error.code not in {
                "application_unavailable",
                "downstream_unavailable",
                "manager_unavailable",
            }:
                raise
            return function(self.recovery)

    def status(self) -> dict[str, Any]:
        return self._call(lambda gateway: gateway.status())

    def components(self) -> dict[str, Any]:
        return self._call(lambda gateway: gateway.components())

    def doctor(self) -> dict[str, Any]:
        return self._call(lambda gateway: gateway.doctor())

    def services(self) -> dict[str, Any]:
        return self._call(lambda gateway: gateway.services())

    def releases(self) -> dict[str, Any]:
        return self._call(lambda gateway: gateway.releases())

    def operation(self, operation_id: str) -> dict[str, Any]:
        return self._call(
            lambda gateway: gateway.operation(operation_id)
        )

    def operation_tasks(
        self,
        operation_id: str,
    ) -> dict[str, Any]:
        return self._call(
            lambda gateway: gateway.operation_tasks(operation_id)
        )

    def create_plan(self, **kwargs) -> dict[str, Any]:
        return self._call(
            lambda gateway: gateway.create_plan(**kwargs)
        )

    def execute_plan(self, **kwargs) -> dict[str, Any]:
        return self._call(
            lambda gateway: gateway.execute_plan(**kwargs)
        )

    def control_runtime(self, **kwargs) -> dict[str, Any]:
        return self._call(
            lambda gateway: gateway.control_runtime(**kwargs)
        )

    def cancel_operation(
        self,
        operation_id: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._call(
            lambda gateway: gateway.cancel_operation(
                operation_id,
                idempotency_key=idempotency_key,
            )
        )
