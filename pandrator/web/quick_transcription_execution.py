"""One captured private transcription operation and its guarded local effects."""

from __future__ import annotations

from copy import deepcopy
from urllib.parse import urljoin

import requests
from sqlalchemy import select

from pandrator.logic.cancellable_process import ProcessCancelled
from pandrator.logic.dubbing import cloud_stt
from pandrator.runtime import DataPaths

from .access.private_transcription_execution_policy import (
    PrivateTranscriptionInput,
    _private_lease,
    require_private_transcription_execution,
)
from .accounts.execution_actor import current_execution_actor
from .accounts.job_authorization import require_current_job_actor_lease
from .accounts.private_work_models import QuickTranscriptionOwner
from .artifacts import sha256_file
from .credential_identity import credential_namespace
from .credentials import hydrate_stt_settings
from .database import Database
from .job_execution import current_job_lease
from .models import Job, QuickTranscription
from .settings_identity import workspace_setting_in_workspace

_CONNECTION_CHANGED = "The selected transcription service is no longer available."
_SOURCE_CHANGED = "The temporary transcription source changed or is unavailable."
_CONNECTION_ALIASES = frozenset(
    {
        "stt_api_base",
        "stt_api_base_url",
        "stt_base_url",
        "cloud_stt_base_url",
        "api_base",
        "base_url",
        "stt_api_key",
        "cloud_stt_api_key",
        "api_key",
        "api_key_env",
        "secret_ref",
    }
)


class PrivateTranscriptionExecution:
    def __init__(
        self,
        database: Database,
        paths: DataPaths,
        payload,
        cancel_event,
        header: PrivateTranscriptionInput,
        settings,
    ):
        self.database = database
        self.paths = paths
        self.payload = dict(payload)
        self.cancel_event = cancel_event
        self.header = header
        self.settings = deepcopy(settings)
        self.root = paths.root / "quick-transcriptions"
        self.directory = self.root / header.transcription_id
        self.source_path = self.directory / f"source{header.source_suffix}"
        self._failure: Exception | None = None
        self._cloud_seed = None
        self._cloud_identity = None

    def _reject(self, error: Exception) -> None:
        if self._failure is None:
            self._failure = error

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise self._failure
        if self.cancel_event.is_set():
            raise ProcessCancelled("Transcription was canceled.")

    def check_in_session(self, unit, *, allow_finished: bool = False) -> None:
        self.raise_if_failed()
        try:
            require_private_transcription_execution(
                unit, self.payload, expected=self.header, allow_finished=allow_finished
            )
        except Exception as error:
            self._reject(error)
            raise

    def check(self) -> None:
        with self.database.snapshot_session() as unit:
            self.check_in_session(unit)

    def before_io(self) -> None:
        self.raise_if_failed()
        self.check()

    def check_source(self) -> None:
        self.before_io()
        try:
            if (
                not self.header.source_suffix.startswith(".")
                or any(value in self.header.source_suffix for value in ("/", "\\"))
                or self.directory.is_symlink()
                or self.source_path.is_symlink()
                or self.paths.managed_path(self.paths.relative_managed_path(self.source_path))
                != self.source_path
                or not self.source_path.is_file()
                or self.source_path.stat().st_size != self.header.size_bytes
            ):
                raise ValueError(_SOURCE_CHANGED)
        except (OSError, ValueError):
            raise ValueError(_SOURCE_CHANGED) from None
        self.before_io()
        try:
            digest = sha256_file(self.source_path)
        except OSError:
            raise ValueError(_SOURCE_CHANGED) from None
        if digest != self.header.sha256:
            raise ValueError(_SOURCE_CHANGED)
        self.before_io()

    def _cloud_runtime(self, seed):
        self.before_io()
        engine = (
            str(seed.get("stt_engine") or seed.get("stt_backend") or "")
            .strip()
            .lower()
            .replace("-", "_")
        )
        with self.database.snapshot_session() as unit:
            self.check_in_session(unit)
            namespace = credential_namespace(unit, self.header.workspace_id)
            record = workspace_setting_in_workspace(unit, self.header.workspace_id, "services.stt")
            value = (
                deepcopy(record.value_json)
                if record is not None and isinstance(record.value_json, dict)
                else {}
            )
        selected = next(
            (
                deepcopy(item)
                for item in value.get("provider_configs", [])
                if isinstance(item, dict)
                and str(item.get("id") or "").strip().lower().replace("-", "_") == engine
            ),
            None,
        )
        if selected is not None and selected.get("enabled", True) is not True:
            raise ValueError(_CONNECTION_CHANGED)
        if selected is None and not namespace.legacy_host_access:
            raise ValueError(_CONNECTION_CHANGED)
        runtime = deepcopy(seed)
        if selected is not None:
            defaults = selected.get("settings")
            if not isinstance(defaults, dict):
                defaults = {}
            runtime = {**deepcopy(defaults), **runtime}
            runtime["provider_configs"] = [selected]
        if not namespace.legacy_host_access:
            for key in _CONNECTION_ALIASES:
                runtime.pop(key, None)
            assert selected is not None
            selected.pop("api_key", None)
            selected.pop("api_key_env", None)
        runtime = hydrate_stt_settings(
            self.database, self.paths, runtime, workspace_id=self.header.workspace_id
        )
        self.before_io()
        profile = cloud_stt._profile_for(runtime)
        if not namespace.legacy_host_access and not str(profile.get("api_key") or "").strip():
            raise ValueError(_CONNECTION_CHANGED)
        return runtime

    @staticmethod
    def _identity(runtime):
        profile = cloud_stt._profile_for(runtime)
        url = urljoin(f"{cloud_stt._api_base(runtime, profile)}/", str(profile["path"]).lstrip("/"))
        timeout = cloud_stt._positive_setting(
            runtime,
            ("stt_request_timeout_seconds", "cloud_stt_timeout_seconds"),
            cloud_stt.DEFAULT_REQUEST_TIMEOUT_SECONDS,
            label="stt_request_timeout_seconds",
        )
        return deepcopy(
            (
                profile["id"],
                url,
                cloud_stt._api_key(runtime, profile),
                cloud_stt.build_azure_definition(runtime, profile),
                timeout,
                cloud_stt._chunk_config(runtime),
            )
        )

    def runtime_settings(self, settings):
        self.before_io()
        runtime = deepcopy(settings)
        engine = runtime.get("stt_engine") or runtime.get("stt_backend") or ""
        if not cloud_stt.is_cloud_stt_engine(engine):
            return runtime
        try:
            runtime = self._cloud_runtime(runtime)
            identity = self._identity(runtime)
            if self._cloud_identity is not None and identity != self._cloud_identity:
                raise ValueError(_CONNECTION_CHANGED)
            self._cloud_seed = deepcopy(settings)
            self._cloud_identity = identity
        except ValueError as error:
            unavailable = ValueError(_CONNECTION_CHANGED)
            self._reject(unavailable)
            raise unavailable from error
        return runtime

    def cloud_request(self, *args, **kwargs):
        self.before_io()
        try:
            if self._cloud_seed is None or self._cloud_identity is None:
                raise ValueError(_CONNECTION_CHANGED)
            fresh = self._cloud_runtime(self._cloud_seed)
            if self._identity(fresh) != self._cloud_identity:
                raise ValueError(_CONNECTION_CHANGED)
        except (cloud_stt.CloudSTTConfigurationError, ValueError) as error:
            unavailable = ValueError(_CONNECTION_CHANGED)
            self._reject(unavailable)
            raise unavailable from error
        self.before_io()
        return requests.post(*args, **kwargs)

    def _structural(self, unit) -> bool:
        if (
            current_execution_actor.get() != self.header.actor
            or _private_lease(current_job_lease.get()) != self.header.lease
        ):
            return False
        require_current_job_actor_lease(unit, self.header.actor)
        job_id = unit.scalar(
            select(Job.id).where(
                Job.id == self.header.job_id,
                Job.kind == "transcription.quick",
                Job.workspace_id == self.header.workspace_id,
                Job.session_id.is_(None),
                Job.project_id.is_(None),
                Job.workflow_run_id.is_(None),
            )
        )
        if job_id is None:
            return False
        selector = unit.scalar(
            select(Job.payload_json["transcription_id"].as_string()).where(Job.id == job_id)
        )
        if selector != self.header.transcription_id:
            return False
        row = unit.execute(
            select(
                QuickTranscription.source_suffix,
                QuickTranscription.format,
                QuickTranscription.size_bytes,
                QuickTranscription.sha256,
                QuickTranscription.uploaded_bytes,
                QuickTranscription.next_chunk_index,
            )
            .join(
                QuickTranscriptionOwner,
                QuickTranscriptionOwner.transcription_id == QuickTranscription.id,
            )
            .where(
                QuickTranscription.id == self.header.transcription_id,
                QuickTranscription.job_id == job_id,
                QuickTranscriptionOwner.user_id == self.header.owner_user_id,
                QuickTranscriptionOwner.workspace_id == self.header.workspace_id,
            )
        ).one_or_none()
        return row is not None and tuple(row) == (
            self.header.source_suffix,
            self.header.format,
            self.header.size_bytes,
            self.header.sha256,
            self.header.uploaded_bytes,
            self.header.next_chunk_index,
        )

    def may_cleanup_inputs(self, unit) -> bool:
        try:
            return self._structural(unit)
        except Exception:
            return False

    def result_is_committed(self, unit) -> bool:
        return (
            unit.scalar(
                select(QuickTranscription.id)
                .join(
                    QuickTranscriptionOwner,
                    QuickTranscriptionOwner.transcription_id == QuickTranscription.id,
                )
                .join(Job, Job.id == QuickTranscription.job_id)
                .where(
                    QuickTranscription.id == self.header.transcription_id,
                    QuickTranscription.state == "finished",
                    QuickTranscription.job_id == self.header.job_id,
                    QuickTranscriptionOwner.user_id == self.header.owner_user_id,
                    QuickTranscriptionOwner.workspace_id == self.header.workspace_id,
                    Job.kind == "transcription.quick",
                    Job.workspace_id == self.header.workspace_id,
                    Job.project_id.is_(None),
                    Job.session_id.is_(None),
                    Job.workflow_run_id.is_(None),
                )
            )
            is not None
        )


def prepare_private_transcription_execution(database, paths, payload, cancel_event):
    with database.snapshot_session() as unit:
        header = require_private_transcription_execution(unit, payload)
        if header is None:
            return None
        settings = deepcopy(
            unit.scalar(
                select(QuickTranscription.settings_json).where(
                    QuickTranscription.id == header.transcription_id
                )
            )
        )
    return PrivateTranscriptionExecution(database, paths, payload, cancel_event, header, settings)
