import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import (
    Artifact,
    ArtifactEdge,
    Document,
    Job,
    OutcomePlan,
    SessionRecord,
    SessionSetting,
    SessionSource,
)
from tests.web_test_support import prepare_web_test_data_root

SRT = "1\n00:00:00,000 --> 00:00:01,000\n{}\n"


class WebSessionForkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        prepare_web_test_data_root(self.temporary.name)
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=bootstrap,
        )
        self.client = self.app.test_client()
        csrf = self.client.post(
            "/api/v1/auth/bootstrap", json={"token": token}
        ).get_json()["csrf_token"]
        self.headers = {"X-CSRF-Token": csrf}
        response = self.client.post(
            "/api/v1/sessions",
            json={
                "name": "Branchable subtitles",
                "workflow_kind": "voiceover",
                "source_language": "en",
                "target_language": "pl",
                "included_stages": [
                    "transcribe",
                    "correct",
                    "translate",
                    "generate_audio",
                    "export",
                ],
            },
            headers=self.headers,
        )
        self.assertEqual(201, response.status_code, response.get_json())
        self.record = response.get_json()
        extension = self.app.extensions["pandrator"]
        self.database = extension["database"]
        self.paths = extension["paths"]
        self.artifacts = extension["artifacts"]
        self.handlers = extension["workflow_handlers"]
        self.library = extension["source_library"]
        self.session_dir = self.paths.sessions / self.record["storage_key"]

        source_path = self.session_dir / "source.mp4"
        source_path.write_bytes(b"source media")
        self.source = self.artifacts.register(
            source_path,
            kind="source",
            role="upload",
            session_id=self.record["id"],
            metadata={"original_filename": "source.mp4"},
        )
        asset = self.library.ensure_for_artifact(
            self.source.id,
            display_name="source.mp4",
            kind="mp4",
        )
        self.library.attach(self.record["id"], asset.id)

        self.transcription = self._checkpoint("transcription", "Hello", self.source)
        self.correction = self._checkpoint("correction", "Hello!", self.transcription)
        self.translation = self._checkpoint("translation", "Cześć!", self.correction)
        downstream_path = self.session_dir / "assembled.wav"
        downstream_path.write_bytes(b"generated audio")
        self.artifacts.register(
            downstream_path,
            kind="audio",
            role="assembled_audio",
            session_id=self.record["id"],
            parent_ids=[self.translation.id],
        )
        with self.database.session() as session:
            session.add(
                SessionSetting(
                    session_id=self.record["id"],
                    section="translation",
                    value_json={
                        "reasoning_effort": "high",
                        "source_artifact_id": self.correction.id,
                    },
                )
            )
            session.add(
                OutcomePlan(
                    session_id=self.record["id"],
                    value_json={
                        "version": 1,
                        "workflow_kind": "voiceover",
                        "inputs": {"generation": "translation"},
                        "transformations": {"translate": True},
                        "deliverables": {"voiceover": True},
                    },
                )
            )
            session.add(
                Job(
                    kind="export.create",
                    session_id=self.record["id"],
                    status="succeeded",
                )
            )

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def _checkpoint(
        self,
        role: str,
        text: str,
        parent: Artifact,
        *,
        session_id: str | None = None,
        session_dir: Path | None = None,
    ) -> Artifact:
        session_id = session_id or self.record["id"]
        session_dir = session_dir or self.session_dir
        path = session_dir / f"{role}.srt"
        path.write_text(SRT.format(text), encoding="utf-8")
        artifact = self.artifacts.register(
            path,
            kind="srt",
            role=role,
            session_id=session_id,
            parent_ids=[parent.id],
            metadata={"language": "pl" if role == "translation" else "en"},
        )
        self.handlers._store_srt_document(
            session_id,
            artifact,
            role,
            language="pl" if role == "translation" else "en",
            parent_artifact=parent if role != "transcription" else None,
        )
        return artifact

    def test_translation_fork_copies_the_checkpoint_path_but_not_later_work(self):
        headers = {
            **self.headers,
            "Idempotency-Key": "translation-fork-retry-key",
        }
        response = self.client.post(
            f"/api/v1/sessions/{self.record['id']}/forks",
            json={
                "checkpoint_artifact_id": self.translation.id,
                "name": "Polish alternative",
            },
            headers=headers,
        )
        self.assertEqual(201, response.status_code, response.get_json())
        forked = response.get_json()
        self.assertEqual(
            ["transcription", "correction", "translation"],
            forked["copied_stages"],
        )

        replay = self.client.post(
            f"/api/v1/sessions/{self.record['id']}/forks",
            json={
                "checkpoint_artifact_id": self.translation.id,
                "name": "Polish alternative",
            },
            headers=headers,
        )
        self.assertEqual(201, replay.status_code, replay.get_json())
        self.assertEqual("true", replay.headers["Idempotency-Replayed"])
        self.assertEqual(forked["id"], replay.get_json()["id"])

        with self.database.session() as session:
            roles = list(
                session.scalars(
                    select(Artifact.role)
                    .where(Artifact.session_id == forked["id"])
                    .order_by(Artifact.created_at)
                ).all()
            )
            self.assertEqual(["transcription", "correction", "translation"], roles)
            cloned_translation = session.get(Artifact, forked["checkpoint_artifact_id"])
            self.assertNotEqual(self.translation.id, cloned_translation.id)
            self.assertEqual(
                self.translation.id,
                cloned_translation.metadata_json["forked_from_artifact_id"],
            )
            self.assertTrue(
                self.paths.managed_path(cloned_translation.relative_path).is_file()
            )
            documents = list(
                session.scalars(
                    select(Document)
                    .where(Document.session_id == forked["id"])
                    .order_by(Document.created_at)
                ).all()
            )
            self.assertEqual(
                ["transcription", "correction", "translation"],
                [item.stage for item in documents],
            )
            self.assertEqual(
                {
                    "reasoning_effort": "high",
                    "source_artifact_id": session.scalar(
                        select(Artifact.id).where(
                            Artifact.session_id == forked["id"],
                            Artifact.role == "correction",
                        )
                    ),
                },
                session.get(SessionSetting, (forked["id"], "translation")).value_json,
            )
            self.assertEqual(
                "translation",
                session.get(OutcomePlan, forked["id"]).value_json["inputs"][
                    "generation"
                ],
            )
            original_source = session.scalar(
                select(SessionSource).where(
                    SessionSource.session_id == self.record["id"],
                    SessionSource.is_current.is_(True),
                )
            )
            fork_source = session.scalar(
                select(SessionSource).where(SessionSource.session_id == forked["id"])
            )
            self.assertNotEqual(original_source.id, fork_source.id)
            self.assertEqual(
                original_source.source_asset_id, fork_source.source_asset_id
            )
            self.assertIsNone(
                session.scalar(select(Job).where(Job.session_id == forked["id"]))
            )

        workflow = self.client.get(
            f"/api/v1/sessions/{forked['id']}/workflow"
        ).get_json()
        stages = {item["key"]: item for item in workflow["stages"]}
        self.assertEqual(
            forked["checkpoint_artifact_id"],
            stages["translate"]["selected_artifact_id"],
        )
        self.assertEqual("completed", stages["translate"]["status"])
        self.assertEqual("ready", stages["generate_audio"]["status"])

    def test_correction_fork_accepts_child_translation(self):
        response = self.client.post(
            f"/api/v1/sessions/{self.record['id']}/forks",
            json={"checkpoint_artifact_id": self.correction.id},
            headers=self.headers,
        )
        self.assertEqual(201, response.status_code, response.get_json())
        forked = response.get_json()
        self.assertEqual(["transcription", "correction"], forked["copied_stages"])
        with self.database.session() as session:
            roles = set(
                session.scalars(
                    select(Artifact.role).where(Artifact.session_id == forked["id"])
                ).all()
            )
            child_correction_id = session.scalar(
                select(Artifact.id).where(
                    Artifact.session_id == forked["id"],
                    Artifact.role == "correction",
                )
            )
            child_storage_key = session.get(SessionRecord, forked["id"]).storage_key
            child_setting_value = dict(
                session.get(
                    SessionSetting,
                    (forked["id"], "translation"),
                ).value_json
            )
        self.assertEqual({"transcription", "correction"}, roles)
        self.assertEqual(
            {
                "reasoning_effort": "high",
                "source_artifact_id": child_correction_id,
            },
            child_setting_value,
        )

        child_correction, _path = self.artifacts.resolve(child_correction_id)
        child_translation = self._checkpoint(
            "translation",
            "Dzień dobry!",
            child_correction,
            session_id=forked["id"],
            session_dir=self.paths.sessions / child_storage_key,
        )
        with self.database.session() as session:
            self.assertIsNotNone(
                session.get(ArtifactEdge, (child_correction.id, child_translation.id))
            )

        workflow = self.client.get(
            f"/api/v1/sessions/{forked['id']}/workflow"
        ).get_json()
        stages = {item["key"]: item for item in workflow["stages"]}
        self.assertEqual("completed", stages["translate"]["status"])
        self.assertEqual("ready", stages["generate_audio"]["status"])

    def test_fork_drops_uncloned_translation_source_override(self):
        with self.database.session() as session:
            setting = session.get(
                SessionSetting,
                (self.record["id"], "translation"),
            )
            setting.value_json = {
                "reasoning_effort": "high",
                "source_artifact_id": self.source.id,
            }

        response = self.client.post(
            f"/api/v1/sessions/{self.record['id']}/forks",
            json={"checkpoint_artifact_id": self.correction.id},
            headers=self.headers,
        )
        self.assertEqual(201, response.status_code, response.get_json())
        forked = response.get_json()
        with self.database.session() as session:
            child_setting_value = dict(
                session.get(
                    SessionSetting,
                    (forked["id"], "translation"),
                ).value_json
            )
        self.assertEqual({"reasoning_effort": "high"}, child_setting_value)

    def test_non_checkpoint_artifacts_are_rejected(self):
        response = self.client.post(
            f"/api/v1/sessions/{self.record['id']}/forks",
            json={"checkpoint_artifact_id": self.transcription.id},
            headers=self.headers,
        )
        self.assertEqual(422, response.status_code, response.get_json())
        self.assertEqual("validation_error", response.get_json()["error"]["code"])


if __name__ == "__main__":
    unittest.main()
