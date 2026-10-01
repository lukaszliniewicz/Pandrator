"""Pinned multilingual projects keep independently editable language sessions."""

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import (
    Artifact,
    Job,
    OutcomePlan,
    SessionRecord,
    SessionSetting,
    TranslationProjectBranch,
)
from pandrator.web.settings_policy import RevisionConflict
from pandrator.web.translation_projects import (
    TranslationProjectConflict,
    create_branches_in_session,
    create_project_in_session,
    get_project,
    get_session_project,
)
from tests.web_test_support import prepare_web_test_data_root


class TranslationProjectServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        prepare_web_test_data_root(self.temporary.name)
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name, testing=True, bootstrap_tokens=bootstrap
        )
        self.client = self.app.test_client()
        csrf = self.client.post("/api/v1/auth/bootstrap", json={"token": token}).get_json()[
            "csrf_token"
        ]
        self.headers = {"X-CSRF-Token": csrf}
        extension = self.app.extensions["pandrator"]
        self.database = extension["database"]
        self.paths = extension["paths"]
        self.sessions = extension["sessions"]
        self.forks = extension["session_forks"]
        self.artifacts = extension["artifacts"]
        self.source = self.sessions.create(
            "Reviewed source",
            workflow_kind="subtitles",
            source_language="en",
            included_stages=["correct", "translate", "export"],
        )
        directory = self.paths.sessions / self.source.storage_key
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "correction.srt"
        path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello.\n", encoding="utf-8")
        self.checkpoint = self.artifacts.register(
            path,
            kind="srt",
            role="correction",
            session_id=self.source.id,
            metadata={"language": "en"},
        )

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def _create_project(self):
        with self.database.immediate_session() as db:
            return create_project_in_session(
                db,
                self.source.id,
                self.checkpoint.id,
                "Languages",
                self.source.revision,
                paths=self.paths,
            )

    def _create_branches(self, project_id, revision, targets):
        directories = []
        try:
            with self.database.immediate_session() as db:
                return create_branches_in_session(
                    db,
                    project_id,
                    revision,
                    targets,
                    session_forks=self.forks,
                    paths=self.paths,
                    created_directories=directories,
                )
        except Exception:
            import shutil

            for directory in directories:
                shutil.rmtree(directory, ignore_errors=True)
            raise

    def test_two_languages_pin_same_source_and_keep_settings_separate(self):
        project = self._create_project()["project"]
        result = self._create_branches(
            project["id"],
            project["revision"],
            [{"target_language": "PL"}, {"target_language": "de_DE"}],
        )["project"]
        self.assertEqual(2, result["revision"])
        self.assertEqual(["pl", "de-de"], [row["target_language"] for row in result["branches"]])
        self.assertEqual(
            ["ready", "ready"], [row["translation_status"] for row in result["branches"]]
        )
        with self.database.session() as db:
            source = db.get(SessionRecord, self.source.id)
            self.assertEqual(self.source.revision, source.revision)
            self.assertEqual("subtitles", source.workflow_kind)
            for branch in result["branches"]:
                record = db.get(SessionRecord, branch["session_id"])
                checkpoint = db.get(Artifact, branch["source_checkpoint_artifact_id"])
                setting = db.get(SessionSetting, (record.id, "translation"))
                outcome = db.get(OutcomePlan, record.id)
                self.assertEqual(branch["target_language"], record.target_language)
                self.assertEqual(self.checkpoint.content_hash, checkpoint.content_hash)
                self.assertEqual(self.checkpoint.content_hash, branch["source_content_hash"])
                self.assertEqual(checkpoint.id, setting.value_json["source_artifact_id"])
                self.assertTrue(setting.value_json["enabled"])
                self.assertEqual("translation", outcome.value_json["inputs"]["generation"])
                self.assertNotIn("correct", record.included_stages_json)
            first, second = [row["session_id"] for row in result["branches"]]
            db.get(SessionSetting, (first, "translation")).value_json = {"enabled": False}
        with self.database.session() as db:
            self.assertTrue(db.get(SessionSetting, (second, "translation")).value_json["enabled"])
            self.assertEqual(result["id"], get_session_project(db, first)["project"]["id"])
            self.assertEqual(result["id"], get_project(db, result["id"])["project"]["id"])
            db.add(Job(kind="dubbing.translate", session_id=first, status="queued"))
        with self.database.session() as db:
            branches = get_project(db, result["id"])["project"]["branches"]
            self.assertEqual(["running", "ready"], [row["translation_status"] for row in branches])

    def test_duplicate_invalid_and_revision_conflicts_leave_no_branches(self):
        project = self._create_project()["project"]
        with self.assertRaises(TranslationProjectConflict):
            self._create_project()
        for targets in (
            [{"target_language": "en"}],
            [{"target_language": "pl"}, {"target_language": "PL"}],
            [{"target_language": "bad language"}],
        ):
            with self.assertRaises((TranslationProjectConflict, ValueError)):
                self._create_branches(project["id"], 1, targets)
        with self.assertRaises(RevisionConflict):
            self._create_branches(project["id"], 2, [{"target_language": "pl"}])
        with self.database.session() as db:
            self.assertEqual([], db.scalars(select(TranslationProjectBranch)).all())

    def test_missing_and_changed_correction_rejected(self):
        with self.database.session() as db:
            self.assertEqual({"project": None}, get_session_project(db, self.source.id))
        with self.assertRaises(KeyError):
            with self.database.immediate_session() as db:
                create_project_in_session(
                    db,
                    self.source.id,
                    "other-artifact",
                    "Languages",
                    1,
                    paths=self.paths,
                )
        foreign = self.sessions.create("Other source", source_language="en")
        foreign_directory = self.paths.sessions / foreign.storage_key
        foreign_directory.mkdir(parents=True, exist_ok=True)
        foreign_path = foreign_directory / "correction.srt"
        foreign_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nOther.\n")
        foreign_checkpoint = self.artifacts.register(
            foreign_path, kind="srt", role="correction", session_id=foreign.id
        )
        with self.assertRaises(KeyError):
            with self.database.immediate_session() as db:
                create_project_in_session(
                    db,
                    self.source.id,
                    foreign_checkpoint.id,
                    "Languages",
                    1,
                    paths=self.paths,
                )
        project = self._create_project()["project"]
        with self.database.session() as db:
            db.get(Artifact, self.checkpoint.id).state = "stale"
        with self.assertRaises(TranslationProjectConflict):
            self._create_branches(project["id"], 1, [{"target_language": "pl"}])

    def test_project_foreign_keys_block_source_deletion(self):
        self._create_project()
        with self.assertRaises(IntegrityError):
            with self.database.immediate_session() as db:
                db.delete(db.get(SessionRecord, self.source.id))
        with self.database.session() as db:
            self.assertIsNotNone(db.get(SessionRecord, self.source.id))

    def test_http_replay_and_failed_batch_leave_no_orphan_sessions(self):
        create_headers = {**self.headers, "Idempotency-Key": "create-project-key"}
        create_payload = {
            "checkpoint_artifact_id": self.checkpoint.id,
            "expected_revision": self.source.revision,
        }
        url = f"/api/v1/sessions/{self.source.id}/translation-project"
        response = self.client.post(url, json=create_payload, headers=create_headers)
        self.assertEqual(200, response.status_code, response.get_json())
        replay = self.client.post(url, json=create_payload, headers=create_headers)
        self.assertEqual(200, replay.status_code)
        self.assertEqual("true", replay.headers.get("Idempotency-Replayed"))
        project = response.get_json()["project"]
        branches_url = f"/api/v1/translation-projects/{project['id']}/branches"
        branch_headers = {**self.headers, "Idempotency-Key": "create-branches-key"}
        payload = {
            "expected_revision": project["revision"],
            "targets": [{"target_language": "pl"}, {"target_language": "de"}],
        }
        result = self.client.post(branches_url, json=payload, headers=branch_headers)
        self.assertEqual(200, result.status_code, result.get_json())
        replay = self.client.post(branches_url, json=payload, headers=branch_headers)
        self.assertEqual(200, replay.status_code)
        self.assertEqual("true", replay.headers.get("Idempotency-Replayed"))
        self.assertEqual(result.get_json(), replay.get_json())
        conflict = self.client.post(
            branches_url,
            json={"expected_revision": 1, "targets": [{"target_language": "fr"}]},
            headers={**self.headers, "Idempotency-Key": "stale-branches-key"},
        )
        self.assertEqual(409, conflict.status_code)
        with self.database.session() as db:
            self.assertEqual(2, len(db.scalars(select(TranslationProjectBranch)).all()))
        before_directories = {item.name for item in self.paths.sessions.iterdir()}
        original_fork = self.forks.fork_in_session
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("Injected second-fork failure")
            return original_fork(*args, **kwargs)

        with patch.object(self.forks, "fork_in_session", side_effect=fail_second):
            failed = self.client.post(
                branches_url,
                json={
                    "expected_revision": 2,
                    "targets": [
                        {"target_language": "fr"},
                        {"target_language": "it"},
                    ],
                },
                headers={**self.headers, "Idempotency-Key": "failed-batch-key"},
            )
        self.assertEqual(422, failed.status_code)
        self.assertEqual(before_directories, {item.name for item in self.paths.sessions.iterdir()})
        with self.database.session() as db:
            self.assertEqual(2, len(db.scalars(select(TranslationProjectBranch)).all()))
            self.assertEqual(3, len(db.scalars(select(SessionRecord)).all()))


if __name__ == "__main__":
    unittest.main()
