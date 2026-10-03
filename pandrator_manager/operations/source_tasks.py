"""Stateless component source staging and shared task helpers."""

from __future__ import annotations

import shutil
from pathlib import Path

from dulwich import porcelain
from dulwich.repo import Repo

from ..components.audiocpp import source_markers_for
from ..components.runtime_bootstrap import generated_runtime_files
from ..errors import ManagerError
from ..models import TaskSpec
from ..tls import CABundleSelection, dulwich_config_with_ca
from .contracts import OperationTaskContext, UnsupportedTask
from .source_errors import _source_acquisition_error
from .task_files import _atomic_text


class ComponentSourceTasks:
    """Component source task methods without per-instance state."""

    @staticmethod
    def _source_acquisition_error(
        *,
        error: Exception,
        label: str,
        repo_url: str,
        ca_bundle: CABundleSelection,
    ) -> ManagerError:
        return _source_acquisition_error(
            error=error,
            label=label,
            repo_url=repo_url,
            ca_bundle=ca_bundle,
        )

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
            raise self._source_acquisition_error(
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

    @staticmethod
    def _source_markers(execution: OperationTaskContext, definition) -> tuple[str, ...]:
        if definition.id == "audio_cpp":
            return source_markers_for(execution.context.system)
        return definition.source_markers

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
