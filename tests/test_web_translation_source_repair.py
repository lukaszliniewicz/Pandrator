import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from alembic import command
from alembic.config import Config

from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import SCHEMA_HEAD, Database, sqlite_url, upgrade_database
from pandrator.web.jobs import JobQueue
from pandrator.web.models import (
    Artifact,
    SessionRecord,
    SessionSetting,
    SessionSource,
    SessionStageSelection,
    SourceAsset,
)
from pandrator.web.sessions import SessionService
from pandrator.web.workflow_handlers import WorkflowHandlers
from pandrator.web.workflows import WorkflowService
from pandrator.web.workspace import OutcomePlanService
from tests.web_test_support import prepare_web_test_data_root

SRT = "1\n00:00:00,000 --> 00:00:01,000\n{}\n"


class TranslationSourceRepairMigrationTests(unittest.TestCase):
    def test_upgrade_repairs_cross_session_fork_translation_settings_once(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "pandrator.sqlite3"
            upgrade_database(database_path)
            config = Config()
            config.set_main_option(
                "script_location",
                str(Path(__file__).parents[1] / "pandrator" / "web" / "migrations"),
            )
            config.set_main_option("sqlalchemy.url", sqlite_url(database_path))
            command.downgrade(config, "0027_durable_agentic_runs")

            database = Database(database_path)
            try:
                with database.session() as session:
                    parent = SessionRecord(
                        id="parent-session",
                        name="Parent",
                        storage_key="parent-storage",
                    )
                    fork = SessionRecord(
                        id="fork-session",
                        name="Fork",
                        storage_key="fork-storage",
                    )
                    other_fork = SessionRecord(
                        id="other-fork-session",
                        name="Other fork",
                        storage_key="other-fork-storage",
                    )
                    local_setting = SessionRecord(
                        id="local-setting-session",
                        name="Local setting",
                        storage_key="local-setting-storage",
                    )
                    attached_setting = SessionRecord(
                        id="attached-setting-session",
                        name="Attached setting",
                        storage_key="attached-setting-storage",
                    )
                    missing_setting = SessionRecord(
                        id="missing-setting-session",
                        name="Missing setting",
                        storage_key="missing-setting-storage",
                    )
                    attached_media_setting = SessionRecord(
                        id="attached-media-setting-session",
                        name="Attached media setting",
                        storage_key="attached-media-setting-storage",
                    )
                    session.add_all(
                        [
                            parent,
                            fork,
                            other_fork,
                            local_setting,
                            attached_setting,
                            missing_setting,
                            attached_media_setting,
                        ]
                    )
                    session.flush()
                    foreign = Artifact(
                        id="foreign-correction",
                        session_id=parent.id,
                        kind="srt",
                        role="correction",
                        relative_path="parent/correction.srt",
                    )
                    local_clone = Artifact(
                        id="forked-correction",
                        session_id=fork.id,
                        kind="srt",
                        role="correction",
                        relative_path="fork/correction.srt",
                        metadata_json={
                            "forked_from_artifact_id": foreign.id,
                        },
                    )
                    local_valid = Artifact(
                        id="fork-local-source",
                        session_id=local_setting.id,
                        kind="srt",
                        role="correction",
                        relative_path="fork/local.srt",
                    )
                    foreign_media = Artifact(
                        id="foreign-media",
                        session_id=parent.id,
                        kind="video",
                        role="upload",
                        relative_path="parent/source.mp4",
                    )
                    session.add_all([foreign, local_clone, local_valid, foreign_media])
                    attached_asset = SourceAsset(
                        id="attached-foreign-asset",
                        artifact_id=foreign.id,
                        display_name="Attached foreign source",
                        kind="srt",
                    )
                    session.add(attached_asset)
                    session.flush()
                    session.add(
                        SessionSource(
                            id="attached-foreign-link",
                            session_id=attached_setting.id,
                            source_asset_id=attached_asset.id,
                            role="primary",
                            is_current=True,
                        )
                    )
                    attached_media_asset = SourceAsset(
                        id="attached-foreign-media-asset",
                        artifact_id=foreign_media.id,
                        display_name="Attached foreign media",
                        kind="video",
                    )
                    session.add(attached_media_asset)
                    session.flush()
                    session.add(
                        SessionSource(
                            id="attached-foreign-media-link",
                            session_id=attached_media_setting.id,
                            source_asset_id=attached_media_asset.id,
                            role="primary",
                            is_current=True,
                        )
                    )
                    session.add_all(
                        [
                            SessionSetting(
                                session_id=fork.id,
                                section="translation",
                                value_json={
                                    "source_artifact_id": foreign.id,
                                    "reasoning_effort": "high",
                                },
                            ),
                            SessionSetting(
                                session_id=other_fork.id,
                                section="translation",
                                value_json={
                                    "source_artifact_id": foreign.id,
                                    "instructions": "keep this setting",
                                },
                            ),
                            SessionSetting(
                                session_id=local_setting.id,
                                section="translation",
                                value_json={
                                    "source_artifact_id": local_valid.id,
                                },
                            ),
                            SessionSetting(
                                session_id=attached_setting.id,
                                section="translation",
                                value_json={"source_artifact_id": foreign.id},
                            ),
                            SessionSetting(
                                session_id=missing_setting.id,
                                section="translation",
                                value_json={
                                    "source_artifact_id": "missing-artifact",
                                    "instructions": "keep this setting",
                                },
                            ),
                            SessionSetting(
                                session_id=attached_media_setting.id,
                                section="translation",
                                value_json={
                                    "source_artifact_id": foreign_media.id,
                                    "instructions": "keep media guidance",
                                },
                            ),
                        ]
                    )
            finally:
                database.dispose()

            upgrade_database(database_path)
            upgrade_database(database_path)

            repaired = Database(database_path)
            try:
                with repaired.session() as session:
                    self.assertEqual(
                        {
                            "source_artifact_id": "forked-correction",
                            "reasoning_effort": "high",
                        },
                        session.get(
                            SessionSetting,
                            ("fork-session", "translation"),
                        ).value_json,
                    )
                    self.assertEqual(
                        {"instructions": "keep this setting"},
                        session.get(
                            SessionSetting,
                            ("other-fork-session", "translation"),
                        ).value_json,
                    )
                    self.assertEqual(
                        {"instructions": "keep media guidance"},
                        session.get(
                            SessionSetting,
                            ("attached-media-setting-session", "translation"),
                        ).value_json,
                    )
                    self.assertEqual(
                        {"source_artifact_id": "fork-local-source"},
                        session.get(
                            SessionSetting,
                            ("local-setting-session", "translation"),
                        ).value_json,
                    )
                    self.assertEqual(
                        {"source_artifact_id": "foreign-correction"},
                        session.get(
                            SessionSetting,
                            ("attached-setting-session", "translation"),
                        ).value_json,
                    )
                    self.assertEqual(
                        {"instructions": "keep this setting"},
                        session.get(
                            SessionSetting,
                            ("missing-setting-session", "translation"),
                        ).value_json,
                    )
            finally:
                repaired.dispose()
            with closing(sqlite3.connect(database_path)) as connection:
                self.assertEqual(
                    SCHEMA_HEAD,
                    connection.execute(
                        "SELECT version_num FROM alembic_version"
                    ).fetchone()[0],
                )


class TranslationSourceResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = prepare_web_test_data_root(self.temporary.name)
        self.database = Database(self.paths.database)
        self.sessions = SessionService(self.database)
        self.artifacts = ArtifactService(self.database, self.paths)
        self.workflows = WorkflowService(self.database, JobQueue(self.database))
        self.handlers = WorkflowHandlers(self.database, self.paths)
        self.parent = self.sessions.create("Parent", workflow_kind="voiceover")
        self.child = self.sessions.create("Child", workflow_kind="voiceover")
        self.parent_dir = self.paths.sessions / self.parent.storage_key
        self.child_dir = self.paths.sessions / self.child.storage_key
        self.parent_dir.mkdir()
        self.child_dir.mkdir()

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def _artifact(self, session_id, directory, name, role, text, *, parent_ids=()):
        path = directory / name
        path.write_text(SRT.format(text), encoding="utf-8")
        return self.artifacts.register(
            path,
            kind="srt",
            role=role,
            session_id=session_id,
            parent_ids=list(parent_ids),
        )

    def test_stale_foreign_translation_setting_falls_back_and_snapshot_names_selected_input(
        self,
    ):
        foreign = self._artifact(
            self.parent.id,
            self.parent_dir,
            "foreign.srt",
            "correction",
            "Foreign",
        )
        upload = self._artifact(
            self.child.id,
            self.child_dir,
            "source.srt",
            "upload",
            "Local source",
        )
        transcription = self._artifact(
            self.child.id,
            self.child_dir,
            "transcription.srt",
            "transcription",
            "Local transcription",
            parent_ids=[upload.id],
        )
        correction = self._artifact(
            self.child.id,
            self.child_dir,
            "correction.srt",
            "correction",
            "Local correction",
            parent_ids=[transcription.id],
        )
        first_translation = self._artifact(
            self.child.id,
            self.child_dir,
            "translation-one.srt",
            "translation",
            "First translation",
            parent_ids=[correction.id],
        )
        self._artifact(
            self.child.id,
            self.child_dir,
            "translation-two.srt",
            "translation",
            "Second translation",
            parent_ids=[correction.id],
        )
        with self.database.session() as session:
            session.add(
                SessionSetting(
                    session_id=self.child.id,
                    section="translation",
                    value_json={"source_artifact_id": foreign.id},
                )
            )
        outcome = OutcomePlanService(self.database)
        current = outcome.get(self.child.id)
        value = current["value"]
        value["inputs"] = {
            **value.get("inputs", {}),
            "translation": "correction",
            "generation": "translation",
        }
        value["transformations"] = {
            **value.get("transformations", {}),
            "translation": True,
            "generate_audio": True,
        }
        outcome.update(self.child.id, current["revision"], value)
        with self.database.session() as session:
            session.get(
                SessionStageSelection,
                (self.child.id, "translate"),
            ).artifact_id = first_translation.id

        resolved = self.workflows.resolve_stage(self.child.id, "translate")
        snapshot = self.workflows.snapshot(self.child.id)
        generation = next(
            stage for stage in snapshot["stages"] if stage["key"] == "generate_audio"
        )

        self.assertEqual(correction.id, resolved.source_artifact_id)
        self.assertEqual(correction.id, resolved.payload["source_artifact_id"])
        self.assertEqual(
            {
                "artifact_id": first_translation.id,
                "role": "translation",
                "stage_key": "translate",
                "version": 1,
                "label": "Translation",
                "origin": "stage",
                "selection_stage": "translate",
                "selected_artifact_id": first_translation.id,
            },
            generation["resolved_input"],
        )

        planned = self.workflows.resolve_stage(
            self.child.id,
            "generate_audio",
            continuation=True,
        )
        self.assertEqual(
            foreign.id,
            planned.payload["stage_settings"]["translate"]["source_artifact_id"],
        )
        with (
            mock.patch.object(
                self.handlers,
                "translate",
                return_value={"artifact_id": first_translation.id},
            ) as translate,
            mock.patch.object(
                self.handlers,
                "_run_reviewable_generation",
                return_value={"generation_run_id": "fixture", "status": "completed"},
            ),
        ):
            self.handlers.continue_workflow(
                {**planned.payload, "reuse_stages": ["correct"]},
                lambda _value, _detail=None: None,
                threading.Event(),
            )
        self.assertEqual(
            correction.id, translate.call_args.args[0]["source_artifact_id"]
        )

    def test_attached_media_translation_setting_falls_back_to_local_subtitles(self):
        media_path = self.parent_dir / "attached-source.mp4"
        media_path.write_bytes(b"media fixture")
        media = self.artifacts.register(
            media_path,
            kind="video",
            role="upload",
            session_id=self.parent.id,
        )
        source = self._artifact(
            self.child.id,
            self.child_dir,
            "local-source.srt",
            "upload",
            "Local source",
        )
        correction = self._artifact(
            self.child.id,
            self.child_dir,
            "local-correction.srt",
            "correction",
            "Local correction",
            parent_ids=[source.id],
        )
        with self.database.session() as session:
            asset = SourceAsset(
                id="attached-media-asset",
                artifact_id=media.id,
                display_name="Attached media",
                kind="video",
            )
            session.add(asset)
            session.flush()
            session.add_all(
                [
                    SessionSource(
                        id="attached-media-link",
                        session_id=self.child.id,
                        source_asset_id=asset.id,
                        role="primary",
                        is_current=False,
                    ),
                    SessionSetting(
                        session_id=self.child.id,
                        section="translation",
                        value_json={"source_artifact_id": media.id},
                    ),
                ]
            )
        outcome = OutcomePlanService(self.database)
        current = outcome.get(self.child.id)
        value = current["value"]
        value["inputs"] = {
            **value.get("inputs", {}),
            "translation": "correction",
            "generation": "translation",
        }
        value["transformations"] = {
            **value.get("transformations", {}),
            "translation": True,
            "generate_audio": True,
        }
        outcome.update(self.child.id, current["revision"], value)

        with (
            mock.patch.object(
                self.handlers,
                "translate",
                return_value={"artifact_id": "translated"},
            ) as translate,
            mock.patch.object(
                self.handlers,
                "_run_reviewable_generation",
                return_value={"generation_run_id": "fixture", "status": "completed"},
            ),
        ):
            self.handlers.continue_workflow(
                {
                    "session_id": self.child.id,
                    "target_stage": "translate",
                    "reuse_stages": ["correct"],
                },
                lambda _value, _detail=None: None,
                threading.Event(),
            )

        self.assertEqual(
            correction.id,
            translate.call_args.args[0]["source_artifact_id"],
        )


if __name__ == "__main__":
    unittest.main()
