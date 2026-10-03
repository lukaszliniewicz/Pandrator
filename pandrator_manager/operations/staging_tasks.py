"""Native archive and model staging without per-instance state."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from ..artifacts.download import ArtifactDownloader, ArtifactSpec
from ..artifacts.extract import SafeExtractor
from ..components.audiocpp import (
    AUDIO_CPP_MODEL_REVISION,
    AUDIO_CPP_VERSION,
    AudioCppModelPackage,
    model_package,
    server_config,
)
from ..components.audiocpp_reuse import reuse_verified_package
from ..components.crispasr import CRISPASR_VERSION
from ..components.slots import active_component_path
from ..context import WorkspaceLayout
from ..models import TaskSpec
from ..processes.runner import CommandRunner, CommandSpec
from ..runtime_specs import runtime_python
from ..tls import select_ca_bundle
from .contracts import OperationTaskContext, UnsupportedTask
from .source_tasks import ComponentSourceTasks
from .task_files import _atomic_json


class ComponentStagingTasks(ComponentSourceTasks):
    """Component archive and model tasks without per-instance state."""

    @staticmethod
    def _runtime_python(layout: WorkspaceLayout) -> Path:
        return runtime_python(layout)

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
        # Persisted plans from before the explicit flag retain it in resolved options.
        resolved = task.inputs.get("resolved")
        options = resolved.get("options") if isinstance(resolved, dict) else None
        offline = bool(task.inputs.get("offline")) or (
            isinstance(options, dict) and bool(options.get("offline"))
        )
        selected = ArtifactDownloader(
            cancellation=execution.cancellation,
            environment=execution.context.environment,
        ).download(specification, cache_path, offline=offline)
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
        python_candidate = self._runtime_python(execution.context.layout)
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
