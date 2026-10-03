"""Built-in typed task handlers for staged component slots and ownership."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import zipfile as zipfile
from collections.abc import Mapping
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dulwich import porcelain
from dulwich.repo import Repo

from ..artifacts import ArtifactDownloader, ArtifactSpec, SafeExtractor
from ..components.audiocpp import (
    AUDIO_CPP_MODEL_REVISION,
    AUDIO_CPP_VERSION,
    AudioCppModelPackage,
    model_package,
    server_config,
    source_markers_for,
)
from ..components.audiocpp_reuse import reuse_verified_package
from ..components.crispasr import CRISPASR_VERSION
from ..components.runtime_bootstrap import generated_runtime_files
from ..components.slots import (
    active_component_path,
    component_container,
    component_pointer,
)
from ..context import WorkspaceLayout
from ..environments import PIXI_VERSION, PixiBootstrapper
from ..errors import ManagerError
from ..legacy_data import (
    legacy_data_inventory,
)
from ..legacy_data import (
    reconcile_legacy_data as reconcile_legacy_data,
)
from ..legacy_data import (
    rollback_legacy_data as rollback_legacy_data,
)
from ..models import (
    ManagedProcessSpec,
    TaskSpec,
)
from ..models import (
    PreflightCheck as PreflightCheck,
)
from ..network import load_network_configuration
from ..preflight import HostPreflight
from ..processes import CommandRunner, CommandSpec
from ..releases.authority import ReleaseAuthority as ReleaseAuthority
from ..releases.bundles import (
    release_cache_path as release_cache_path,
)
from ..releases.bundles import (
    validate_release_bundle as validate_release_bundle,
)
from ..releases.handoff import (
    prepare_manager_handoff as prepare_manager_handoff,
)
from ..releases.handoff import (
    rollback_prepared_manager_handoff as rollback_prepared_manager_handoff,
)
from ..releases.models import ReleaseArtifact as ReleaseArtifact
from ..releases.slots import ReleaseSlotManager as ReleaseSlotManager
from ..runtime_specs import (
    PANDRATOR_API_SERVICE as PANDRATOR_API_SERVICE,
)
from ..runtime_specs import (
    PANDRATOR_CORE_SERVICES as PANDRATOR_CORE_SERVICES,
)
from ..runtime_specs import (
    PANDRATOR_MCP_SERVICE,
    PANDRATOR_SERVICE_STOP_ORDER,
    PANDRATOR_WORKER_SERVICE,
    pandrator_runtime_specs,
    runtime_python,
)
from ..runtime_specs import (
    PANDRATOR_SERVICE_START_ORDER as PANDRATOR_SERVICE_START_ORDER,
)
from ..state import ManagerStore
from ..tls import CABundleSelection, dulwich_config_with_ca, select_ca_bundle
from ..uninstall import (
    prepare_uninstall_handoff,
    rollback_prepared_uninstall,
)
from .contracts import OperationTaskContext as OperationTaskContext
from .contracts import UnsupportedTask as UnsupportedTask
from .release_tasks import ReleaseTasks
from .uninstall_tasks import UninstallTasks


def _is_tls_verification_error(error: BaseException) -> bool:
    inspected: set[int] = set()
    pending: list[BaseException] = [error]
    while pending and len(inspected) < 16:
        selected = pending.pop()
        if id(selected) in inspected:
            continue
        inspected.add(id(selected))
        message = str(selected).casefold()
        if any(
            marker in message
            for marker in (
                "certificate_verify_failed",
                "certificate verify failed",
                "unable to get local issuer certificate",
                "self-signed certificate",
                "hostname mismatch",
            )
        ):
            return True
        for nested in (selected.__cause__, selected.__context__):
            if isinstance(nested, BaseException):
                pending.append(nested)
        pending.extend(item for item in selected.args if isinstance(item, BaseException))
    return False


def _source_acquisition_error(
    *,
    error: Exception,
    label: str,
    repo_url: str,
    ca_bundle: CABundleSelection,
) -> ManagerError:
    host = str(urlsplit(repo_url).hostname or "the source host")
    details = {
        "host": host,
        "ca_bundle_source": ca_bundle.source,
        "error_type": type(error).__name__,
    }
    if _is_tls_verification_error(error):
        return ManagerError(
            "source_tls_verification_failed",
            f"Pandrator could not verify the TLS certificate while downloading "
            f"{label} from {host}. Check the computer's date and time and any "
            "HTTPS-inspecting proxy, then download the diagnostic bundle if "
            "the problem continues.",
            details,
            502,
        )
    return ManagerError(
        "source_download_failed",
        f"Pandrator could not download {label} from {host}. Check the internet "
        "and proxy connection, then download the diagnostic bundle if the "
        "problem continues.",
        details,
        502,
    )


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt" and content.startswith("#!"):
            path.chmod(path.stat().st_mode | 0o755)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class FilesystemTaskHandler(ReleaseTasks, UninstallTasks):
    """Executes only manager-generated task kinds; no raw API command exists."""

    @staticmethod
    def _release_runtime_specs(layout: WorkspaceLayout) -> tuple[ManagedProcessSpec, ...]:
        return pandrator_runtime_specs(layout)

    @staticmethod
    def _prepare_uninstall_handoff(
        *,
        layout: WorkspaceLayout,
        store: ManagerStore,
        operation_id: str,
        expected_revision: int,
        purge_data: bool,
        export_data: str | None,
        prior_services: Mapping[str, Any],
    ) -> dict[str, Any]:
        return prepare_uninstall_handoff(
            layout=layout,
            store=store,
            operation_id=operation_id,
            expected_revision=expected_revision,
            purge_data=purge_data,
            export_data=export_data,
            prior_services=prior_services,
        )

    @staticmethod
    def _rollback_prepared_uninstall(
        *,
        layout: WorkspaceLayout,
        operation_id: str,
    ) -> None:
        rollback_prepared_uninstall(
            layout=layout,
            operation_id=operation_id,
        )

    supported_kinds = frozenset(
        {
            "stage_component",
            "stage_crispasr",
            "stage_audio_cpp",
            "verify_component",
            "activate_component",
            "validate_service",
            "stop_service",
            "remove_owned_component",
            "preflight_operation",
            "ensure_runtime_tool",
            "verify_release",
            "download_release",
            "stage_release",
            "stop_application_release",
            "reconcile_legacy_data",
            "activate_application_release",
            "prepare_manager_handoff",
            "start_application",
            "stop_all_services",
            "export_uninstall_data",
            "prepare_uninstall_handoff",
        }
    )

    def execute(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        if task.kind not in self.supported_kinds:
            raise UnsupportedTask(f"Unsupported operation task kind: {task.kind}")
        return getattr(self, f"_execute_{task.kind}")(execution, task)

    def rollback(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
        result: dict,
    ) -> None:
        method = getattr(self, f"_rollback_{task.kind}", None)
        if method is not None:
            method(execution, task, result)

    @staticmethod
    def _definition(execution: OperationTaskContext, task: TaskSpec):
        if not task.component_id:
            raise ValueError(f"Task {task.id} has no component owner.")
        return execution.registry.definition(task.component_id)

    def _staging_source(
        self,
        execution: OperationTaskContext,
        component_id: str,
    ) -> Path:
        target = execution.context.layout.staging / execution.operation.id / component_id / "source"
        return execution.context.layout.require_within(
            target,
            roots=(execution.context.layout.staging,),
        )

    def _execute_stage_component(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        execution.check_cancelled()
        definition = self._definition(execution, task)
        if not definition.repo_url:
            raise UnsupportedTask(
                f"{definition.label} has no signed or repository-backed "
                "installation source in this manager release."
            )
        target = self._staging_source(execution, definition.id)
        if target.is_dir():
            self._prepare_runtime_adapter(target, definition.id)
        if target.is_dir() and self._markers_present(
            target,
            definition.source_markers,
        ):
            self._ensure_source_revision(target, definition)
            return {
                "staged_path": str(target),
                "revision": self._revision(target),
                "reused": True,
            }
        if target.exists():
            execution.context.layout.require_within(
                target,
                roots=(execution.context.layout.staging,),
            )
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        git_config, ca_bundle = dulwich_config_with_ca(execution.context.environment)
        try:
            cloned = porcelain.clone(
                definition.repo_url,
                str(target),
                checkout=True,
                config=git_config,
            )
        except Exception as error:
            raise _source_acquisition_error(
                error=error,
                label=definition.label,
                repo_url=definition.repo_url,
                ca_bundle=ca_bundle,
            ) from error
        # Dulwich can retain pack files on Windows. Close before the
        # path-based revision reset so a failed checkout cannot leak handles.
        try:
            cloned.close()
        except Exception as error:
            raise RuntimeError(
                f"{definition.label} source repository could not be closed."
            ) from error
        if definition.source_revision:
            self._ensure_source_revision(target, definition)
        self._prepare_runtime_adapter(target, definition.id)
        execution.check_cancelled()
        return {
            "staged_path": str(target),
            "revision": self._revision(target),
            "reused": False,
        }

    @staticmethod
    def _prepare_runtime_adapter(target: Path, component_id: str) -> None:
        for relative, content in generated_runtime_files(component_id).items():
            _atomic_text(target / relative, content)

    @classmethod
    def _ensure_source_revision(cls, target: Path, definition) -> None:
        """Ensure a reusable Git source tree is at its requested revision."""

        requested = definition.source_revision
        if not requested:
            return
        if cls._revision(target).casefold().startswith(requested.casefold()):
            return
        try:
            porcelain.reset(
                str(target),
                mode="hard",
                treeish=requested,
            )
        except Exception as error:
            raise RuntimeError(
                f"{definition.label} source revision {requested} could not be checked out."
            ) from error
        selected = cls._revision(target)
        if not selected.casefold().startswith(requested.casefold()):
            raise RuntimeError(
                f"{definition.label} source revision {requested} was not "
                f"checked out (selected {selected})."
            )

    def _execute_stage_crispasr(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        """Acquire, extract, and probe the exact pinned native runtime."""

        execution.check_cancelled()
        definition = self._definition(execution, task)
        raw_asset = task.inputs.get("asset")
        if not isinstance(raw_asset, dict):
            raise UnsupportedTask("The CrispASR asset contract is missing.")
        try:
            filename = str(raw_asset["filename"])
            specification = ArtifactSpec(
                url=str(raw_asset["url"]),
                sha256=str(raw_asset["sha256"]),
                filename=filename,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise UnsupportedTask("The CrispASR asset contract is invalid.") from error
        if (
            not filename
            or filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
            or Path(filename).name != filename
        ):
            raise UnsupportedTask("The CrispASR asset filename is not a safe cache basename.")

        target = self._staging_source(execution, definition.id)
        executable_name = "crispasr.exe" if os.name == "nt" else "crispasr"
        existing_executable = target / executable_name
        if (
            target.is_dir()
            and (target / "install.json").is_file()
            and existing_executable.is_file()
        ):
            return {
                "staged_path": str(target),
                "revision": (
                    f"crispasr-{CRISPASR_VERSION}-{task.inputs.get('effective_compute') or 'cpu'}"
                ),
                "reused": True,
            }

        staging_root = target.parent
        execution.context.layout.require_within(
            staging_root,
            roots=(execution.context.layout.staging,),
        )
        if staging_root.exists():
            shutil.rmtree(staging_root)
        staging_root.mkdir(parents=True, exist_ok=True)

        cache_path = execution.context.layout.cache / "artifacts" / "crispasr" / filename
        selected = ArtifactDownloader(
            cancellation=execution.cancellation,
            environment=execution.context.environment,
        ).download(specification, cache_path)
        unpacked = staging_root / "unpacked"
        SafeExtractor().extract(selected, unpacked)
        executable = next(
            (candidate for candidate in unpacked.rglob(executable_name) if candidate.is_file()),
            None,
        )
        if executable is None:
            raise RuntimeError(f"The verified CrispASR archive did not contain {executable_name}.")
        shutil.copytree(executable.parent, target)
        staged_executable = target / executable_name
        if os.name != "nt":
            staged_executable.chmod(staged_executable.stat().st_mode | 0o755)
        probe = CommandRunner(
            cancellation=execution.cancellation,
            base_environment=execution.context.environment,
        ).run(
            CommandSpec(
                argv=(str(staged_executable), "--version"),
                cwd=target,
                timeout_seconds=30,
                label="crispasr-version",
            )
        )
        version_output = "\n".join((probe.stdout, probe.stderr))
        if f"version       : {CRISPASR_VERSION}" not in version_output:
            raise RuntimeError("The verified CrispASR binary reported an unexpected version.")
        _atomic_json(
            target / "install.json",
            {
                "version": CRISPASR_VERSION,
                "requested_backend": str(task.inputs.get("requested_compute") or "auto"),
                "effective_backend": str(task.inputs.get("effective_compute") or "cpu"),
                "runtime_variant": str(raw_asset.get("runtime_variant") or "cpu"),
                "compiled_backends": list(raw_asset.get("compiled_backends") or ()),
                "asset": filename,
                "sha256": specification.sha256,
                "default_model": (
                    (task.inputs.get("resolved") or {})
                    .get("options", {})
                    .get("engine", "parakeet-tdt-0.6b-v3")
                ),
                "default_quantization": (
                    (task.inputs.get("resolved") or {}).get("quantization") or "q8_0"
                ),
            },
        )
        shutil.rmtree(unpacked)
        execution.check_cancelled()
        return {
            "staged_path": str(target),
            "revision": (
                f"crispasr-{CRISPASR_VERSION}-{task.inputs.get('effective_compute') or 'cpu'}"
            ),
            "reused": False,
            "asset_path": str(selected),
        }

    @staticmethod
    def _source_markers(execution: OperationTaskContext, definition) -> tuple[str, ...]:
        if definition.id == "audio_cpp":
            return source_markers_for(execution.context.system)
        return definition.source_markers

    @staticmethod
    def _safe_archive_filename(filename: str, label: str) -> None:
        if (
            not filename
            or filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
            or Path(filename).name != filename
        ):
            raise UnsupportedTask(f"The {label} archive filename is not a safe cache basename.")

    @staticmethod
    def _merge_model_package_provenance(
        marker: Path,
        package: AudioCppModelPackage,
    ) -> None:
        package_id = package.id
        payload: dict[str, Any] = {}
        if marker.is_file():
            try:
                selected = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    f"audio.cpp model package marker for {package_id} is invalid."
                ) from error
            if not isinstance(selected, dict):
                raise RuntimeError(f"audio.cpp model package marker for {package_id} is invalid.")
            payload = selected
        payload.update(
            {
                "package_id": package_id,
                "provenance": {
                    "manager": "audio.cpp tools/model_manager_v2.py",
                    "repository": package.repository,
                    "revision": package.revision,
                    "requested_revision": package.revision,
                    "digest_verified": True,
                    "sha256": dict(zip(package.files, package.sha256, strict=True)),
                },
            }
        )
        _atomic_json(marker, payload)

    @staticmethod
    def _pin_audio_cpp_model_specs(
        specs_root: Path,
        packages: list[AudioCppModelPackage],
    ) -> None:
        """Override selected upstream package specs with our immutable revision."""

        remaining = {package.id for package in packages}
        packages_by_id = {package.id: package for package in packages}
        for path in sorted(specs_root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError) as error:
                raise RuntimeError(f"Invalid audio.cpp model spec: {path.name}.") from error
            entries = payload.get("packages") if isinstance(payload, dict) else None
            if not isinstance(entries, list):
                continue
            changed = False
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("id") not in remaining:
                    continue
                download = entry.get("download")
                if download is None:
                    download = {}
                    entry["download"] = download
                if not isinstance(download, dict):
                    raise RuntimeError(f"Invalid audio.cpp download spec for {entry.get('id')}.")
                package = packages_by_id[str(entry["id"])]
                download.update(
                    {
                        "kind": package.download_kind,
                        "repo": package.repository,
                        "revision": package.revision,
                    }
                )
                # model_manager_v2 reads file layout from the package, not
                # from its nested download connection settings.
                entry.update({
                    "files": list(package.download_files or tuple(
                        f"{package.target_directory}/{path}" for path in package.files
                    )),
                    "strip_prefix": package.strip_prefix if package.download_files else package.target_directory,
                    "target_directory": package.target_directory,
                })
                remaining.remove(package.id)
                changed = True
            if changed:
                _atomic_json(path, payload)
        if remaining:
            raise RuntimeError(
                "The verified audio.cpp archive did not contain model specs for: "
                + ", ".join(sorted(remaining))
                + "."
            )

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def _execute_stage_audio_cpp(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        """Acquire the signed runtime and stage model-manager packages."""

        execution.check_cancelled()
        definition = self._definition(execution, task)
        raw_assets = task.inputs.get("assets")
        if not isinstance(raw_assets, list) or not raw_assets:
            raise UnsupportedTask("The audio.cpp runtime asset contract is missing.")
        specifications: list[tuple[dict, str, ArtifactSpec]] = []
        try:
            for raw_asset in raw_assets:
                if not isinstance(raw_asset, dict):
                    raise ValueError("asset entry is not an object")
                filename = str(raw_asset["filename"])
                self._safe_archive_filename(filename, "audio.cpp runtime")
                specifications.append(
                    (
                        raw_asset,
                        filename,
                        ArtifactSpec(
                            url=str(raw_asset["url"]),
                            sha256=str(raw_asset["sha256"]),
                            filename=filename,
                        ),
                    )
                )
        except (KeyError, TypeError, ValueError) as error:
            raise UnsupportedTask("The audio.cpp runtime asset contract is invalid.") from error

        raw_models = task.inputs.get("models")
        if not isinstance(raw_models, list) or not raw_models:
            raise UnsupportedTask("The audio.cpp model package contract is missing.")
        try:
            packages = [model_package(str(package_id)) for package_id in raw_models]
        except ValueError as error:
            raise UnsupportedTask(str(error)) from error
        if len({package.id for package in packages}) != len(packages):
            raise UnsupportedTask("The audio.cpp model package contract contains duplicates.")

        effective = str(task.inputs.get("effective_compute") or "cpu")
        # Model selections and repairs can change a slot without changing the
        # runtime version. Only retries of this operation may reuse its slot.
        revision = (
            f"audio-cpp-{task.inputs.get('version') or AUDIO_CPP_VERSION}-"
            f"{effective}-{execution.operation.id}"
        )

        target = self._staging_source(execution, definition.id)
        markers = self._source_markers(execution, definition)
        if target.is_dir() and self._markers_present(target, markers):
            if all(
                package.marker_path(target / "models").is_file()
                and all(
                    path.is_file() and self._sha256_file(path) == expected_sha256
                    for path, expected_sha256 in zip(
                        package.required_paths(target / "models"),
                        package.sha256,
                        strict=True,
                    )
                )
                for package in packages
            ):
                return {
                    "staged_path": str(target),
                    "revision": revision,
                    "models": [package.id for package in packages],
                    "reused": True,
                }

        staging_root = target.parent
        execution.context.layout.require_within(
            staging_root,
            roots=(execution.context.layout.staging,),
        )
        if staging_root.exists():
            shutil.rmtree(staging_root)
        staging_root.mkdir(parents=True, exist_ok=True)
        unpacked = staging_root / "unpacked"
        offline = bool(task.inputs.get("offline"))
        if offline:
            raise UnsupportedTask(
                "Offline audio.cpp installation is unavailable because the Manager "
                "does not yet cache model packages for offline reuse. Pandrator "
                "verifies immutable model payloads, but model installation still "
                "requires an online fetch."
            )
        selected_assets: list[str] = []
        for raw_asset, filename, specification in specifications:
            execution.check_cancelled()
            cache_path = (
                execution.context.layout.cache / "artifacts" / "audio_cpp" / filename
            )
            selected = ArtifactDownloader(
                cancellation=execution.cancellation,
                environment=execution.context.environment,
            ).download(specification, cache_path, offline=offline)
            SafeExtractor().extract(selected, unpacked)
            selected_assets.append(str(raw_asset.get("filename") or specification.filename))

        # Upstream runtime archives are root-normalized: the server binary,
        # model-manager script, and model_specs directory live directly below
        # the archive root. Refuse an unexpected enclosing/member root instead
        # of guessing and risking a broken or transient path in the slot.
        server_name = (
            "audiocpp_server.exe"
            if execution.context.system.casefold() == "windows"
            else "audiocpp_server"
        )
        server_source = unpacked / server_name
        manager_source = unpacked / "tools" / "model_manager_v2.py"
        if not server_source.is_file() or not manager_source.is_file():
            missing = [
                str(path.relative_to(unpacked))
                for path in (server_source, manager_source)
                if not path.is_file()
            ]
            raise RuntimeError(
                "The verified audio.cpp archive did not contain the expected "
                "archive-root files: " + ", ".join(missing) + "."
            )
        self._pin_audio_cpp_model_specs(unpacked / "model_specs", packages)
        target.mkdir(parents=True, exist_ok=True)
        for source in unpacked.iterdir():
            destination = target / source.name
            if source.is_dir():
                shutil.copytree(source, destination)
            else:
                shutil.copy2(source, destination)
        staged_server = target / server_name
        if execution.context.system.casefold() != "windows":
            staged_server.chmod(staged_server.stat().st_mode | 0o755)
            staged_cli = target / "audiocpp_cli"
            if staged_cli.is_file():
                staged_cli.chmod(staged_cli.stat().st_mode | 0o755)

        models_root = target / "models"
        python_candidate = runtime_python(execution.context.layout)
        if python_candidate.is_file():
            python_executable = str(python_candidate)
        elif not getattr(sys, "frozen", False):
            python_executable = sys.executable
        else:
            python_executable = shutil.which("python3") or shutil.which("python") or sys.executable
        invocations: list[list[str]] = []
        active_slot = active_component_path(execution.context.layout, definition.id)
        reused_models: dict[str, str] = {}
        model_installer_environment = {
            "SSL_CERT_FILE": str(select_ca_bundle(execution.context.environment).path)
        }
        for package in packages:
            execution.check_cancelled()
            reuse_mode = reuse_verified_package(
                active_slot, models_root, package,
                self._sha256_file, execution.check_cancelled,
            )
            if reuse_mode is not None:
                reused_models[package.id] = reuse_mode
                self._merge_model_package_provenance(
                    package.marker_path(models_root), package,
                )
                continue
            invocation = [
                python_executable,
                str(target / "tools" / "model_manager_v2.py"),
                "install",
                package.id,
                "--models-root",
                str(models_root),
            ]
            invocations.append(invocation)
            CommandRunner(
                cancellation=execution.cancellation,
                base_environment=execution.context.environment,
            ).run(
                CommandSpec(
                    argv=tuple(invocation),
                    cwd=target,
                    env=model_installer_environment,
                    timeout_seconds=2 * 60 * 60,
                    label=f"audio-cpp-model-{package.id}",
                )
            )
            missing_outputs = [
                path for path in package.required_paths(models_root) if not path.is_file()
            ]
            if missing_outputs:
                raise RuntimeError(
                    f"audio.cpp model manager did not install every required file "
                    f"for {package.id}: "
                    + ", ".join(str(path.relative_to(models_root)) for path in missing_outputs)
                )
            for path, expected_sha256 in zip(
                package.required_paths(models_root), package.sha256, strict=True
            ):
                actual_sha256 = self._sha256_file(path)
                if actual_sha256 != expected_sha256:
                    raise RuntimeError(
                        f"audio.cpp model package {package.id} failed SHA-256 verification: "
                        f"{path.relative_to(models_root)}."
                    )
            self._merge_model_package_provenance(
                package.marker_path(models_root),
                package,
            )

        _atomic_json(
            target / "server.json", server_config(effective, [package.id for package in packages])
        )
        _atomic_json(
            target / "install.json",
            {
                "component": definition.id,
                "version": str(task.inputs.get("version") or AUDIO_CPP_VERSION),
                "requested_backend": str(task.inputs.get("requested_compute") or "auto"),
                "effective_backend": effective,
                "assets": selected_assets,
                "models": [package.id for package in packages],
                "model_revisions": {
                    package.id: package.revision
                    for package in packages
                },
                "model_repositories": {
                    package.id: package.repository
                    for package in packages
                },
                # Retain the historical summary field for older readers;
                # model_revisions is authoritative when packages have mixed pins.
                "model_revision": AUDIO_CPP_MODEL_REVISION,
                "model_digest_verification": "sha256",
                "model_reuse": reused_models,
            },
        )
        shutil.rmtree(unpacked)
        execution.check_cancelled()
        return {
            "staged_path": str(target),
            "revision": revision,
            "models": [package.id for package in packages],
            "assets": selected_assets,
            "model_manager": invocations,
            "reused_models": reused_models,
            "reused": False,
        }

    def _execute_preflight_operation(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        del task
        checks = HostPreflight(
            execution.context,
            execution.registry,
        ).evaluate(
            desired=execution.plan.desired,
            tasks=execution.plan.tasks,
        )
        HostPreflight.require_success(checks)
        return {"checks": [check.model_dump(mode="json") for check in checks]}

    def _execute_ensure_runtime_tool(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        tool = str(task.inputs.get("tool") or "")
        version = str(task.inputs.get("version") or "")
        if tool != "pixi" or version != PIXI_VERSION:
            raise UnsupportedTask(f"Unsupported runtime tool requirement: {tool} {version}")
        bootstrapper = PixiBootstrapper(
            execution.context,
            runner=CommandRunner(
                cancellation=execution.cancellation,
                base_environment=execution.context.environment,
            ),
            downloader=ArtifactDownloader(
                cancellation=execution.cancellation,
                environment=execution.context.environment,
            ),
        )
        target = bootstrapper.target.resolve(strict=False)
        manager_owned = any(
            record["owner_kind"] == "runtime_tool"
            and record["owner_id"] == "pixi"
            and Path(record["path"]).resolve(strict=False) == target
            for record in execution.store.owned_paths()
        )
        return bootstrapper.ensure(
            execution.context.layout.staging / execution.operation.id,
            execution.context.layout.backups / execution.operation.id,
            replace_existing=manager_owned,
            offline=any(
                bool(state.options.get("offline")) for state in execution.plan.desired.values()
            ),
        )

    def _rollback_ensure_runtime_tool(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
        result: dict,
    ) -> None:
        del result
        if str(task.inputs.get("tool") or "") != "pixi":
            return
        PixiBootstrapper(execution.context).rollback(
            execution.context.layout.staging / execution.operation.id
        )

    @staticmethod
    def _revision(repository: Path) -> str:
        try:
            with Repo(str(repository)) as selected:
                return selected.head().decode("ascii")
        except Exception:
            return "unversioned"

    @staticmethod
    def _markers_present(root: Path, markers: tuple[str, ...]) -> bool:
        return bool(markers) and all((root / marker).exists() for marker in markers)

    def _stage_result(
        self,
        execution: OperationTaskContext,
        component_id: str,
    ) -> dict:
        key = f"{component_id}:stage"
        try:
            return execution.prior_results[key]
        except KeyError:
            raise RuntimeError(f"Missing staged result for {component_id}.") from None

    def _execute_verify_component(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        definition = self._definition(execution, task)
        stage = self._stage_result(execution, definition.id)
        root = Path(stage["staged_path"]).resolve(strict=False)
        execution.context.layout.require_within(
            root,
            roots=(execution.context.layout.staging,),
        )
        source_markers = self._source_markers(execution, definition)
        missing = [marker for marker in source_markers if not (root / marker).exists()]
        if missing:
            raise RuntimeError(
                f"{definition.label} staging is incomplete; missing " + ", ".join(missing)
            )
        return {
            "verified_path": str(root),
            "markers": list(source_markers),
            "revision": stage["revision"],
        }

    def _execute_activate_component(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        execution.check_cancelled()
        definition = self._definition(execution, task)
        stage = self._stage_result(execution, definition.id)
        staged = Path(stage["staged_path"]).resolve(strict=False)
        execution.context.layout.require_within(
            staged,
            roots=(execution.context.layout.staging,),
        )
        revision = str(stage["revision"] or execution.operation.id)
        safe_revision = (
            "".join(
                character for character in revision if character.isalnum() or character in "._-"
            )[:80]
            or execution.operation.id
        )
        container = component_container(execution.context.layout, definition.id)
        versions = container / "versions"
        destination = versions / safe_revision
        versions.mkdir(parents=True, exist_ok=True)
        execution.context.layout.require_within(
            destination,
            roots=(versions,),
        )
        pointer = component_pointer(execution.context.layout, definition.id)
        activation_journal = staged.parent / "activation.json"
        execution.context.layout.require_within(
            activation_journal,
            roots=(execution.context.layout.staging,),
        )
        if activation_journal.is_file():
            try:
                journal = json.loads(activation_journal.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                raise RuntimeError(f"{definition.label} activation journal is invalid.") from error
            if (
                not isinstance(journal, dict)
                or journal.get("component_id") != definition.id
                or journal.get("destination") != str(destination)
            ):
                raise RuntimeError(
                    f"{definition.label} activation journal does not match the planned slot."
                )
            previous_pointer = journal.get("previous_pointer")
            created_slot = bool(journal.get("created_slot"))
        else:
            try:
                previous_pointer = json.loads(pointer.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                previous_pointer = None
            created_slot = not destination.is_dir()
            _atomic_json(
                activation_journal,
                {
                    "component_id": definition.id,
                    "destination": str(destination),
                    "created_slot": created_slot,
                    "previous_pointer": previous_pointer,
                },
            )
        source_markers = self._source_markers(execution, definition)
        if destination.is_dir():
            if not self._markers_present(
                destination,
                source_markers,
            ):
                raise RuntimeError(
                    f"Existing {definition.label} slot {safe_revision} is incomplete."
                )
            if staged.exists():
                shutil.rmtree(staged)
        else:
            os.replace(staged, destination)
        pointer_payload = {
            "component_id": definition.id,
            "version": safe_revision,
            "path": str(destination),
            "activated_by": execution.operation.id,
        }
        _atomic_json(pointer, pointer_payload)
        ownership = {
            "path": str(container),
            "owner_kind": "component",
            "owner_id": definition.id,
            "evidence": {
                "operation_id": execution.operation.id,
                "revision": safe_revision,
                "markers": list(source_markers),
            },
        }
        return {
            "pointer": str(pointer),
            "active_path": str(destination),
            "revision": safe_revision,
            "created_slot": created_slot,
            "previous_pointer": previous_pointer,
            "ownership": ownership,
        }

    def _rollback_activate_component(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
        result: dict,
    ) -> None:
        definition = self._definition(execution, task)
        if not result:
            journal_path = (
                execution.context.layout.staging
                / execution.operation.id
                / definition.id
                / "activation.json"
            )
            execution.context.layout.require_within(
                journal_path,
                roots=(execution.context.layout.staging,),
            )
            if not journal_path.is_file():
                return
            try:
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    f"{definition.label} activation rollback journal is invalid."
                ) from error
            if (
                not isinstance(journal, dict)
                or journal.get("component_id") != definition.id
                or not isinstance(journal.get("destination"), str)
            ):
                raise RuntimeError(f"{definition.label} activation rollback journal is invalid.")
            result = {
                "active_path": journal.get("destination"),
                "created_slot": bool(journal.get("created_slot")),
                "previous_pointer": journal.get("previous_pointer"),
            }
        removal_guard = nullcontext()
        if definition.service_key and execution.supervisor is not None:
            removal_guard = execution.supervisor.component_slot_removal_guard(definition.id)
        with removal_guard:
            pointer = component_pointer(execution.context.layout, definition.id)
            previous = result.get("previous_pointer")
            if isinstance(previous, dict):
                _atomic_json(pointer, previous)
            else:
                try:
                    pointer.unlink()
                except FileNotFoundError:
                    pass
            if result.get("created_slot"):
                active = Path(str(result.get("active_path") or ""))
                if active.exists():
                    container = component_container(
                        execution.context.layout,
                        definition.id,
                    )
                    execution.context.layout.require_within(
                        active,
                        roots=(container / "versions",),
                    )
                    shutil.rmtree(active)

    def _execute_validate_service(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        definition = self._definition(execution, task)
        if execution.supervisor is None or execution.service_spec_factory is None:
            raise UnsupportedTask(f"{definition.label} service validation is unavailable.")
        desired = execution.plan.desired[definition.id]
        resolved = execution.registry.driver(definition.id).resolve(
            execution.context,
            definition,
            desired,
        )
        spec = execution.service_spec_factory(definition.id, resolved)
        if spec is None:
            raise UnsupportedTask(f"{definition.label} has no managed runtime specification.")
        previous_spec = execution.supervisor.replace_spec(spec)
        try:
            service = execution.supervisor.start(spec.service_id)
        except Exception:
            if previous_spec is not None:
                execution.supervisor.replace_spec(previous_spec)
            else:
                execution.supervisor.unregister(spec.service_id)
            raise
        stop_result = execution.prior_results.get(f"{definition.id}:stop", {})
        keep_running = bool(
            desired.options.get("start_after_install", False)
            or stop_result.get("was_running", False)
            or stop_result.get("desired_running", False)
        )
        if not keep_running:
            execution.supervisor.stop(spec.service_id)
        return {
            "service_id": spec.service_id,
            "health": service.health.model_dump(mode="json") if service.health else None,
            "kept_running": keep_running,
            "previous_spec": (
                previous_spec.model_dump(mode="json") if previous_spec is not None else None
            ),
        }

    def _rollback_validate_service(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
        result: dict,
    ) -> None:
        if execution.supervisor is None or not result.get("service_id"):
            return
        service_id = str(result["service_id"])
        if result.get("kept_running"):
            execution.supervisor.stop(service_id)
        previous = result.get("previous_spec")
        if isinstance(previous, dict):
            execution.supervisor.replace_spec(ManagedProcessSpec.model_validate(previous))
        else:
            execution.supervisor.unregister(service_id)

    def _execute_start_application(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        if execution.supervisor is None:
            return {
                "started": False,
                "error": "The process supervisor is unavailable.",
            }
        preferences: dict[str, str] = {}
        crispasr = execution.plan.desired.get("crispasr")
        if crispasr is not None and crispasr.present:
            engine = str(crispasr.options.get("engine") or "").strip()
            quantization = str(
                crispasr.quantization or crispasr.options.get("quantization") or ""
            ).strip()
            if engine:
                preferences["CRISPASR_DEFAULT_ENGINE"] = engine
            if quantization:
                preferences["CRISPASR_DEFAULT_QUANTIZATION"] = quantization
        try:
            exposure = load_network_configuration(
                execution.context.layout,
                environment=execution.context.environment,
            ).application
            specifications = pandrator_runtime_specs(
                execution.context.layout,
                exposure=exposure,
                preferences=preferences,
            )
            running = {
                service.id
                for service in execution.supervisor.snapshot()
                if service.process is not None
            }
            # A component update activates a new application slot before this
            # task refreshes the launch contracts. ProcessSupervisor correctly
            # refuses to replace a running contract, so quiesce every
            # application-owned service first.
            for service_id in PANDRATOR_SERVICE_STOP_ORDER:
                if service_id in running:
                    execution.supervisor.stop(service_id)
            selected_ids = {specification.service_id for specification in specifications}
            for service_id in PANDRATOR_SERVICE_STOP_ORDER:
                if (
                    service_id not in selected_ids
                    and execution.supervisor.spec(service_id) is not None
                ):
                    execution.supervisor.unregister(service_id)
            for specification in specifications:
                execution.supervisor.replace_spec(specification)
            service = execution.supervisor.start(PANDRATOR_WORKER_SERVICE)
            mcp_error = None
            if PANDRATOR_MCP_SERVICE in selected_ids:
                try:
                    execution.supervisor.start(PANDRATOR_MCP_SERVICE)
                except Exception as error:
                    mcp_error = str(error) or "Pandrator MCP could not be started."
                    execution.context.event_sink.emit(
                        "application.mcp_start_failed",
                        {"error": mcp_error, "action": "install"},
                        component_id="pandrator",
                        operation_id=execution.operation.id,
                        service_id=PANDRATOR_MCP_SERVICE,
                    )
        except Exception as error:
            for service_id in PANDRATOR_SERVICE_STOP_ORDER:
                try:
                    if execution.supervisor.spec(service_id) is not None:
                        execution.supervisor.stop(service_id)
                except Exception:
                    pass
            execution.context.event_sink.emit(
                "application.autostart_failed",
                {"error": str(error)},
                component_id="pandrator",
                operation_id=execution.operation.id,
            )
            return {"started": False, "error": str(error)}
        execution.context.event_sink.emit(
            "application.started",
            {"action": "install"},
            component_id="pandrator",
            operation_id=execution.operation.id,
        )
        return {
            "started": True,
            "service_id": service.id,
            "health": (
                service.health.model_dump(mode="json") if service.health is not None else None
            ),
            "mcp_error": mcp_error,
        }

    def _rollback_start_application(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
        result: dict,
    ) -> None:
        if execution.supervisor is None or not result.get("started"):
            return
        for service_id in PANDRATOR_SERVICE_STOP_ORDER:
            if execution.supervisor.spec(service_id) is not None:
                execution.supervisor.stop(service_id)

    def _execute_stop_service(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        definition = self._definition(execution, task)
        if not definition.service_key or execution.supervisor is None:
            return {
                "service_id": None,
                "was_running": False,
                "desired_running": False,
            }
        snapshots = {service.id: service for service in execution.supervisor.snapshot()}
        previous = snapshots.get(definition.service_key)
        was_running = bool(previous is not None and previous.process is not None)
        desired_running = bool(previous is not None and previous.desired_running)
        if previous is not None:
            execution.supervisor.stop(definition.service_key)
        return {
            "service_id": definition.service_key,
            "was_running": was_running,
            "desired_running": desired_running,
        }

    def _rollback_stop_service(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
        result: dict,
    ) -> None:
        if (
            (result.get("was_running") or result.get("desired_running"))
            and result.get("service_id")
            and execution.supervisor is not None
        ):
            execution.supervisor.start(str(result["service_id"]))

    def _execute_remove_owned_component(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        definition = self._definition(execution, task)
        ownership_records = [
            record
            for record in execution.store.owned_paths()
            if record["owner_id"] == definition.id
            and record["owner_kind"] in {"component", "legacy_component"}
        ]
        owned = [Path(record["path"]) for record in ownership_records]
        container = component_container(execution.context.layout, definition.id)
        if container.exists() and container not in owned:
            owned.append(container)
        if not owned:
            raise RuntimeError(
                f"Refusing to remove {definition.label}: no positive ownership "
                "manifest is available. Import or repair the legacy installation first."
            )
        legacy_roots = [
            Path(record["path"]).expanduser().resolve(strict=False)
            for record in ownership_records
            if record["owner_kind"] == "legacy_component"
        ]
        embedded_data = [
            str(item.source)
            for item in legacy_data_inventory(execution.context.layout).items
            if any(
                item.source.resolve(strict=False) == root
                or execution.context.layout.contains(root, item.source)
                for root in legacy_roots
            )
        ]
        if embedded_data:
            raise ManagerError(
                "legacy_data_reconciliation_required",
                "This legacy component still contains mutable data. Use a "
                "reviewed application migration or whole-product uninstall "
                "so the manager can preserve it before removing the source.",
                {
                    "component_id": definition.id,
                    "paths": embedded_data,
                },
                409,
            )
        # A component-container ownership record supersedes any child records.
        # Moving both would make execution order-dependent and break rollback.
        canonical_owned = sorted(
            {path.expanduser().resolve(strict=False) for path in owned},
            key=lambda path: (len(path.parts), str(path).casefold()),
        )
        owned = [
            candidate
            for index, candidate in enumerate(canonical_owned)
            if not any(
                execution.context.layout.contains(parent, candidate) and parent != candidate
                for parent in canonical_owned[:index]
            )
        ]
        backup_root = execution.context.layout.backups / execution.operation.id / definition.id
        moved: list[dict[str, str]] = []
        for index, source in enumerate(owned):
            destination = backup_root / f"{index}-{source.name}"
            execution.context.layout.require_within(
                destination,
                roots=(execution.context.layout.backups,),
            )
            if destination.exists():
                if source.exists():
                    raise RuntimeError(
                        f"Both the owned path and its operation backup exist: {source}"
                    )
                moved.append({"source": str(source), "backup": str(destination)})
                continue
            if not source.exists():
                continue
            execution.context.layout.require_within(
                source,
                roots=(execution.context.layout.root,),
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            moved.append({"source": str(source), "backup": str(destination)})
        return {"moved": moved, "backup_root": str(backup_root)}

    def _rollback_remove_owned_component(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
        result: dict,
    ) -> None:
        definition = self._definition(execution, task)
        moved = list(result.get("moved") or [])
        if not moved:
            owned = [
                Path(record["path"]).expanduser().resolve(strict=False)
                for record in execution.store.owned_paths()
                if record["owner_id"] == definition.id
                and record["owner_kind"] in {"component", "legacy_component"}
            ]
            container = component_container(
                execution.context.layout,
                definition.id,
            )
            if container not in owned:
                owned.append(container)
            canonical_owned = sorted(
                set(owned),
                key=lambda path: (len(path.parts), str(path).casefold()),
            )
            owned = [
                candidate
                for index, candidate in enumerate(canonical_owned)
                if not any(
                    execution.context.layout.contains(parent, candidate) and parent != candidate
                    for parent in canonical_owned[:index]
                )
            ]
            backup_root = execution.context.layout.backups / execution.operation.id / definition.id
            moved = [
                {
                    "source": str(source),
                    "backup": str(backup_root / f"{index}-{source.name}"),
                }
                for index, source in enumerate(owned)
                if (backup_root / f"{index}-{source.name}").exists()
            ]
        for record in reversed(moved):
            source = Path(record["source"])
            backup = Path(record["backup"])
            if not backup.exists():
                continue
            execution.context.layout.require_within(
                backup,
                roots=(execution.context.layout.backups,),
            )
            execution.context.layout.require_within(
                source,
                roots=(execution.context.layout.root,),
            )
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(backup, source)

    def finalize(
        self,
        execution: OperationTaskContext,
        *,
        succeeded: bool,
    ) -> None:
        operation_staging = execution.context.layout.staging / execution.operation.id
        if operation_staging.exists():
            execution.context.layout.require_within(
                operation_staging,
                roots=(execution.context.layout.staging,),
            )
            shutil.rmtree(operation_staging)
        if succeeded:
            backup = execution.context.layout.backups / execution.operation.id
            if backup.exists():
                execution.context.layout.require_within(
                    backup,
                    roots=(execution.context.layout.backups,),
                )
                shutil.rmtree(backup)
