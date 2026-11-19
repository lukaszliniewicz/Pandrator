"""Queue a translation project's branch forks instead of holding the request."""

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import delete, func, select

from pandrator.web import translation_project_jobs
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.fork_file_preparation import _PreparedForkFiles
from pandrator.web.jobs import Worker
from pandrator.web.model_base import utcnow
from pandrator.web.models import (
    Artifact,
    Document,
    DocumentRevision,
    Job,
    JobActorRecord,
    Project,
    SessionRecord,
    TranslationProject,
    TranslationProjectBranch,
    WorkspaceMembership,
)
from pandrator.web.projects.models import ProjectMembership
from pandrator.web.session_forks import SessionForkService
from pandrator.web.translation_project_jobs import run_creation
from tests.test_job_access_routes import case as case  # noqa: F401
from tests.test_job_access_routes import credential
from tests.web_test_support import prepare_web_test_data_root

URL = "/api/v1/sessions/{session_id}/translation-project"


class TranslationProjectJobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        prepare_web_test_data_root(self.temporary.name)
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name, testing=True, bootstrap_tokens=bootstrap
        )
        self.client = self.app.test_client()
        csrf = self.client.post(
            "/api/v1/auth/bootstrap", json={"token": token}
        ).get_json()["csrf_token"]
        self.headers = {"X-CSRF-Token": csrf}
        extension = self.app.extensions["pandrator"]
        self.database = extension["database"]
        self.paths = extension["paths"]
        self.artifacts = extension["artifacts"]
        self.jobs = extension["jobs"]
        self.handlers = extension["workflow_handlers"]

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def _session(self, *, setup=None, name="Queued languages"):
        body = {
            "name": name,
            "workflow_kind": "subtitles",
            "source_language": "en",
            "included_stages": ["correct", "translate", "export"],
        }
        if setup is not None:
            body["multilingual_setup"] = setup
        response = self.client.post(
            "/api/v1/sessions",
            json=body,
            headers={**self.headers, "Idempotency-Key": f"session-{uuid4().hex}"},
        )
        self.assertEqual(201, response.status_code, response.get_json())
        return response.get_json()

    def _checkpoint(self, session_id: str):
        with self.database.session() as db:
            record = db.get(SessionRecord, session_id)
            directory = self.paths.sessions / record.storage_key
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "correction.srt"
        path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello.\n", encoding="utf-8")
        checkpoint = self.artifacts.register(
            path, kind="srt", role="correction", session_id=session_id,
            metadata={"language": "en"},
        )
        with self.database.session() as db:
            document = Document(session_id=session_id, stage="correction", language="en")
            db.add(document)
            db.flush()
            revision = DocumentRevision(
                document_id=document.id,
                revision_number=1,
                content_hash=checkpoint.content_hash,
                reviewed=True,
            )
            db.add(revision)
            db.flush()
            document.active_revision_id = revision.id
            artifact = db.get(Artifact, checkpoint.id)
            artifact.metadata_json = {
                **(artifact.metadata_json or {}),
                "revision_id": revision.id,
            }
        return checkpoint

    def _revision(self, session_id: str) -> int:
        with self.database.session() as db:
            return int(db.get(SessionRecord, session_id).revision)

    def _post(self, session_id: str, payload: dict, key: str):
        return self.client.post(
            URL.format(session_id=session_id),
            json=payload,
            headers={**self.headers, "Idempotency-Key": key},
        )

    def _run_queued(self) -> int:
        worker = Worker(self.jobs, "translation-project-test", self.handlers.handlers())
        ran = 0
        while worker.run_once():
            ran += 1
        return ran

    def _count(self, model) -> int:
        with self.database.session() as db:
            return int(db.scalar(select(func.count()).select_from(model)) or 0)

    def _state(self, session_id: str) -> dict:
        response = self.client.get(URL.format(session_id=session_id))
        self.assertEqual(200, response.status_code, response.get_json())
        return response.get_json()

    def test_synchronous_creation_is_unchanged(self):
        created = self._session()
        checkpoint = self._checkpoint(created["id"])
        response = self._post(
            created["id"],
            {"checkpoint_artifact_id": checkpoint.id,
             "expected_revision": self._revision(created["id"])},
            "sync-project-key",
        )
        self.assertEqual(200, response.status_code, response.get_json())
        self.assertEqual(created["id"], response.get_json()["project"]["source_session_id"])
        self.assertEqual(0, self._count(Job))
        self.assertEqual(1, self._count(TranslationProject))

    def test_queued_creation_returns_a_handle_and_its_worker_creates_the_project(self):
        created = self._session(
            setup={"target_languages": ["PL", "de"], "generate_voiceover": False}
        )
        checkpoint = self._checkpoint(created["id"])
        payload = {
            "checkpoint_artifact_id": checkpoint.id,
            "expected_revision": self._revision(created["id"]),
            "create_planned_branches": True,
            "run_as_job": True,
        }
        response = self._post(created["id"], payload, "queued-project-key")
        self.assertEqual(202, response.status_code, response.get_json())
        handle = response.get_json()
        self.assertEqual("translation_project.create", handle["kind"])
        self.assertEqual("queued", handle["status"])
        self.assertEqual(created["id"], handle["session_id"])

        # A queued creation is not an absent project.
        pending = self._state(created["id"])
        self.assertIsNone(pending["project"])
        self.assertEqual(handle["id"], pending["pending_work_id"])
        compact = self.client.get(
            f"{URL.format(session_id=created['id'])}?view=compact"
        ).get_json()
        self.assertIsNone(compact["project"])
        self.assertEqual(handle["id"], compact["pending_work_id"])

        self.assertTrue(self._run_queued())
        with self.database.session() as db:
            job = db.get(Job, handle["id"])
        self.assertEqual("succeeded", job.status, job.error_message)
        self.assertIsNone(job.error_code)
        summary = job.result_json or {}
        self.assertEqual(1, self._count(TranslationProject))
        self.assertEqual(2, summary["branch_count"])
        self.assertEqual({"pl", "de"}, {
            branch["target_language"] for branch in summary["branches"]
        })
        branch_sessions = {branch["session_id"] for branch in summary["branches"]}
        self.assertEqual(2, len(branch_sessions))
        self.assertEqual(2, self._count(TranslationProjectBranch))

        project = self._state(created["id"])["project"]
        self.assertEqual(summary["project_id"], project["id"])
        self.assertEqual(branch_sessions, {item["session_id"] for item in project["branches"]})
        self.assertEqual(
            {"pl", "de"},
            {item["target_language"] for item in project["branches"]},
        )
        self.assertNotIn("pending_work_id", self._state(created["id"]))
        for session_id in branch_sessions:
            self.assertEqual(200, self.client.get(
                f"/api/v1/sessions/{session_id}/translation-project"
            ).status_code)

    def test_retrying_the_same_request_returns_the_same_handle(self):
        created = self._session()
        checkpoint = self._checkpoint(created["id"])
        payload = {
            "checkpoint_artifact_id": checkpoint.id,
            "expected_revision": self._revision(created["id"]),
            "run_as_job": True,
        }
        first = self._post(created["id"], payload, "retried-project-key")
        self.assertEqual(202, first.status_code, first.get_json())
        replay = self._post(created["id"], payload, "retried-project-key")
        self.assertEqual(202, replay.status_code, replay.get_json())
        self.assertEqual("true", replay.headers.get("Idempotency-Replayed"))
        self.assertEqual(first.get_json()["id"], replay.get_json()["id"])
        self.assertEqual(1, self._count(Job))

        # The replay survives the job finishing, and it never re-enqueues.
        self._run_queued()
        after = self._post(created["id"], payload, "retried-project-key")
        self.assertEqual(202, after.status_code, after.get_json())
        self.assertEqual(first.get_json()["id"], after.get_json()["id"])
        self.assertEqual(1, self._count(Job))
        self.assertEqual(1, self._count(TranslationProject))

    def test_a_validation_failure_answers_at_once_and_enqueues_nothing(self):
        created = self._session()
        checkpoint = self._checkpoint(created["id"])
        payload = {
            "checkpoint_artifact_id": checkpoint.id,
            "expected_revision": self._revision(created["id"]),
            "create_planned_branches": True,
        }
        synchronous = self._post(
            created["id"], {**payload, "run_as_job": False}, "invalid-sync-key"
        )
        queued = self._post(
            created["id"], {**payload, "run_as_job": True}, "invalid-queued-key"
        )
        self.assertEqual(422, synchronous.status_code, synchronous.get_json())
        self.assertEqual(422, queued.status_code, queued.get_json())
        self.assertEqual(
            synchronous.get_json()["error"]["code"],
            queued.get_json()["error"]["code"],
        )
        self.assertEqual(0, self._count(Job))
        self.assertEqual(0, self._count(TranslationProject))

        stale = self._post(
            created["id"],
            {
                **payload,
                "run_as_job": True,
                "expected_revision": self._revision(created["id"]) + 5,
            },
            "stale-queued-key",
        )
        self.assertEqual(409, stale.status_code, stale.get_json())
        self.assertEqual(0, self._count(Job))

    def test_a_second_creation_refuses_and_names_the_existing_work(self):
        created = self._session()
        checkpoint = self._checkpoint(created["id"])
        payload = {
            "checkpoint_artifact_id": checkpoint.id,
            "expected_revision": self._revision(created["id"]),
            "run_as_job": True,
        }
        first = self._post(created["id"], payload, "first-queued-key")
        self.assertEqual(202, first.status_code, first.get_json())
        second = self._post(created["id"], payload, "second-queued-key")
        self.assertEqual(409, second.status_code, second.get_json())
        message = second.get_json()["error"]["message"]
        self.assertIn(first.get_json()["id"], message)
        self.assertEqual(1, self._count(Job))

        self._run_queued()
        # Once the project exists the refusal names it instead.
        with self.database.session() as db:
            project_id = db.scalar(select(TranslationProject.id))
        existing = self._post(created["id"], payload, "existing-project-key")
        self.assertEqual(409, existing.status_code, existing.get_json())
        self.assertIn(project_id, existing.get_json()["error"]["message"])
        self.assertEqual(1, self._count(TranslationProject))

    def test_a_synchronous_creation_refuses_while_a_job_is_queued(self):
        created = self._session()
        checkpoint = self._checkpoint(created["id"])
        payload = {
            "checkpoint_artifact_id": checkpoint.id,
            "expected_revision": self._revision(created["id"]),
        }
        queued = self._post(created["id"], {**payload, "run_as_job": True}, "queued-first-key")
        self.assertEqual(202, queued.status_code, queued.get_json())
        synchronous = self._post(created["id"], payload, "synchronous-second-key")
        self.assertEqual(409, synchronous.status_code, synchronous.get_json())
        self.assertIn(queued.get_json()["id"], synchronous.get_json()["error"]["message"])
        self.assertEqual(0, self._count(TranslationProject))

        # The queued job, not the refused request, creates the project.
        self._run_queued()
        self.assertEqual(1, self._count(TranslationProject))

    def test_a_fork_failure_leaves_no_project_or_prepared_directory(self):
        created = self._session(setup={"target_languages": ["pl"]})
        checkpoint = self._checkpoint(created["id"])
        before = {item.name for item in self.paths.sessions.iterdir()}
        payload = {
            "checkpoint_artifact_id": checkpoint.id,
            "expected_revision": self._revision(created["id"]),
            "create_planned_branches": True,
            "run_as_job": True,
        }
        self.assertEqual(202, self._post(created["id"], payload, "failing-fork-key").status_code)

        def fail(*args, **kwargs):
            raise ValueError("Injected branch fork failure")

        with patch.object(SessionForkService, "fork_in_session", fail):
            self.assertTrue(self._run_queued())
        with self.database.session() as db:
            job = db.scalar(select(Job).where(Job.kind == "translation_project.create"))
        self.assertEqual("failed", job.status, job.result_json)
        self.assertIn("Injected branch fork failure", job.error_message)
        self.assertEqual(0, self._count(TranslationProject))
        self.assertEqual(0, self._count(TranslationProjectBranch))
        self.assertEqual(1, self._count(SessionRecord))
        self.assertEqual(before, {item.name for item in self.paths.sessions.iterdir()})
        self.assertNotIn("pending_work_id", self._state(created["id"]))

    def test_the_job_is_registered_with_its_required_payload_fields(self):
        registry = self.handlers.handler_registry
        registration = {
            item.kind: item for item in registry.registrations()
        }["translation_project.create"]
        self.assertEqual("projects", registration.domain)
        self.assertEqual(
            ("session_id", "checkpoint_artifact_id", "expected_revision"),
            registration.payload_contract.required_fields,
        )

    def _queued_payload(self, session_id: str, checkpoint_id: str, *, planned: bool) -> dict:
        return {
            "checkpoint_artifact_id": checkpoint_id,
            "expected_revision": self._revision(session_id),
            "create_planned_branches": planned,
            "run_as_job": True,
        }

    def _session_directory_names(self) -> set[str]:
        return {item.name for item in self.paths.sessions.iterdir()}

    def _revoke_edit_authority(self, session_id: str, job_id: str) -> None:
        with self.database.immediate_session() as db:
            actor = db.get(JobActorRecord, job_id)
            source = db.get(SessionRecord, session_id)
            project = db.get(Project, source.project_id)
            db.execute(
                delete(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == project.workspace_id,
                    WorkspaceMembership.user_id == actor.user_id,
                )
            )

    def _grant_viewing(self, session_id: str, job_id: str) -> None:
        with self.database.immediate_session() as db:
            actor = db.get(JobActorRecord, job_id)
            source = db.get(SessionRecord, session_id)
            db.add(
                ProjectMembership(
                    project_id=source.project_id,
                    user_id=actor.user_id,
                    role="viewer",
                    created_at=utcnow(),
                )
            )

    def test_revoked_edit_authority_fails_without_a_project_or_branch_files(self):
        created = self._session(setup={"target_languages": ["pl"]})
        checkpoint = self._checkpoint(created["id"])
        before = self._session_directory_names()
        queued = self._post(
            created["id"],
            self._queued_payload(created["id"], checkpoint.id, planned=True),
            "revoked-authority-key",
        )
        self.assertEqual(202, queued.status_code, queued.get_json())
        job_id = queued.get_json()["id"]
        # The route checked EDIT at enqueue; the worker must re-check the actor.
        self._revoke_edit_authority(created["id"], job_id)
        with patch.object(
            translation_project_jobs,
            "prepare_branch_files",
            wraps=translation_project_jobs.prepare_branch_files,
        ) as prepare:
            self.assertTrue(self._run_queued())
        # No fork tree is even collected, let alone materialised.
        prepare.assert_not_called()
        with self.database.session() as db:
            job = db.get(Job, job_id)
        self.assertEqual("failed", job.status, job.error_message)
        self.assertIn("not found", (job.error_message or "").lower())
        self.assertEqual(0, self._count(TranslationProject))
        self.assertEqual(0, self._count(TranslationProjectBranch))
        self.assertEqual(before, self._session_directory_names())

    def test_authority_revoked_while_files_materialise_creates_nothing(self):
        created = self._session(setup={"target_languages": ["pl"]})
        checkpoint = self._checkpoint(created["id"])
        before = self._session_directory_names()
        queued = self._post(
            created["id"],
            self._queued_payload(created["id"], checkpoint.id, planned=True),
            "revoked-while-materialising-key",
        )
        self.assertEqual(202, queued.status_code, queued.get_json())
        job_id = queued.get_json()["id"]
        materialize = _PreparedForkFiles.materialize

        def materialize_then_revoke(prepared):
            materialize(prepared)
            # The first check has passed. The actor keeps viewing access, so
            # only an EDIT check in the writer can refuse the commit.
            self._revoke_edit_authority(created["id"], job_id)
            self._grant_viewing(created["id"], job_id)

        with patch.object(_PreparedForkFiles, "materialize", materialize_then_revoke):
            self.assertTrue(self._run_queued())
        with self.database.session() as db:
            job = db.get(Job, job_id)
        self.assertEqual("failed", job.status, job.error_message)
        self.assertIn("not permitted", (job.error_message or "").lower())
        self.assertEqual(0, self._count(TranslationProject))
        self.assertEqual(0, self._count(TranslationProjectBranch))
        self.assertEqual(before, self._session_directory_names())

    def test_a_cancel_requested_job_creates_nothing(self):
        created = self._session(setup={"target_languages": ["pl"]})
        checkpoint = self._checkpoint(created["id"])
        before = self._session_directory_names()
        payload = self._queued_payload(created["id"], checkpoint.id, planned=True)
        queued = self._post(created["id"], payload, "cancel-requested-key")
        self.assertEqual(202, queued.status_code, queued.get_json())
        job_id = queued.get_json()["id"]
        with self.database.immediate_session() as db:
            db.get(Job, job_id).status = "cancel_requested"
        result = run_creation(
            self.database,
            self.paths,
            self.artifacts,
            {**payload, "session_id": created["id"], "_job_id": job_id},
        )
        self.assertEqual({}, result)
        self.assertEqual(0, self._count(TranslationProject))
        self.assertEqual(0, self._count(TranslationProjectBranch))
        self.assertEqual(before, self._session_directory_names())

    def test_a_committed_project_refuses_a_later_cancellation(self):
        created = self._session(setup={"target_languages": ["pl"]})
        checkpoint = self._checkpoint(created["id"])
        queued = self._post(
            created["id"],
            self._queued_payload(created["id"], checkpoint.id, planned=True),
            "committed-cancel-key",
        )
        self.assertEqual(202, queued.status_code, queued.get_json())
        job_id = queued.get_json()["id"]
        self._run_queued()
        with self.database.session() as db:
            job = db.get(Job, job_id)
            self.assertEqual("succeeded", job.status, job.error_message)
            self.assertTrue((job.result_json or {}).get("project_id"))
        with self.database.immediate_session() as db:
            with self.assertRaises(ValueError) as raised:
                self.jobs.request_cancel_in_session(db, job_id)
        self.assertEqual(
            "The translation project has already been created; this job can no longer be cancelled.",
            str(raised.exception),
        )
        with self.database.session() as db:
            self.assertEqual("succeeded", db.get(Job, job_id).status)
        self.assertEqual(1, self._count(TranslationProject))

    def test_a_failing_final_progress_report_still_returns_the_result(self):
        created = self._session()
        checkpoint = self._checkpoint(created["id"])

        def progress(value, detail=None):
            if value == 1.0:
                raise RuntimeError("Injected final progress failure")

        result = run_creation(
            self.database,
            self.paths,
            self.artifacts,
            {
                "session_id": created["id"],
                "checkpoint_artifact_id": checkpoint.id,
                "expected_revision": self._revision(created["id"]),
            },
            progress=progress,
        )
        self.assertTrue(result["project_id"])
        self.assertEqual(1, self._count(TranslationProject))


def test_a_caller_without_edit_keeps_the_common_403(case):
    viewer = credential(case, "viewer")
    response = viewer.client.post(
        URL.format(session_id=case.a.id),
        json={
            "checkpoint_artifact_id": "missing-checkpoint",
            "expected_revision": 1,
            "run_as_job": True,
        },
        headers={**viewer.headers, "Idempotency-Key": "queued-creation-denied"},
    )
    assert response.status_code == 403, response.get_json()
    assert response.get_json()["error"]["code"] == "scope_denied"


if __name__ == "__main__":
    unittest.main()
