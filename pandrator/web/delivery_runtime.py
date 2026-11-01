"""Run-local delivery authority, source pins and private output staging."""

from __future__ import annotations

import logging
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .access.artifact_visibility import artifact_roots_clause, resource_visibility_clause
from .access.assembly_execution_policy import AssemblyExecutionProof, require_assembly_execution
from .access.contracts import AccessDenied
from .access.delivery_execution_policy import DeliveryExecutionProof, require_delivery_execution
from .access.project_visibility import project_ids_for_user, workspace_authority
from .accounts.credential_resources import credential_project_clause, credential_workspace_clause
from .accounts.execution_actor import UserActor, current_execution_actor
from .accounts.models import Workspace
from .models import Artifact, SessionRecord, new_id
from .projects.models import Project
from .settings_policy import RevisionConflict
from .workflow_output_context import OutputWorkflowContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _InputPin:
    artifact_id: str
    workspace_id: str
    project_id: str | None
    session_id: str | None
    content_hash: str | None
    relative_path: str | None = None


class DeliveryRuntime:
    """Scoped queued workers use this context; trusted direct calls keep theirs."""

    def __init__(
        self,
        context: OutputWorkflowContext,
        proof: DeliveryExecutionProof | AssemblyExecutionProof,
        cancel_event: threading.Event,
        *,
        assembly_payload: Mapping[str, Any] | None = None,
    ):
        self.base = context
        self.proof = proof
        self.cancel_event = cancel_event
        self._assembly_payload = (
            {
                key: assembly_payload[key]
                for key in ("output_assembly_id", "_job_id", "_lease_generation")
                if key in assembly_payload
            }
            if assembly_payload is not None
            else None
        )
        self._inputs: dict[str, _InputPin] = {}
        self._lock = threading.RLock()
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._stage: Path | None = None
        self._session_root: Path | None = None

    def _pins(self) -> dict[str, _InputPin]:
        with self._lock:
            return dict(self._inputs)

    def prove(self, session: Session, *, publication: bool = False) -> None:
        with session.no_autoflush:
            self._prove(session, publication=publication)

    def _prove(self, session: Session, *, publication: bool) -> None:
        if isinstance(self.proof, AssemblyExecutionProof):
            if self._assembly_payload is None:
                raise AccessDenied()
            require_assembly_execution(session, self._assembly_payload, expected=self.proof)
        else:
            require_delivery_execution(session, self.proof)
        pins = self._pins()
        actor = current_execution_actor.get()
        if publication:
            actual = {}
            ids = list(pins)
            for offset in range(0, len(ids), 500):
                query = select(
                    Artifact.id,
                    Artifact.workspace_id,
                    Artifact.project_id,
                    Artifact.session_id,
                    Artifact.content_hash,
                    Artifact.relative_path,
                ).where(Artifact.id.in_(ids[offset : offset + 500]), Artifact.state != "deleted")
                query = query.where(
                    resource_visibility_clause(Artifact, actor)
                    if type(actor) is UserActor
                    else artifact_roots_clause(Artifact)
                )
                for row in session.execute(query):
                    actual[row.id] = _InputPin(*row)
            for identifier, expected in pins.items():
                current = actual.get(identifier)
                if (
                    current is None
                    or replace(current, relative_path=expected.relative_path) != expected
                ):
                    raise RevisionConflict(
                        "A selected export input changed or is no longer available."
                    )
                if (
                    expected.relative_path is not None
                    and current.relative_path != expected.relative_path
                ):
                    raise RevisionConflict("A selected export input path changed.")
            return
        if type(actor) is not UserActor:
            return
        # Root authority already covers its project. Recheck only additional
        # source permission roots, in batches rather than per-media-row loops.
        projects = {
            pin.project_id
            for pin in pins.values()
            if pin.project_id is not None and pin.project_id != self.proof.project_id
        }
        if projects:
            allowed = set(
                session.scalars(
                    select(Project.id).where(
                        Project.id.in_(projects),
                        Project.id.in_(project_ids_for_user(actor.user_id)),
                        credential_project_clause(actor),
                    )
                ).all()
            )
            if allowed != projects:
                raise AccessDenied()
        workspaces = {pin.workspace_id for pin in pins.values() if pin.project_id is None}
        if workspaces:
            allowed = set(
                session.scalars(
                    select(Workspace.id).where(
                        Workspace.id.in_(workspaces),
                        Workspace.state == "active",
                        workspace_authority(actor.user_id),
                        credential_workspace_clause(actor),
                    )
                ).all()
            )
            if allowed != workspaces:
                raise AccessDenied()

    def before_io(self) -> None:
        if self.cancel_event.is_set():
            raise InterruptedError("Export was canceled.")
        with self.base.database.snapshot_session() as session:
            self.prove(session)

    def authorize_input(self, session: Session, artifact_id: str) -> None:
        with session.no_autoflush:
            self._authorize_input(session, artifact_id)

    def _authorize_input(self, session: Session, artifact_id: str) -> None:
        if type(artifact_id) is not str or not artifact_id or artifact_id != artifact_id.strip():
            raise AccessDenied()
        actor = current_execution_actor.get()
        query = select(
            Artifact.id,
            Artifact.workspace_id,
            Artifact.project_id,
            Artifact.session_id,
            Artifact.content_hash,
        ).where(Artifact.id == artifact_id, Artifact.state != "deleted")
        query = query.where(
            resource_visibility_clause(Artifact, actor)
            if type(actor) is UserActor
            else artifact_roots_clause(Artifact)
        )
        row = session.execute(query).one_or_none()
        if row is None:
            raise AccessDenied()
        current = _InputPin(*row)
        with self._lock:
            previous = self._inputs.get(artifact_id)
            if previous is not None and replace(previous, relative_path=None) != current:
                raise RevisionConflict("A selected export input changed.")
            self._inputs.setdefault(artifact_id, current)

    def resolve_input(self, artifact_id: str) -> tuple[Artifact, Path]:
        with self.base.database.snapshot_session() as session:
            self.prove(session)
            self.authorize_input(session, artifact_id)
            artifact = session.get_one(Artifact, artifact_id)
            with self._lock:
                pin = self._inputs[artifact_id]
                if pin.relative_path is not None and pin.relative_path != artifact.relative_path:
                    raise RevisionConflict("A selected export input path changed.")
                self._inputs[artifact_id] = replace(pin, relative_path=artifact.relative_path)
            session.expunge(artifact)
        self.before_io()
        path = self.base.paths.managed_path(artifact.relative_path)
        if not path.is_file():
            raise FileNotFoundError(path)
        return artifact, path

    def destination(self, path: Path) -> Path:
        assert self._stage is not None and self._session_root is not None
        try:
            relative = path.resolve().relative_to(self._stage.resolve())
        except ValueError:
            raise AccessDenied() from None
        return self._session_root / relative

    def publish(self, rendered: Path, **kwargs: Any) -> Artifact:
        from .delivery_publication import publish_delivery_artifact

        if kwargs.get("session_id") != self.proof.session_id:
            raise AccessDenied()
        requested = kwargs.pop("requested_destination", None) or rendered
        destination = self.destination(requested)
        return publish_delivery_artifact(
            self.base.database,
            self.base.paths,
            self.base.artifacts,
            rendered,
            destination,
            **kwargs,
            before_io=self.before_io,
            authorization_guard=lambda session: self.prove(session, publication=True),
            cancel_event=self.cancel_event,
        )

    def session_dir(self, session_id: str) -> Path:
        if session_id != self.proof.session_id:
            raise AccessDenied()
        self.before_io()
        assert self._stage is not None
        return self._stage

    def operation_dir(self, session_id: str, stage_key: str) -> Path:
        path = self.session_dir(session_id) / "stage-runs" / f"{stage_key}-{new_id()}"
        self.destination(path)
        path.mkdir(parents=True, exist_ok=False)
        return path

    def session_record(self, session_id: str) -> SessionRecord:
        if session_id != self.proof.session_id:
            raise AccessDenied()
        with self.base.database.snapshot_session() as session:
            self.prove(session)
            record = session.get_one(SessionRecord, session_id)
            session.expunge(record)
        return record

    def render_sound_mix(self, session_id: str, base_artifact_id: str, **kwargs: Any) -> Artifact:
        if session_id != self.proof.session_id or self.base._render_sound_mix is None:
            raise AccessDenied()
        self.before_io()
        return self.base._render_sound_mix(
            session_id,
            base_artifact_id,
            **kwargs,
            before_io=self.before_io,
            authorization_guard=lambda session: self.prove(session, publication=True),
            authorize_input=self.authorize_input,
        )

    def input_context(self) -> OutputWorkflowContext:
        """Also fence post-assembly sounds without creating export staging."""
        if self.proof.job_id is None or current_execution_actor.get() is None:
            return self.base
        return replace(
            self.base,
            _resolve_input=self.resolve_input,
            _session_record=self.session_record,
            _before_io=self.before_io,
            _authorization_guard=self.prove,
            _authorize_input=self.authorize_input,
            _render_sound_mix=(
                self.render_sound_mix if self.base._render_sound_mix is not None else None
            ),
        )

    def __enter__(self) -> OutputWorkflowContext:
        scoped = self.input_context()
        if scoped is self.base:
            self.before_io()
            return self.base
        record = self.session_record(self.proof.session_id)
        self._session_root = self.base.paths.sessions / record.storage_key
        self._session_root.mkdir(parents=True, exist_ok=True)
        self._temporary = tempfile.TemporaryDirectory(prefix=".delivery-", dir=self._session_root)
        self._stage = Path(self._temporary.name)
        return replace(
            scoped,
            _session_dir=self.session_dir,
            _operation_dir=self.operation_dir,
            _publish_output=self.publish,
            _output_destination=self.destination,
        )

    def __exit__(self, *exc: object) -> None:
        if self._temporary is not None:
            try:
                self._temporary.cleanup()
            except OSError:
                if not exc or exc[0] is None:
                    raise
                logger.warning(
                    "Could not remove private delivery staging after failure.", exc_info=True
                )
