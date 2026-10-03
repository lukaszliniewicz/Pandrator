"""Component slot verification, activation, and rollback tasks."""

from __future__ import annotations

import json
import os
import shutil
from contextlib import nullcontext
from pathlib import Path

from ..components.slots import component_container, component_pointer
from ..models import TaskSpec
from .contracts import OperationTaskContext
from .source_tasks import ComponentSourceTasks
from .task_files import _atomic_json


class ComponentActivationTasks(ComponentSourceTasks):
    """Stateless tasks for owned component slots and activation journals."""

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
        # Anchor both roots before trusting containment relative to versions.
        execution.context.layout.require_within(
            container,
            roots=(execution.context.layout.root,),
        )
        execution.context.layout.require_within(versions, roots=(container,))
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
        if execution.supervisor is not None:
            removal_guard = execution.supervisor.component_slot_removal_guard(definition.id)
        elif any(
            service.component_id == definition.id and service.process is not None
            for service in execution.store.list_services()
        ):
            raise RuntimeError(
                f"Cannot roll back {definition.label} activation without a process supervisor "
                "while a managed process is recorded."
            )
        with removal_guard:
            container = component_container(execution.context.layout, definition.id)
            # Refuse redirected roots before restoring pointers or deleting slots.
            execution.context.layout.require_within(
                container,
                roots=(execution.context.layout.root,),
            )
            execution.context.layout.require_within(
                container / "versions", roots=(container,),
            )
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
                    execution.context.layout.require_within(
                        active,
                        roots=(container / "versions",),
                    )
                    shutil.rmtree(active)
