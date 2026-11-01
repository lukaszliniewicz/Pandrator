from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, cast

import pytest

import pandrator_mcp.clients.local_manager as local_manager_module
import pandrator_mcp.context as context_module
from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.clients.local_manager import LocalManagerGateway
from pandrator_mcp.context import build_runtime
from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.network_policy import TargetMode
from pandrator_mcp.settings import McpSettings
from pandrator_mcp.targets import TargetProfile, TargetRegistry

ADMIN_ERROR_MESSAGE = (
    "Connect an Administrator application credential to manage models and runtimes."
)


class _LocalManager:
    def __init__(self) -> None:
        self.reads: list[str] = []
        self.requests: list[tuple[str, str]] = []

    def status(self) -> dict[str, Any]:
        self.reads.append("status")
        return {"available": True}

    def request(self, method: str, path: str, **_kwargs: Any) -> Any:
        self.requests.append((method, path))
        raise AssertionError("Local mutation requests must not be issued.")


class _Application:
    def __init__(self, error: PandratorMcpError | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _call(self, name: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((name, kwargs))
        if self.error is not None:
            raise self.error
        return {"operation": name}

    def manager_create_plan(
        self,
        *,
        kind: str,
        desired: dict[str, dict[str, Any]],
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._call(
            "create_plan",
            kind=kind,
            desired=desired,
            expected_revision=expected_revision,
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
        return self._call(
            "execute_plan",
            plan_id=plan_id,
            plan_digest=plan_digest,
            accepted_confirmations=accepted_confirmations,
            idempotency_key=idempotency_key,
        )

    def manager_runtime(
        self,
        *,
        action: str,
        service_ids: tuple[str, ...],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._call(
            "control_runtime",
            action=action,
            service_ids=service_ids,
            idempotency_key=idempotency_key,
        )

    def manager_cancel_operation(
        self,
        operation_id: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._call(
            "cancel_operation",
            operation_id=operation_id,
            idempotency_key=idempotency_key,
        )


class _RecordingApplicationGateway:
    instances: list[_RecordingApplicationGateway] = []

    def __init__(self, application: ApplicationClient) -> None:
        self.application = application
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.instances.append(self)

    def _record(self, method: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((method, kwargs))
        return {"delegated": method}

    def create_plan(self, **kwargs: Any) -> dict[str, Any]:
        return self._record("create_plan", **kwargs)

    def execute_plan(self, **kwargs: Any) -> dict[str, Any]:
        return self._record("execute_plan", **kwargs)

    def control_runtime(self, **kwargs: Any) -> dict[str, Any]:
        return self._record("control_runtime", **kwargs)

    def cancel_operation(self, operation_id: str, **kwargs: Any) -> dict[str, Any]:
        return self._record("cancel_operation", operation_id=operation_id, **kwargs)


def _mutations(gateway: LocalManagerGateway) -> tuple[Callable[[], Any], ...]:
    desired = {"component": {"model_id": "model-test"}}
    confirmations = ("provider", "storage")
    return (
        lambda: gateway.create_plan(
            kind="model.upload",
            desired=desired,
            expected_revision=17,
            idempotency_key="manager-plan-0001",
        ),
        lambda: gateway.execute_plan(
            plan_id="plan-1",
            plan_digest="digest-1",
            accepted_confirmations=confirmations,
            idempotency_key="manager-exec-0001",
        ),
        lambda: gateway.control_runtime(
            action="restart",
            target="managed_services",
            service_ids=("worker", "api"),
            idempotency_key="manager-runtime-0001",
        ),
        lambda: gateway.cancel_operation(
            "operation-1",
            idempotency_key="manager-cancel-0001",
        ),
    )


def test_all_manager_mutations_delegate_exact_arguments_to_application_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RecordingApplicationGateway.instances.clear()
    monkeypatch.setattr(
        local_manager_module,
        "ApplicationProxyManagerGateway",
        _RecordingApplicationGateway,
    )
    application = cast(ApplicationClient, object())
    local_manager = _LocalManager()
    monkeypatch.setattr(local_manager_module, "_manager_client", lambda _workspace: local_manager)
    gateway = LocalManagerGateway("/managed/workspace", application=application)

    assert [operation() for operation in _mutations(gateway)] == [
        {"delegated": "create_plan"},
        {"delegated": "execute_plan"},
        {"delegated": "control_runtime"},
        {"delegated": "cancel_operation"},
    ]
    proxy = _RecordingApplicationGateway.instances[0]
    assert proxy.application is application
    assert proxy.calls == [
        (
            "create_plan",
            {
                "kind": "model.upload",
                "desired": {"component": {"model_id": "model-test"}},
                "expected_revision": 17,
                "idempotency_key": "manager-plan-0001",
            },
        ),
        (
            "execute_plan",
            {
                "plan_id": "plan-1",
                "plan_digest": "digest-1",
                "accepted_confirmations": ("provider", "storage"),
                "idempotency_key": "manager-exec-0001",
            },
        ),
        (
            "control_runtime",
            {
                "action": "restart",
                "target": "managed_services",
                "service_ids": ("worker", "api"),
                "idempotency_key": "manager-runtime-0001",
            },
        ),
        (
            "cancel_operation",
            {"operation_id": "operation-1", "idempotency_key": "manager-cancel-0001"},
        ),
    ]
    assert local_manager.requests == []


def test_missing_application_fails_closed_without_manager_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_manager = _LocalManager()
    monkeypatch.setattr(local_manager_module, "_manager_client", lambda _workspace: local_manager)
    gateway = LocalManagerGateway("/managed/workspace")

    for operation in _mutations(gateway):
        with pytest.raises(PandratorMcpError) as caught:
            operation()
        assert caught.value.code == "administrator_enrollment_required"
        assert str(caught.value) == ADMIN_ERROR_MESSAGE
    assert local_manager.requests == []


@pytest.mark.parametrize("error_code", ["scope_denied", "application_unavailable"])
def test_application_denial_or_outage_never_retries_raw_manager_mutations(
    monkeypatch: pytest.MonkeyPatch,
    error_code: Literal["scope_denied", "application_unavailable"],
) -> None:
    local_manager = _LocalManager()
    monkeypatch.setattr(local_manager_module, "_manager_client", lambda _workspace: local_manager)
    application = _Application(PandratorMcpError(error_code, "Application mutation failed."))
    gateway = LocalManagerGateway(
        "/managed/workspace",
        application=cast(ApplicationClient, application),
    )

    for operation in _mutations(gateway):
        with pytest.raises(PandratorMcpError) as caught:
            operation()
        assert caught.value.code == error_code
    assert [name for name, _kwargs in application.calls] == [
        "create_plan",
        "execute_plan",
        "control_runtime",
        "cancel_operation",
    ]
    assert local_manager.requests == []


def test_application_runtime_mutation_keeps_proxy_restriction_without_raw_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_manager = _LocalManager()
    monkeypatch.setattr(local_manager_module, "_manager_client", lambda _workspace: local_manager)
    application = _Application()
    gateway = LocalManagerGateway(
        "/managed/workspace",
        application=cast(ApplicationClient, application),
    )

    with pytest.raises(PandratorMcpError) as caught:
        gateway.control_runtime(
            action="stop",
            target="application",
            service_ids=(),
            idempotency_key="manager-app-stop-001",
        )
    assert caught.value.code == "manager_unavailable"
    assert application.calls == []
    assert local_manager.requests == []


def test_local_diagnostic_reads_remain_available_when_application_mutations_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_manager = _LocalManager()
    monkeypatch.setattr(local_manager_module, "_manager_client", lambda _workspace: local_manager)
    application = _Application(PandratorMcpError("application_unavailable", "Application is down."))
    gateway = LocalManagerGateway(
        "/managed/workspace",
        application=cast(ApplicationClient, application),
    )

    assert gateway.status() == {"available": True}
    assert local_manager.reads == ["status"]
    assert application.calls == []
    assert local_manager.requests == []


def test_local_managed_runtime_passes_its_application_client_to_manager_gateway(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _Application()
    monkeypatch.setattr(context_module, "ApplicationClient", lambda *_args, **_kwargs: application)
    profile = TargetProfile(
        name="local-managed",
        mode=TargetMode.LOCAL_MANAGED,
        workspace=str(tmp_path / "workspace"),
    )
    registry = TargetRegistry([profile])
    runtime = build_runtime(
        McpSettings(
            target_name="local-managed",
            configuration_path=tmp_path / "mcp-targets.json",
        ),
        registry=registry,
    )

    assert isinstance(runtime.manager, LocalManagerGateway)
    assert runtime.application is application
    assert runtime.manager.application is application
    assert runtime.manager._application_manager is not None
    assert runtime.manager._application_manager.application is application
