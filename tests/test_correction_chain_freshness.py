import tempfile
import unittest

from pandrator.web.api import create_app
from pandrator.web.artifact_selection import choose_artifact
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.jobs import JobQueue
from pandrator.web.media_edit import MediaEditService
from pandrator.web.models import (
    Artifact,
    OutcomePlan,
    SessionSetting,
    SessionSource,
    SessionStageSelection,
    SourceAsset,
)
from pandrator.web.workflows import WorkflowService
from tests.web_test_support import prepare_web_test_data_root


class CorrectionChainFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        prepare_web_test_data_root(self.temporary.name)
        bootstrap = BootstrapTokenStore()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=bootstrap,
            background_maintenance=False,
        )
        self.client = self.app.test_client()
        token = bootstrap.issue()
        self.client.post("/api/v1/auth/bootstrap", json={"token": token})
        self.extension = self.app.extensions["pandrator"]
        self.database = self.extension["database"]
        self.workflow = WorkflowService(
            self.database,
            JobQueue(self.database),
        )
        self.session_counter = 0

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def _session(self):
        record = self.extension["sessions"].create(
            f"Correction freshness {self.session_counter}",
            workflow_kind="subtitles",
            source_language="en",
            target_language="de",
        )
        self.session_counter += 1
        return record.id

    def _artifact(self, session_id, name, *, role, text, parent_ids=None, language="en"):
        directory = (
            self.extension["paths"].sessions
            / self.extension["sessions"].get(session_id).storage_key
        )
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(
            f"1\n00:00:00,000 --> 00:00:01,000\n{text}\n",
            encoding="utf-8",
        )
        artifact = self.extension["artifacts"].register(
            path,
            kind="srt",
            role=role,
            session_id=session_id,
            parent_ids=parent_ids,
        )
        self.extension["workflow_handlers"]._store_srt_document(
            session_id,
            artifact,
            role,
            language=language,
        )
        return artifact.id

    def _set_source_metadata(self, artifact_id, *, source_artifact_id, source_hash):
        with self.database.session() as session:
            artifact = session.get(Artifact, artifact_id)
            artifact.metadata_json = {
                **(artifact.metadata_json or {}),
                "source_artifact_id": source_artifact_id,
                "source_content_hash": source_hash,
            }

    def _select(self, session_id, selections):
        with self.database.session() as session:
            for stage_key, artifact_id in selections:
                choose_artifact(session, session_id, stage_key, artifact_id)

    def _replace_selected_source(self, session_id, artifact_id):
        with self.database.session() as session:
            selection = session.get(
                SessionStageSelection,
                (session_id, "transcribe"),
            )
            selection.artifact_id = artifact_id
            selection.revision += 1

    def _set_translation_source(self, session_id, artifact_id):
        with self.database.session() as session:
            setting = session.get(SessionSetting, (session_id, "translation"))
            if setting is None:
                setting = SessionSetting(
                    session_id=session_id,
                    section="translation",
                    value_json={},
                    revision=1,
                )
                session.add(setting)
            else:
                setting.revision += 1
            setting.value_json = {"source_artifact_id": artifact_id}

    def _stage(self, session_id, key):
        snapshot = self.workflow.snapshot(session_id)
        return next(item for item in snapshot["stages"] if item["key"] == key)

    def test_correction_chain_is_fresh_only_on_selected_upstream_branch(self):
        session_id = self._session()
        selected_source = self._artifact(
            session_id,
            "source-selected.srt",
            role="transcription",
            text="Same source bytes.",
        )
        sibling_source = self._artifact(
            session_id,
            "source-sibling.srt",
            role="transcription",
            text="Same source bytes.",
        )
        first_correction = self._artifact(
            session_id,
            "correction-first.srt",
            role="correction",
            text="Same source bytes.",
            parent_ids=[selected_source],
        )
        second_correction = self._artifact(
            session_id,
            "correction-second.srt",
            role="correction",
            text="Same source bytes.",
            parent_ids=[first_correction],
        )
        with self.database.session() as session:
            source = session.get(Artifact, first_correction)
            output = session.get(Artifact, second_correction)
            self.assertEqual(
                session.get(Artifact, selected_source).content_hash,
                session.get(Artifact, sibling_source).content_hash,
            )
            output.metadata_json = {
                **(output.metadata_json or {}),
                "source_artifact_id": first_correction,
                "source_content_hash": source.content_hash,
            }
        self._select(
            session_id,
            (("transcribe", selected_source), ("correct", second_correction)),
        )

        self.assertEqual("completed", self._stage(session_id, "correct")["status"])

        self._replace_selected_source(session_id, sibling_source)
        correction_stage = self._stage(session_id, "correct")
        self.assertEqual("stale", correction_stage["status"])
        self.assertEqual("prerequisite_superseded", correction_stage["stale_reason"])

    def test_translation_stays_stale_when_exact_selected_source_changes(self):
        session_id = self._session()
        selected_source = self._artifact(
            session_id,
            "translation-source-selected.srt",
            role="transcription",
            text="Same source bytes.",
        )
        sibling_source = self._artifact(
            session_id,
            "translation-source-sibling.srt",
            role="transcription",
            text="Same source bytes.",
        )
        translation = self._artifact(
            session_id,
            "translation.srt",
            role="translation",
            text="Dieselben Quellbytes.",
            parent_ids=[selected_source],
            language="de",
        )
        with self.database.session() as session:
            source = session.get(Artifact, selected_source)
            output = session.get(Artifact, translation)
            output.metadata_json = {
                **(output.metadata_json or {}),
                "source_artifact_id": selected_source,
                "source_content_hash": source.content_hash,
            }
        self._select(
            session_id,
            (("transcribe", selected_source), ("translate", translation)),
        )
        self._set_translation_source(session_id, selected_source)

        self.assertEqual("completed", self._stage(session_id, "translate")["status"])

        self._set_translation_source(session_id, sibling_source)
        translation_stage = self._stage(session_id, "translate")
        self.assertEqual("stale", translation_stage["status"])
        self.assertEqual("prerequisite_superseded", translation_stage["stale_reason"])

    def test_direct_generation_uses_selected_media_edit_subtitles(self):
        record = self.extension["sessions"].create(
            "Media edit generation source",
            workflow_kind="media_edit",
            source_language="en",
            target_language="de",
        )
        session_id = record.id
        directory = self.extension["paths"].sessions / record.storage_key
        directory.mkdir(parents=True, exist_ok=True)
        video_path = directory / "source.mp4"
        video_path.write_bytes(b"media edit source fixture")
        original = self.extension["artifacts"].register(
            video_path,
            kind="video",
            role="upload",
            session_id=session_id,
            metadata={"original_filename": "source.mp4"},
        )
        transcription_id = self._artifact(
            session_id,
            "generation-transcription.srt",
            role="transcription",
            text="Source transcript.",
            parent_ids=[original.id],
        )
        with self.database.session() as session:
            asset = SourceAsset(
                artifact_id=original.id,
                display_name="source.mp4",
                kind="mp4",
                mime_type="video/mp4",
            )
            session.add(asset)
            session.flush()
            session.add(
                SessionSource(
                    session_id=session_id,
                    source_asset_id=asset.id,
                    role="primary",
                    is_current=True,
                )
            )
            session.add(
                OutcomePlan(
                    session_id=session_id,
                    value_json={
                        "workflow_kind": "media_edit",
                        "deliverables": {"edited_media": True},
                        "transformations": {
                            "transcribe": True,
                            "media_edit": True,
                            "correct": True,
                            "translate": True,
                            "generate_audio": False,
                        },
                        "inputs": {
                            "translation": "translation",
                            "generation": "media_edit",
                        },
                    },
                )
            )
        plan = MediaEditService(
            self.database,
            self.extension["artifacts"],
            lambda _session_id: directory,
            duration_probe=lambda _path: 5000,
        ).prepare(session_id)["plan"]

        edited_subtitles_id = self._artifact(
            session_id,
            "edited-subtitles.srt",
            role="media_edit_subtitles",
            text="Edited source transcript.",
            parent_ids=[original.id, transcription_id],
        )
        with self.database.session() as session:
            edited_subtitles = session.get(Artifact, edited_subtitles_id)
            edited_subtitles.metadata_json = {
                **(edited_subtitles.metadata_json or {}),
                "media_edit_revision_id": plan["revision_id"],
                "content_hash": plan["content_hash"],
                "plan_id": plan["plan_id"],
                "revision": plan["revision"],
            }
        correction_id = self._artifact(
            session_id,
            "edited-correction.srt",
            role="correction",
            text="Corrected edited source.",
            parent_ids=[edited_subtitles_id],
        )
        translation_id = self._artifact(
            session_id,
            "edited-translation.srt",
            role="translation",
            text="Übersetzte Quelle.",
            parent_ids=[correction_id],
            language="de",
        )
        self._select(
            session_id,
            (
                ("edit_media", edited_subtitles_id),
                ("correct", correction_id),
                ("translate", translation_id),
            ),
        )

        resolved = self.workflow.resolve_stage(session_id, "generate_audio")

        self.assertEqual(edited_subtitles_id, resolved.source_artifact_id)


if __name__ == "__main__":
    unittest.main()
