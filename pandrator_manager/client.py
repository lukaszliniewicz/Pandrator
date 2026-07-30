"""Typed local client with descriptor and process-identity validation."""

from __future__ import annotations

import hmac
import ipaddress
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .auth import read_client_secret
from .context import WorkspaceLayout
from .desktop import host_process_environment
from .launcher import (
    LauncherRuntime,
    current_runtime_executable,
    installed_launcher,
    runtime_command,
)
from .models import (
    ConnectionDescriptor,
    DesiredComponentState,
    DoctorReport,
    OperationKind,
    OperationPlan,
    OperationRecord,
    ProcessIdentity,
)
from .processes.identity import (
    IdentityInspectionFailed,
    IdentityMismatch,
    validate_identity,
)
from .releases.bundles import active_manager_bundle
from .releases.handoff import pending_handoffs
from .uninstall import (
    clear_uninstall_status,
    pending_uninstalls,
    read_uninstall_status,
    uninstall_control_root,
    uninstall_helper_command,
    uninstall_statuses,
)


class ManagerUnavailable(RuntimeError):
    pass


class ProductUninstalled(ManagerUnavailable):
    def __init__(self, status: dict):
        super().__init__("Pandrator has been uninstalled from this workspace.")
        self.status = status


class ManagerApiError(RuntimeError):
    def __init__(self, status_code: int, payload: dict):
        error = payload.get("error") if isinstance(payload, dict) else None
        message = (
            error.get("message")
            if isinstance(error, dict)
            else f"Manager request failed with HTTP {status_code}."
        )
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class ManagerClient:
    def __init__(
        self,
        layout: WorkspaceLayout,
        descriptor: ConnectionDescriptor,
        secret: str,
        *,
        session: requests.Session | None = None,
    ) -> None:
        self.layout = layout
        self.descriptor = descriptor
        self.secret = secret
        self.session = session or requests.Session()
        self.session.trust_env = False

    @classmethod
    def discover(cls, workspace: str | Path) -> "ManagerClient":
        layout = WorkspaceLayout.from_value(workspace)
        try:
            descriptor = ConnectionDescriptor.model_validate_json(
                layout.descriptor.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as error:
            raise ManagerUnavailable("Manager connection descriptor is unavailable.") from error
        if Path(descriptor.workspace).resolve(strict=False) != layout.workspace:
            raise ManagerUnavailable("Manager descriptor belongs to another workspace.")
        parsed = urlsplit(descriptor.base_url)
        try:
            host = str(parsed.hostname or "").split("%", 1)[0]
            address = ipaddress.ip_address(host)
            port = parsed.port
        except ValueError as error:
            raise ManagerUnavailable(
                "Manager descriptor contains an unsafe endpoint."
            ) from error
        mapped = getattr(address, "ipv4_mapped", None)
        if (
            parsed.scheme != "http"
            or port is None
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or not (
                address.is_loopback
                or (mapped is not None and mapped.is_loopback)
            )
        ):
            raise ManagerUnavailable("Manager descriptor contains an unsafe endpoint.")
        identity = ProcessIdentity(
            pid=descriptor.pid,
            create_time=descriptor.process_create_time,
            executable=descriptor.executable,
            manager_instance_id=descriptor.instance_id,
        )
        try:
            process = validate_identity(identity)
        except (IdentityMismatch, IdentityInspectionFailed) as error:
            raise ManagerUnavailable("Manager process identity is invalid.") from error
        if process is None:
            raise ManagerUnavailable("Manager process is not running.")
        try:
            secret = read_client_secret(layout.credential)
        except (OSError, RuntimeError) as error:
            raise ManagerUnavailable("Manager client credential is unavailable.") from error
        return cls(layout, descriptor, secret)

    @classmethod
    def ensure_running(
        cls,
        workspace: str | Path,
        *,
        timeout_seconds: float = 30,
    ) -> "ManagerClient":
        layout = WorkspaceLayout.from_value(workspace)
        cls._resume_pending_uninstall(
            layout,
            timeout_seconds=timeout_seconds,
        )
        if not layout.database.is_file():
            completed = next(
                (
                    clear_uninstall_status(
                        layout,
                        str(status.get("operation_id") or ""),
                    )
                    or status
                    for status in uninstall_statuses(layout)
                    if str(status.get("status") or "").startswith(
                        "succeeded"
                    )
                ),
                None,
            )
            if completed is not None:
                raise ProductUninstalled(completed)
        try:
            client = cls.discover(workspace)
            client.status()
            return client
        except (ManagerUnavailable, ManagerApiError, requests.RequestException):
            pass
        cls.start_daemon(workspace)
        deadline = time.monotonic() + timeout_seconds
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                client = cls.discover(workspace)
                client.status()
                return client
            except (
                ManagerUnavailable,
                ManagerApiError,
                requests.RequestException,
            ) as error:
                last_error = error
                time.sleep(0.1)
        raise ManagerUnavailable(
            f"Manager did not become ready: {last_error or 'timeout'}"
        )

    @classmethod
    def _resume_pending_uninstall(
        cls,
        layout: WorkspaceLayout,
        *,
        timeout_seconds: float,
    ) -> None:
        pending = pending_uninstalls(layout)
        if not pending:
            return
        if len(pending) != 1:
            raise ManagerUnavailable(
                "Multiple pending uninstalls require manual recovery."
            )
        operation_id = pending[0]
        deadline = time.monotonic() + timeout_seconds
        attempts = 0
        process: subprocess.Popen | None = None
        while time.monotonic() < deadline:
            if process is None:
                process = cls.start_daemon(layout.workspace)
                attempts += 1
            status = read_uninstall_status(layout, operation_id)
            state = str((status or {}).get("status") or "")
            if state.startswith("succeeded"):
                finalized = clear_uninstall_status(
                    layout,
                    operation_id,
                )
                raise ProductUninstalled(finalized or status or {})
            if state == "failed":
                # The helper restored the old manager; normal discovery below
                # will reconnect once it has completed startup.
                return
            if state == "recovery_required":
                raise ManagerUnavailable(
                    "Uninstall recovery requires manual intervention: "
                    + str((status or {}).get("rollback_error") or "unknown error")
                )
            return_code = process.poll()
            if return_code is not None:
                if (
                    state == "cleanup_interrupted"
                    and pending_uninstalls(layout)
                    and attempts < 3
                ):
                    process = None
                    continue
                if state:
                    raise ManagerUnavailable(
                        f"Uninstall helper stopped with status {state}."
                    )
                raise ManagerUnavailable(
                    f"Uninstall helper exited with code {return_code}."
                )
            time.sleep(0.1)
        raise ManagerUnavailable(
            "Pending uninstall did not finish before the recovery timeout."
        )

    @staticmethod
    def start_daemon(workspace: str | Path) -> subprocess.Popen:
        layout = WorkspaceLayout.from_value(workspace)
        manager_pending = pending_handoffs(layout)
        uninstall_pending = pending_uninstalls(layout)
        if len(manager_pending) + len(uninstall_pending) > 1:
            raise ManagerUnavailable(
                "Multiple pending external handoffs require recovery."
            )
        if uninstall_pending:
            control = uninstall_control_root(layout)
            control.mkdir(parents=True, exist_ok=True)
            log_path = control / "uninstall-launch.log"
        else:
            layout.logs.mkdir(parents=True, exist_ok=True)
            log_path = layout.logs / "manager-launch.log"
        log = log_path.open("ab", buffering=0)
        pending = manager_pending
        active = active_manager_bundle(layout)
        cwd = (
            str(active.application_root)
            if active is not None
            else str(Path.cwd())
        )
        if uninstall_pending:
            cwd = str(uninstall_control_root(layout))
        stable = installed_launcher(layout)
        fallback = LauncherRuntime(
            mode=(
                "native_launcher"
                if bool(getattr(sys, "frozen", False))
                else "python"
            ),
            executable=current_runtime_executable(),
        )
        if uninstall_pending:
            command = uninstall_helper_command(
                layout,
                uninstall_pending[0],
            )
        elif pending:
            command = runtime_command(
                stable or fallback,
                action="handoff",
                workspace=layout.workspace,
                operation_id=pending[0],
            )
        elif active is not None:
            command = runtime_command(
                LauncherRuntime(
                    mode=active.metadata.runtime_kind,
                    executable=active.python,
                ),
                action="daemon",
                workspace=layout.workspace,
            )
        else:
            command = runtime_command(
                stable or fallback,
                action="daemon",
                workspace=layout.workspace,
            )
        options = (
            {
                "creationflags": (
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.CREATE_NO_WINDOW
                )
            }
            if os.name == "nt"
            else {"start_new_session": True}
        )
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                shell=False,
                cwd=cwd,
                env=host_process_environment(),
                **options,
            )
        except Exception:
            log.close()
            raise
        log.close()
        # Keep a waiter alive for the detached child. On POSIX this prevents a
        # stopped manager becoming a zombie; on Windows it keeps Popen's
        # process handle owned until the daemon exits.
        threading.Thread(
            target=process.wait,
            name=f"pandrator-manager-reaper-{process.pid}",
            daemon=True,
        ).start()
        return process

    def request(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict | None = None,
        idempotency_key: str | None = None,
        timeout: float = 30,
        stream: bool = False,
    ) -> requests.Response:
        if (
            not path.startswith("/v1/")
            or "://" in path
            or method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}
        ):
            raise ValueError("Manager client request is outside the v1 contract.")
        headers = {
            "Authorization": f"Bearer {self.secret}",
            "Accept": "application/json",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        response = self.session.request(
            method,
            f"{self.descriptor.base_url.rstrip('/')}{path}",
            headers=headers,
            json=json_payload,
            timeout=timeout,
            stream=stream,
        )
        response_instance = str(
            response.headers.get("X-Pandrator-Manager-Instance") or ""
        )
        if not hmac.compare_digest(
            response_instance,
            self.descriptor.instance_id,
        ):
            raise ManagerUnavailable(
                "Manager response identity does not match its descriptor."
            )
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {"error": {"message": response.text[:1000]}}
            raise ManagerApiError(response.status_code, payload)
        return response

    def status(self) -> dict:
        return self.request("GET", "/v1/status").json()

    def capabilities(self) -> dict:
        return self.request("GET", "/v1/capabilities").json()

    def components(self) -> list[dict]:
        return self.request("GET", "/v1/components").json()["items"]

    def services(self) -> list[dict]:
        return self.request("GET", "/v1/services").json()["items"]

    def releases(self) -> dict:
        return self.request("GET", "/v1/releases").json()

    def doctor(self) -> DoctorReport:
        return DoctorReport.model_validate(
            self.request("GET", "/v1/doctor", timeout=120).json()
        )

    def legacy_report(self) -> dict:
        return self.request("GET", "/v1/legacy").json()

    def import_legacy(
        self,
        source_digest: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict:
        return self.request(
            "POST",
            "/v1/legacy/import",
            json_payload={
                "source_digest": source_digest,
                "confirmed": True,
            },
            idempotency_key=idempotency_key or str(uuid.uuid4()),
        ).json()

    def create_plan(
        self,
        kind: OperationKind,
        desired: dict[str, DesiredComponentState],
        *,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> OperationPlan:
        payload = {
            "kind": kind.value,
            "desired": {
                key: value.model_dump(mode="json")
                for key, value in desired.items()
            },
            "expected_revision": expected_revision,
        }
        response = self.request(
            "POST",
            "/v1/plans",
            json_payload=payload,
            idempotency_key=idempotency_key or str(uuid.uuid4()),
        )
        return OperationPlan.model_validate(response.json())

    def create_release_plan(
        self,
        manifest: dict,
        *,
        expected_revision: int | None = None,
        offline: bool = False,
        start_after_activation: bool = True,
        idempotency_key: str | None = None,
    ) -> OperationPlan:
        response = self.request(
            "POST",
            "/v1/releases/plans",
            json_payload={
                "manifest": manifest,
                "expected_revision": expected_revision,
                "offline": offline,
                "start_after_activation": start_after_activation,
            },
            idempotency_key=idempotency_key or str(uuid.uuid4()),
        )
        return OperationPlan.model_validate(response.json())

    def create_uninstall_plan(
        self,
        *,
        expected_revision: int | None = None,
        purge_data: bool = False,
        export_data: str | None = None,
        idempotency_key: str | None = None,
    ) -> OperationPlan:
        response = self.request(
            "POST",
            "/v1/uninstall/plans",
            json_payload={
                "expected_revision": expected_revision,
                "purge_data": purge_data,
                "export_data": export_data,
            },
            idempotency_key=idempotency_key or str(uuid.uuid4()),
        )
        return OperationPlan.model_validate(response.json())

    def submit_operation(
        self,
        plan: OperationPlan,
        *,
        accepted_confirmations: tuple[str, ...] = (),
        idempotency_key: str | None = None,
    ) -> OperationRecord:
        response = self.request(
            "POST",
            "/v1/operations",
            json_payload={
                "plan_id": plan.id,
                "plan_digest": plan.digest,
                "accepted_confirmations": list(accepted_confirmations),
            },
            idempotency_key=idempotency_key or str(uuid.uuid4()),
        )
        return OperationRecord.model_validate(response.json())

    def runtime(
        self,
        action: str,
        service_ids: tuple[str, ...] = (),
        *,
        idempotency_key: str | None = None,
    ) -> list[dict]:
        if action not in {"start", "stop", "restart"}:
            raise ValueError("Runtime action must be start, stop, or restart.")
        return self.request(
            "POST",
            f"/v1/runtime/{action}",
            json_payload={"service_ids": list(service_ids)},
            idempotency_key=idempotency_key or str(uuid.uuid4()),
            timeout=10 * 60,
        ).json()["items"]

    def recovery_url(self) -> str:
        return self.request(
            "POST",
            "/v1/recovery-sessions",
            json_payload={},
            idempotency_key=str(uuid.uuid4()),
        ).json()["url"]

    def _wait_for_shutdown_confirmation(
        self,
        *,
        timeout_seconds: float = 10,
    ) -> bool:
        """Confirm that the exact manager process exited after a lost reply."""

        identity = ProcessIdentity(
            pid=self.descriptor.pid,
            create_time=self.descriptor.process_create_time,
            executable=self.descriptor.executable,
            manager_instance_id=self.descriptor.instance_id,
        )
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                if validate_identity(identity) is None:
                    return True
            except IdentityMismatch:
                # The recorded PID now refers to another process, so the exact
                # manager instance addressed by this client has exited.
                return True
            except IdentityInspectionFailed:
                # A transient inspection failure is not enough to claim that
                # an authenticated shutdown request succeeded.
                pass
            time.sleep(0.05)
        return False

    def stop_manager(self) -> None:
        try:
            self.request(
                "POST",
                "/v1/runtime/stop-manager",
                json_payload={},
                idempotency_key=str(uuid.uuid4()),
            )
        except requests.ConnectionError:
            # The endpoint necessarily closes the server that is carrying its
            # own response. A slow event loop can therefore finish the
            # authenticated shutdown but drop the final response bytes.
            # Accept that disconnect only after the recorded PID/create-time/
            # executable identity is demonstrably gone.
            if self._wait_for_shutdown_confirmation():
                return
            raise
