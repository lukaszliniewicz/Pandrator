"""Native training-worker qualification without external processes or GPU work."""

from __future__ import annotations

import copy
import json
import threading
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest
from sqlalchemy import event, select

from pandrator.logic import xtts_trainer_handler as trainer
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.jobs import JobQueue
from pandrator.web.models import Artifact, ArtifactEdge, Job, TrainingRun, utcnow
from pandrator.web.schemas import TrainingCreateRequest
from pandrator.web.training_lifecycle import TrainingService
from pandrator.web.workflow_handlers import WorkflowHandlers
from tests.web_test_support import prepare_web_test_data_root


class NativeTraining:
    def __init__(self, root: Path):
        self.paths = prepare_web_test_data_root(root / "managed")
        self.database = Database(self.paths.database)
        self.queue = JobQueue(self.database)
        self.artifacts = ArtifactService(self.database, self.paths)
        self.service = TrainingService(self.database, self.paths, self.queue)
        self.handlers = WorkflowHandlers(self.database, self.paths, jobs=self.queue)
        self.audio = self.paths.uploads / "source.wav"
        self.text = self.paths.uploads / "source.txt"
        self.audio.write_bytes(b"original training audio")
        self.text.write_bytes(b"Original training transcript.\n")
        self.source = self.artifacts.register(self.audio, kind="audio", role="upload")
        self.transcript = self.artifacts.register(self.text, kind="text", role="upload")
        self.source_bytes = (self.audio.read_bytes(), self.text.read_bytes())
        self.trainer_root = root / "trainer"
        self.model_root = self.paths.models / "xtts"
        self.cancel_event = threading.Event()
        self.trainer_calls = 0
        self.bundle: Path | None = None
        self.bundle_bytes: dict[str, bytes] = {}

    def seed(self, model_name="narrator", *, durable=True, raw=False):
        inputs = {
            "model_name": model_name,
            "source_artifact_id": self.source.id,
            "source_text_artifact_id": self.transcript.id,
            "settings": {"epochs": 1},
        }
        if durable and not raw:
            run, _job = self.service.start(TrainingCreateRequest(**inputs))
            self.training_id = run.id
        else:
            with self.database.session() as session:
                run = TrainingRun(
                    model_name=model_name,
                    source_artifact_id=self.source.id,
                    source_text_artifact_id=self.transcript.id,
                    settings_json=inputs["settings"],
                )
                session.add(run)
                session.flush()
                self.training_id = run.id
                if durable:
                    job = self.queue.enqueue_in_session(
                        session,
                        "training.xtts",
                        {**inputs, "training_id": run.id},
                        resource_keys=["training:xtts", "gpu:default"],
                    )
                    run.job_id = job.id
        self.payload = {**inputs, "training_id": self.training_id}
        if durable:
            job = self.queue.claim("qualification-worker", lease_seconds=300)
            assert job is not None
            self.job_id = job.id
            self.generation = job.lease_generation
            self.payload.update(_job_id=job.id, _lease_generation=job.lease_generation)
        return self.payload

    def make_bundle(self, model_name=None):
        name = model_name or self.payload["model_name"]
        self.bundle = self.trainer_root / name / "models" / "xtts-final"
        self.bundle.mkdir(parents=True)
        self.bundle_bytes = {
            filename: f"tiny original {filename}\n".encode()
            for filename in trainer.XTTS_MODEL_BUNDLE_FILENAMES
        }
        for filename, contents in self.bundle_bytes.items():
            (self.bundle / filename).write_bytes(contents)

    def fake_trainer(self, before_copy=None, error=None):
        def start(settings, **kwargs):
            self.trainer_calls += 1
            if before_copy:
                before_copy()
            if error:
                raise error
            paths = {
                "trainer_dir": str(self.trainer_root),
                "xtts_models_dir": str(self.model_root),
            }
            # Baseline code has no callback keyword. Follow the worker's actual
            # interface so this control exercises its real four-file copy path.
            copy_kwargs = {}
            if "publish_callback" in kwargs:
                copy_kwargs["publish_callback"] = kwargs["publish_callback"]
            if kwargs.get("model_root") is not None:
                assert Path(kwargs["model_root"]).resolve() == self.model_root.resolve()
            return trainer._copy_trained_model(settings["model_name"], paths, **copy_kwargs)

        return start

    def invoke(self, fake, payload=None):
        with mock.patch.object(trainer, "start_training", side_effect=fake):
            return self.handlers.train_xtts(
                self.payload if payload is None else payload,
                lambda *_args: None,
                self.cancel_event,
            )

    def invoke_allow_refusal(self, fake, payload=None):
        try:
            return self.invoke(fake, payload)
        except (InterruptedError, RuntimeError, ValueError):
            return None

    def run_snapshot(self):
        with self.database.snapshot_session() as session:
            run = session.get(TrainingRun, self.training_id)
            return copy.deepcopy(
                {column.name: getattr(run, column.name) for column in TrainingRun.__table__.columns}
            )

    def assert_sources_retained(self):
        assert (self.audio.read_bytes(), self.text.read_bytes()) == self.source_bytes
        if self.bundle is not None:
            assert {
                filename: (self.bundle / filename).read_bytes() for filename in self.bundle_bytes
            } == self.bundle_bytes

    def assert_unpublished(self):
        with self.database.snapshot_session() as session:
            assert (
                list(session.scalars(select(Artifact).where(Artifact.role == "xtts_model"))) == []
            )
            assert list(session.scalars(select(ArtifactEdge))) == []
            assert session.get(TrainingRun, self.training_id).output_artifact_id is None
        if self.model_root.exists():
            assert not [
                file
                for file in self.model_root.rglob("*")
                if file.is_file() and ".downloads" not in file.relative_to(self.model_root).parts
            ]
        self.assert_sources_retained()

    def advance_owner(self):
        with self.database.session() as session:
            job = session.get(Job, self.job_id)
            job.lease_generation += 1
            job.lease_owner = "newer-worker"
            job.lease_expires_at = utcnow() + timedelta(minutes=5)
            run = session.get(TrainingRun, self.training_id)
            run.status = "running"
            run.error_message = "newer owner diagnostic"
            run.updated_at = utcnow()
        self.newer_snapshot = self.run_snapshot()


@pytest.fixture
def native(tmp_path):
    harness = NativeTraining(tmp_path)
    try:
        yield harness
    finally:
        harness.database.dispose()


@pytest.mark.parametrize("refusal", ["stale", "expired", "foreign", "partial", "omitted"])
def test_worker_rejects_invalid_claim_before_training(native, refusal):
    native.seed()
    native.make_bundle()
    payload = dict(native.payload)
    if refusal == "stale":
        payload["_lease_generation"] -= 1
    elif refusal == "expired":
        with native.database.session() as session:
            session.get(Job, native.job_id).lease_expires_at = utcnow() - timedelta(seconds=1)
    elif refusal == "foreign":
        with native.database.session() as session:
            job = session.get(Job, native.job_id)
            job.payload_json = {**job.payload_json, "training_id": "different-training"}
    elif refusal == "partial":
        payload.pop("_lease_generation")
    elif refusal == "omitted":
        payload.pop("_lease_generation")
        payload.pop("_job_id")
    before = native.run_snapshot()
    with pytest.raises((InterruptedError, RuntimeError, ValueError)):
        native.invoke(native.fake_trainer(), payload)
    assert native.trainer_calls == 0
    assert native.run_snapshot() == before
    native.assert_unpublished()


def test_legacy_direct_orphan_training_remains_allowed(native):
    native.seed(durable=False)
    native.make_bundle()
    result = native.invoke(native.fake_trainer())
    assert result["training_id"] == native.training_id
    assert native.run_snapshot()["status"] == "succeeded"
    assert native.trainer_calls == 1
    native.assert_sources_retained()


def test_stale_owner_inside_trainer_cannot_publish_or_overwrite_newer_state(native):
    native.seed()
    native.make_bundle()
    native.invoke_allow_refusal(native.fake_trainer(before_copy=native.advance_owner))
    assert native.run_snapshot() == native.newer_snapshot
    native.assert_unpublished()


def test_native_cancel_request_inside_trainer_blocks_publication(native):
    native.seed()
    native.make_bundle()
    native.invoke_allow_refusal(
        native.fake_trainer(before_copy=lambda: native.queue.request_cancel(native.job_id))
    )
    assert not native.cancel_event.is_set()
    assert native.run_snapshot()["status"] in {"cancel_requested", "canceled"}
    native.assert_unpublished()


@pytest.mark.parametrize(
    "model_name", ["../escape", "__ABSOLUTE__", "__pycache__/narrator", r"custom\narrator"]
)
def test_worker_rejects_unsafe_name_before_external_trainer(native, model_name):
    if model_name == "__ABSOLUTE__":
        model_name = str(native.paths.root.parent / "escaped-model")
    native.seed(model_name, raw=True)
    with pytest.raises((InterruptedError, RuntimeError, ValueError)):
        native.invoke(native.fake_trainer())
    assert native.trainer_calls == 0
    native.assert_unpublished()
    assert not (native.paths.root.parent / "escaped-model").exists()
    assert not (native.paths.models / "escape").exists()


def test_nested_model_success_has_exact_bundle_manifest_and_source_lineage(native):
    native.seed("custom/narrator")
    native.make_bundle()
    result = native.invoke(native.fake_trainer())
    target = native.model_root / "custom" / "narrator"
    assert {file.name for file in target.iterdir()} == {
        *trainer.XTTS_MODEL_BUNDLE_FILENAMES,
        "pandrator-training.json",
    }
    for filename, contents in native.bundle_bytes.items():
        assert (target / filename).read_bytes() == contents
    manifest = json.loads((target / "pandrator-training.json").read_text())
    assert manifest["model_name"] == "custom/narrator"
    with native.database.snapshot_session() as session:
        artifacts = list(session.scalars(select(Artifact).where(Artifact.role == "xtts_model")))
        assert len(artifacts) == 1
        run = session.get(TrainingRun, native.training_id)
        assert run.status == "succeeded"
        assert run.output_artifact_id == artifacts[0].id == result["artifact_id"]
        assert artifacts[0].relative_path == native.paths.relative_managed_path(
            target / "pandrator-training.json"
        )
        assert set(
            session.scalars(
                select(ArtifactEdge.parent_artifact_id).where(
                    ArtifactEdge.child_artifact_id == artifacts[0].id
                )
            )
        ) == {native.source.id, native.transcript.id}
    native.assert_sources_retained()


def test_registration_failure_after_flush_rolls_back_publication(native):
    native.seed()
    native.make_bundle()
    register = native.handlers.artifacts.register_in_session
    injections = []

    def register_then_refuse(session, *args, **kwargs):
        artifact = register(session, *args, **kwargs)
        session.flush()
        injections.append(artifact.id)
        raise RuntimeError("native registration refused after flush")

    with mock.patch.object(
        native.handlers.artifacts, "register_in_session", side_effect=register_then_refuse
    ):
        with pytest.raises(RuntimeError, match="registration refused"):
            native.invoke(native.fake_trainer())
    assert len(injections) == 1
    assert native.run_snapshot()["status"] == "failed"
    native.assert_unpublished()


def test_commit_refusal_of_success_rolls_back_publication_and_allows_failure_write(native):
    native.seed()
    native.make_bundle()
    refusals = []

    def refuse_success(connection):
        status = connection.scalar(
            select(TrainingRun.status).where(TrainingRun.id == native.training_id)
        )
        if status == "succeeded":
            refusals.append(status)
            raise RuntimeError("native success commit refused")

    event.listen(native.database.engine, "commit", refuse_success)
    try:
        with pytest.raises(RuntimeError, match="success commit refused"):
            native.invoke(native.fake_trainer())
    finally:
        event.remove(native.database.engine, "commit", refuse_success)
    assert len(refusals) == 1
    assert native.run_snapshot()["status"] == "failed"
    native.assert_unpublished()


@pytest.mark.parametrize("raises", [True, False])
def test_trainer_failure_after_lease_change_does_not_overwrite_newer_owner(native, raises):
    native.seed()
    native.make_bundle()
    if raises:
        fake = native.fake_trainer(
            before_copy=native.advance_owner, error=RuntimeError("old trainer error")
        )
    else:

        def fake(_settings, **_kwargs):
            native.trainer_calls += 1
            native.advance_owner()
            return False, "old trainer failure"

    native.invoke_allow_refusal(fake)
    assert native.run_snapshot() == native.newer_snapshot
    native.assert_unpublished()


def test_terminal_training_cannot_regress_or_retrain(native):
    native.seed()
    native.make_bundle()
    with native.database.session() as session:
        run = session.get(TrainingRun, native.training_id)
        run.status = "succeeded"
        run.output_artifact_id = native.source.id
        run.error_message = "terminal diagnostic"
    before = native.run_snapshot()
    native.invoke_allow_refusal(native.fake_trainer())
    assert native.trainer_calls == 0
    assert native.run_snapshot() == before
    assert not (native.model_root / "narrator").exists()
    native.assert_sources_retained()


@pytest.mark.parametrize(
    "model_name", ["../escape", "__ABSOLUTE__", "__pycache__/narrator", r"custom\narrator"]
)
def test_wrapper_unsafe_name_refuses_before_environment_or_process(native, model_name):
    if model_name == "__ABSOLUTE__":
        model_name = str(native.paths.root.parent / "escaped-wrapper")
    with (
        mock.patch.object(
            trainer, "validate_training_environment", return_value=(False, "environment witness")
        ) as environment,
        mock.patch.object(trainer.subprocess, "Popen") as process,
    ):
        success, _message = trainer.start_training(
            {"model_name": model_name, "source_audio_path": str(native.audio)}
        )
    assert not success
    environment.assert_not_called()
    process.assert_not_called()
    native.assert_sources_retained()


def test_wrapper_preexisting_cancellation_never_starts_process_or_copies(native):
    native.cancel_event.set()
    with (
        mock.patch.object(
            trainer, "validate_training_environment", return_value=(False, "environment witness")
        ),
        mock.patch.object(trainer.subprocess, "Popen") as process,
        mock.patch.object(trainer, "_copy_trained_model") as copy_model,
    ):
        success, message = trainer.start_training(
            {"model_name": "narrator", "source_audio_path": str(native.audio)},
            stop_event=native.cancel_event,
        )
    assert not success
    assert "cancel" in message.casefold()
    process.assert_not_called()
    copy_model.assert_not_called()
    native.assert_sources_retained()


def test_exclusive_promotion_preserves_raced_empty_directory(native):
    from pandrator.logic.xtts_model_paths import promote_training_model_directory

    staging = native.paths.root / "private-stage"
    target = native.paths.root / "raced-target"
    staging.mkdir()
    (staging / "model.pth").write_bytes(b"new weights")
    target.mkdir()
    original_inode = target.stat().st_ino
    with pytest.raises(FileExistsError):
        promote_training_model_directory(staging, target)
    assert target.stat().st_ino == original_inode
    assert list(target.iterdir()) == []
    assert (staging / "model.pth").read_bytes() == b"new weights"


def test_final_claim_recheck_refuses_expiry_during_registration(native):
    native.seed()
    native.make_bundle()
    original = native.handlers.artifacts.register_in_session

    def expire(session, *args, **kwargs):
        result = original(session, *args, **kwargs)
        session.get(Job, native.job_id).lease_expires_at = utcnow() - timedelta(seconds=1)
        session.flush()
        return result

    with mock.patch.object(native.handlers.artifacts, "register_in_session", side_effect=expire):
        native.invoke_allow_refusal(native.fake_trainer())
    native.assert_unpublished()


def test_postpublication_progress_failure_preserves_committed_success(native):
    native.seed()
    native.make_bundle()

    def fake(settings, **kwargs):
        result = native.fake_trainer()(settings, **kwargs)
        kwargs["status_callback"]("Training completed")
        return result

    def progress(value, _message):
        if value >= 0.98:
            raise RuntimeError("notification unavailable")

    with mock.patch.object(trainer, "start_training", side_effect=fake):
        result = native.handlers.train_xtts(native.payload, progress, native.cancel_event)
    snapshot = native.run_snapshot()
    assert snapshot["status"] == "succeeded"
    assert snapshot["output_artifact_id"] == result["artifact_id"]
    assert (native.model_root / "narrator" / "model.pth").read_bytes() == native.bundle_bytes[
        "model.pth"
    ]
    native.assert_sources_retained()


def test_commit_failure_preserves_replaced_publication_ownership(native):
    native.seed()
    native.make_bundle()
    target = native.model_root / "narrator"
    retained = native.model_root / ".downloads" / "retained-original"

    def replace_before_commit(connection):
        status = connection.scalar(
            select(TrainingRun.status).where(TrainingRun.id == native.training_id)
        )
        if status == "succeeded":
            target.rename(retained)
            target.mkdir()
            (target / "foreign.txt").write_bytes(b"foreign replacement")
            raise RuntimeError("commit refused after replacement")

    event.listen(native.database.engine, "commit", replace_before_commit)
    try:
        with pytest.raises(RuntimeError, match="commit refused"):
            native.invoke(native.fake_trainer())
    finally:
        event.remove(native.database.engine, "commit", replace_before_commit)
    assert (target / "foreign.txt").read_bytes() == b"foreign replacement"
    assert (retained / "model.pth").read_bytes() == native.bundle_bytes["model.pth"]
    with native.database.snapshot_session() as session:
        assert list(session.scalars(select(Artifact).where(Artifact.role == "xtts_model"))) == []
        assert list(session.scalars(select(ArtifactEdge))) == []
    assert native.run_snapshot()["output_artifact_id"] is None
    native.assert_sources_retained()


@pytest.mark.parametrize("mode", ["success", "cancel", "missing-record"])
def test_wrapper_uses_managed_root_and_checks_stop_after_process_wait(native, mode):
    cancel_after_wait = mode == "cancel"
    native.seed()
    native.make_bundle()
    legacy_root = native.paths.root / "unused-legacy-models"
    process = mock.Mock(stdout=[], returncode=0)
    if cancel_after_wait:
        process.wait.side_effect = native.cancel_event.set
    paths = {
        "trainer_dir": str(native.trainer_root),
        "xtts_models_dir": str(legacy_root),
        "pixi_executable": "pixi",
        "trainer_manifest": "pixi.toml",
        "trainer_script": "train.py",
    }
    with (
        mock.patch.object(trainer, "get_training_paths", return_value=paths),
        mock.patch.object(trainer, "validate_training_environment", return_value=(True, "ready")),
        mock.patch.object(trainer, "_build_trainer_subprocess_env", return_value={}),
        mock.patch.object(trainer.subprocess, "Popen", return_value=process) as launch,
    ):

        def missing_record(_staging, _target):
            raise ValueError("Training record not found.")

        def start():
            return trainer.start_training(
                {
                    "model_name": "narrator",
                    "source_audio_path": str(native.audio),
                    "alignment_model": None,
                },
                stop_event=native.cancel_event,
                model_root=native.model_root,
                publish_callback=missing_record if mode == "missing-record" else None,
            )

        if mode == "missing-record":
            with pytest.raises(ValueError, match="Training record not found"):
                start()
        else:
            success, _message = start()
            assert success is not cancel_after_wait
    launch.assert_called_once()
    assert not legacy_root.exists()
    assert paths["xtts_models_dir"] == str(legacy_root)
    assert (native.model_root / "narrator").exists() is (mode == "success")
    native.assert_sources_retained()
