"""Per-workspace Pandrator Manager daemon."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path

import psutil
from waitress import create_server

from . import __version__
from .api import create_api
from .application import ManagerApplication, create_application
from .auth import ensure_client_secret, protect_path
from .components.slots import active_component_path
from .context import WorkspaceLayout
from .models import (
    ConnectionDescriptor,
    DesiredComponentState,
    ManagedProcessSpec,
    ProcessIdentity,
)
from .network import (
    EndpointExposure,
    load_network_configuration,
)
from .operations import OperationEngine
from .processes.identity import (
    IdentityInspectionFailed,
    IdentityMismatch,
    capture_identity,
    validate_identity,
)
from .releases.handoff import (
    ManagerHandoffCoordinator,
    pending_handoffs,
    run_handoff,
)
from .runtime_specs import (
    component_runtime_spec,
    pandrator_runtime_specs,
    silero_runtime_spec,
)
from .supervisor import ProcessSupervisor
from .uninstall import (
    UninstallHandoffCoordinator,
    pending_uninstalls,
    uninstall_control_root,
    uninstall_helper_command,
)


class ManagerAlreadyRunning(RuntimeError):
    pass


class ManagerInstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.instance_id = str(uuid.uuid4())
        self.acquired = False
        self.identity: ProcessIdentity | None = None

    def acquire(self) -> ProcessIdentity:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        protect_path(self.path.parent, directory=True)
        identity = capture_identity(
            psutil.Process(os.getpid()),
            manager_instance_id=self.instance_id,
        )
        payload = {
            **identity.model_dump(mode="json"),
            "created_at": time.time(),
        }
        for _attempt in range(2):
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                try:
                    current = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    current = {}
                raw_pid = 0
                try:
                    raw_pid = int(current.get("pid") or 0)
                except (AttributeError, TypeError, ValueError):
                    pass
                try:
                    existing_identity = ProcessIdentity.model_validate(
                        {
                            key: current.get(key)
                            for key in (
                                "pid",
                                "create_time",
                                "executable",
                                "manager_instance_id",
                            )
                        }
                    )
                    identity_complete = True
                    owner = validate_identity(existing_identity)
                except ValueError:
                    identity_complete = False
                    owner = None
                except IdentityMismatch:
                    identity_complete = True
                    owner = None
                except IdentityInspectionFailed as error:
                    raise ManagerAlreadyRunning(
                        "Could not safely inspect the existing manager owner."
                    ) from error
                if owner is not None:
                    raise ManagerAlreadyRunning(
                        f"Workspace is already managed by PID {owner.pid}."
                    ) from None
                if (
                    not identity_complete
                    and raw_pid > 0
                    and psutil.pid_exists(raw_pid)
                ):
                    raise ManagerAlreadyRunning(
                        "A live legacy manager lock cannot be verified safely."
                    ) from None
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            protect_path(self.path)
            self.acquired = True
            self.identity = identity
            return identity
        raise ManagerAlreadyRunning("Could not acquire the manager instance lock.")

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if payload.get("manager_instance_id") == self.instance_id:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self.acquired = False


def _atomic_descriptor(path: Path, descriptor: ConnectionDescriptor) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            handle.write(descriptor.model_dump_json(indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        protect_path(temporary)
        os.replace(temporary, path)
        protect_path(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _remove_descriptor(path: Path, instance_id: str) -> None:
    try:
        descriptor = ConnectionDescriptor.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return
    if descriptor.instance_id == instance_id:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def installed_component_runtime_specs(
    application: ManagerApplication,
    layout: WorkspaceLayout,
) -> tuple[ManagedProcessSpec, ...]:
    """Build launch specs only for active, manager-qualified component slots."""

    stored = application.store.component_records()
    specs: list[ManagedProcessSpec] = []
    for definition in application.registry.definitions():
        if not definition.service_key:
            continue
        if active_component_path(layout, definition.id) is None:
            continue
        desired = stored.get(definition.id, (None, None))[0]
        if desired is None:
            desired = DesiredComponentState()
        if not desired.present:
            continue
        try:
            resolved = application.registry.driver(definition.id).resolve(
                application.context,
                definition,
                desired,
            )
            spec = component_runtime_spec(layout, definition.id, resolved)
        except Exception:
            logging.exception(
                "Could not resolve managed runtime for %s",
                definition.id,
            )
            continue
        if spec is not None:
            specs.append(spec)
    return tuple(specs)


def run_daemon(
    workspace: str | Path,
    *,
    port: int | None = None,
    register_silero: bool = True,
    handoff_child: str | None = None,
) -> int:
    layout = WorkspaceLayout.from_value(workspace)
    if handoff_child is None:
        manager_pending = pending_handoffs(layout)
        uninstall_pending = pending_uninstalls(layout)
        if len(manager_pending) + len(uninstall_pending) > 1:
            raise RuntimeError(
                "More than one pending external handoff requires recovery."
            )
        if uninstall_pending:
            command = uninstall_helper_command(
                layout,
                uninstall_pending[0],
            )
            return subprocess.run(
                command,
                cwd=uninstall_control_root(layout),
                stdin=subprocess.DEVNULL,
                shell=False,
                check=False,
            ).returncode
        if manager_pending:
            return run_handoff(layout.workspace, manager_pending[0])
    layout.ensure_base_directories()
    protect_path(layout.state, directory=True)
    network = load_network_configuration(layout)
    manager_exposure = network.manager
    if port is not None:
        manager_exposure = EndpointExposure.model_validate(
            {
                **manager_exposure.model_dump(mode="python"),
                "port": int(port),
            }
        )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            RotatingFileHandler(
                layout.logs / "manager.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            ),
            logging.StreamHandler(),
        ],
    )
    lock = ManagerInstanceLock(layout.instance_lock)
    identity = lock.acquire()
    server = None
    supervisor = None
    operation_engine = None
    try:
        secret = ensure_client_secret(layout.credential)
        owner_password = str(
            os.environ.pop("PANDRATOR_OWNER_PASSWORD", "") or ""
        )
        application_environment = (
            {"PANDRATOR_OWNER_PASSWORD": owner_password}
            if owner_password
            else {}
        )
        application = create_application(layout.workspace)
        application.instance_id = lock.instance_id
        supervisor = ProcessSupervisor(
            application.context,
            application.store,
            manager_instance_id=lock.instance_id,
        )
        supervisor.register_many(
            pandrator_runtime_specs(
                layout,
                exposure=network.application,
                preferences={
                    **application.pandrator_runtime_environment(),
                    **application_environment,
                },
            )
        )
        installed_specs = installed_component_runtime_specs(application, layout)
        supervisor.register_many(installed_specs)
        registered_services = {spec.service_id for spec in installed_specs}
        if (
            register_silero
            and "tts.silero" not in registered_services
            and (layout.root / "silero-fastapi" / "pyproject.toml").is_file()
        ):
            supervisor.register(silero_runtime_spec(layout))
        supervisor.validate_complete()
        supervisor.start_monitoring()
        server_box: dict[str, object] = {}
        shutdown_started = threading.Event()

        def request_shutdown() -> None:
            selected = server_box.get("server")
            if selected is not None and not shutdown_started.is_set():
                shutdown_started.set()

                def close_server() -> None:
                    # Let the stop response flush, then close keep-alive
                    # channels from inside Waitress's asyncore thread. Closing
                    # its wakeup descriptor from this request thread races the
                    # active poll and produces a false EBADF traceback.
                    time.sleep(0.2)

                    def close_in_event_loop() -> None:
                        selected.asyncore.close_all(map=selected._map)

                    try:
                        selected.trigger.pull_trigger(close_in_event_loop)
                    except OSError:
                        logging.debug(
                            "Manager API shutdown trigger was already closed."
                        )

                threading.Thread(
                    target=close_server,
                    name="manager-shutdown",
                    daemon=True,
                ).start()

        manager_handoff = ManagerHandoffCoordinator(
            layout,
            shutdown_callback=request_shutdown,
        )
        uninstall_handoff = UninstallHandoffCoordinator(
            layout,
            shutdown_callback=request_shutdown,
        )

        def coordinate_handoff(execution, result) -> None:
            if result.get("handoff_kind") == "uninstall":
                uninstall_handoff(execution, result)
            else:
                manager_handoff(execution, result)

        operation_engine = OperationEngine(
            application.context,
            application.store,
            application.registry,
            supervisor=supervisor,
            lifecycle_lock=application.lifecycle_lock,
            release_authority=application.release_authority,
            manager_handoff_callback=coordinate_handoff,
            service_spec_factory=(
                lambda component_id, resolved: component_runtime_spec(
                    layout,
                    component_id,
                    resolved,
                )
            ),
        )
        application.attach_operation_queue(operation_engine)
        operation_engine.start()

        api = create_api(
            application,
            supervisor,
            client_secret=secret,
            shutdown_callback=request_shutdown,
            manager_exposure=manager_exposure,
            application_exposure=network.application,
            application_environment=application_environment,
        )
        owner_password = ""
        server = create_server(
            api,
            host=manager_exposure.bind_host,
            port=manager_exposure.port,
            threads=8,
            channel_timeout=120,
        )
        server_box["server"] = server
        effective_port = int(server.effective_port)
        descriptor_host = (
            "[::1]" if ":" in manager_exposure.probe_host else "127.0.0.1"
        )
        descriptor = ConnectionDescriptor(
            manager_version=__version__,
            workspace=str(layout.workspace),
            base_url=f"http://{descriptor_host}:{effective_port}",
            public_url=manager_exposure.public_url,
            instance_id=lock.instance_id,
            pid=identity.pid,
            process_create_time=identity.create_time,
            executable=identity.executable,
        )
        _atomic_descriptor(layout.descriptor, descriptor)

        def restore_desired_services() -> None:
            failures = supervisor.restore_desired()
            for service_id, message in failures.items():
                logging.error(
                    "Could not restore desired managed service %s: %s",
                    service_id,
                    message,
                )

        threading.Thread(
            target=restore_desired_services,
            name="manager-restore-desired-services",
            daemon=True,
        ).start()

        def signal_shutdown(_signum, _frame) -> None:
            request_shutdown()

        for signal_name in ("SIGINT", "SIGTERM"):
            selected_signal = getattr(signal, signal_name, None)
            if selected_signal is not None:
                signal.signal(selected_signal, signal_shutdown)
        logging.info(
            "Pandrator Manager %s listening locally on %s%s",
            __version__,
            descriptor.base_url,
            (
                f" and exposed as {manager_exposure.public_url}"
                if manager_exposure.remote_enabled
                else ""
            ),
        )
        server.run()
        return 0
    finally:
        if operation_engine is not None:
            operation_engine.shutdown()
        if supervisor is not None:
            supervisor.shutdown(stop_children=False)
        if server is not None:
            try:
                server.task_dispatcher.shutdown(
                    cancel_pending=True,
                    timeout=5,
                )
                server.close()
            except Exception:
                logging.exception("Could not close manager API server")
        _remove_descriptor(layout.descriptor, lock.instance_id)
        lock.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pandrator-manager-daemon")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--port", type=int)
    parser.add_argument("--no-silero", action="store_true")
    parser.add_argument("--handoff-child")
    args = parser.parse_args(argv)
    try:
        return run_daemon(
            args.workspace,
            port=args.port,
            register_silero=not args.no_silero,
            handoff_child=args.handoff_child,
        )
    except ManagerAlreadyRunning as error:
        print(str(error), file=sys.stderr)
        return 3
    except Exception:
        logging.exception("Pandrator Manager failed")
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
