"""Opt-in selected-take inspection keeps legacy take history available."""

from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from typing import get_args
from unittest.mock import patch

from sqlalchemy import event

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.generation_review import PROJECTABLE_SEGMENT_FIELDS
from pandrator.web.models import Artifact, AudioTake, GenerationRun, GenerationSegment
from pandrator.web.openapi import build_openapi_document
from pandrator.web.workspace_settings import WorkspaceSettingsService
from pandrator_mcp.schemas.generation_fields import GenerationSegmentField


class GenerationSelectedTakeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        bootstrap = BootstrapTokenStore()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=bootstrap,
        )
        self.services = self.app.extensions["pandrator"]
        self.database = self.services["database"]
        self.addCleanup(self.database.dispose)
        self.generation = self.services["generation"]
        self.client = self.app.test_client()
        token = bootstrap.issue()
        self.headers = {
            "X-CSRF-Token": self.client.post(
                "/api/v1/auth/bootstrap", json={"token": token}
            ).get_json()["csrf_token"]
        }
        self.session_id = self.client.post(
            "/api/v1/sessions",
            json={"name": "Selected-take inspection", "workflow_kind": "voiceover"},
            headers=self.headers,
        ).get_json()["id"]
        plan = self.generation.create_plan(
            self.session_id,
            source_revision_id=None,
            settings={},
            segments=[
                {"text": "First speech block."},
                {"text": "Second speech block."},
                {"text": "Third speech block."},
            ],
        )
        self.revision_id = plan["active_revision_id"]
        self.segment_ids = [
            item["id"] for item in self.generation.list_segments(self.session_id)["items"]
        ]
        with self.database.session() as session:
            for segment_id in self.segment_ids:
                session.get(GenerationSegment, segment_id).status = "completed"

    def add_take(
        self,
        segment_id: str,
        *,
        active: bool,
        status: str = "completed",
        with_artifact: bool = True,
        metadata_size: int = 0,
    ) -> tuple[str, str | None]:
        with self.database.session() as session:
            artifact_id = None
            if with_artifact:
                artifact = Artifact(
                    session_id=self.session_id,
                    kind="audio",
                    role="generation_take",
                    relative_path=f"test/{uuid.uuid4().hex}.wav",
                    metadata_json={"test_padding": "x" * metadata_size},
                )
                session.add(artifact)
                session.flush()
                artifact_id = artifact.id
            take = AudioTake(
                generation_segment_id=segment_id,
                artifact_id=artifact_id,
                status=status,
                is_active=active,
                duration_ms=120,
            )
            session.add(take)
            session.flush()
            return take.id, artifact_id

    def add_recast_run(
        self,
        before_take_ids: dict[str, str],
        after_take_ids: dict[str, str],
    ) -> str:
        with self.database.session() as session:
            run = GenerationRun(
                session_id=self.session_id,
                plan_revision_id=self.revision_id,
                sequence_number=900,
                operation="recast",
                audio_version_json={
                    "version": 1,
                    "kind": "recast",
                    "before_take_ids": before_take_ids,
                    "after_take_ids": after_take_ids,
                },
            )
            session.add(run)
            session.flush()
            return run.id

    def test_full_is_the_default_and_selected_projects_complete_take_metadata(self):
        old_id, _ = self.add_take(self.segment_ids[0], active=False)
        active_id, _ = self.add_take(self.segment_ids[0], active=True)
        inactive_id, _ = self.add_take(self.segment_ids[1], active=False)
        missing_artifact_id, _ = self.add_take(
            self.segment_ids[2], active=True, with_artifact=False
        )

        legacy = self.generation.list_segments(self.session_id)
        explicit_full = self.generation.list_segments(
            self.session_id, take_history="full"
        )
        self.assertEqual(legacy, explicit_full)
        self.assertEqual(
            {old_id, active_id},
            {take["id"] for take in legacy["items"][0]["takes"]},
        )
        self.assertNotIn("take_history_loaded", legacy["items"][0])

        selected = self.generation.list_segments(
            self.session_id, take_history="selected"
        )
        for full_item, selected_item in zip(
            explicit_full["items"], selected["items"], strict=True
        ):
            for key, value in full_item.items():
                if key != "takes":
                    self.assertEqual(value, selected_item[key], key)
        first, second, third = selected["items"]
        self.assertEqual([active_id], [take["id"] for take in first["takes"]])
        self.assertEqual(2, first["take_count"])
        self.assertFalse(first["take_history_loaded"])
        self.assertEqual(active_id, first["active_take_id"])
        self.assertTrue(first["has_usable_take"])
        self.assertEqual([], second["takes"])
        self.assertEqual(1, second["take_count"])
        self.assertFalse(second["take_history_loaded"])
        self.assertIsNone(second["active_take_id"])
        self.assertFalse(second["has_usable_take"])
        self.assertEqual([missing_artifact_id], [take["id"] for take in third["takes"]])
        self.assertIsNone(third["takes"][0]["artifact_id"])
        self.assertEqual(1, third["take_count"])
        self.assertTrue(third["take_history_loaded"])
        self.assertIsNone(third["active_take_id"])
        self.assertFalse(third["has_usable_take"])

        compact = self.generation.list_segments(
            self.session_id, view="compact", take_history="selected"
        )["items"]
        self.assertEqual(2, compact[0]["take_count"])
        self.assertEqual(active_id, compact[0]["active_take_id"])
        self.assertTrue(compact[0]["has_usable_take"])
        self.assertFalse(compact[0]["take_history_loaded"])

    def test_historical_cutoff_keeps_exact_latest_take_without_unavailable_fallback(self):
        segment_id = self.segment_ids[0]
        before_id, _ = self.add_take(segment_id, active=False)
        chosen_id, artifact_id = self.add_take(segment_id, active=False, status="stale")
        newer_id, _ = self.add_take(segment_id, active=True)
        with self.database.session() as session:
            old = GenerationRun(session_id=self.session_id, plan_revision_id=self.revision_id,
                                sequence_number=1, status="completed")
            cutoff = GenerationRun(session_id=self.session_id, plan_revision_id=self.revision_id,
                                   sequence_number=2, status="completed")
            newer = GenerationRun(session_id=self.session_id, plan_revision_id=self.revision_id,
                                  sequence_number=3, status="completed")
            session.add_all([old, cutoff, newer])
            session.flush()
            for take_id, run in ((before_id, old), (chosen_id, cutoff), (newer_id, newer)):
                take = session.get(AudioTake, take_id)
                take.generation_run_id = run.id
                take.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
            session.get(Artifact, artifact_id).content_hash = "a" * 64
            cutoff_id = cutoff.id
        options = {"generation_run_id": cutoff_id, "take_history": "selected",
                   "view": "compact", "segment_ids": [segment_id]}
        item = self.generation.list_segments(self.session_id, **options)["items"][0]
        self.assertEqual(chosen_id, item["history_take_id"])
        self.assertEqual(chosen_id, item["effective_take"]["id"])
        self.assertEqual("a" * 64, item["effective_take"]["content_hash"])
        self.assertEqual(newer_id, item["active_take_id"])
        with self.database.session() as session:
            session.get(Artifact, artifact_id).state = "deleted"
        missing = self.generation.list_segments(self.session_id, **options)["items"][0]
        self.assertEqual(chosen_id, missing["history_take_id"])
        self.assertIsNone(missing["effective_take"])
        self.assertFalse(missing["has_reusable_take"])

    def test_projection_schema_parity_and_actionable_invalid_fields(self):
        self.assertEqual(PROJECTABLE_SEGMENT_FIELDS, set(get_args(GenerationSegmentField)))
        base = f"/api/v1/sessions/{self.session_id}/generation-segments"
        invalid = self.client.get(base + "?fields=id,not_a_field")
        self.assertEqual(422, invalid.status_code)
        error = invalid.get_json()["error"]
        self.assertEqual(["not_a_field"], error["details"]["invalid_fields"])
        self.assertEqual(PROJECTABLE_SEGMENT_FIELDS, set(error["details"]["allowed_fields"]))
        selected = self.client.get(base + "?view=compact&segment_ids=" + self.segment_ids[1])
        self.assertEqual(200, selected.status_code)
        self.assertEqual([self.segment_ids[1]], [row["id"] for row in selected.get_json()["items"]])
        unknown = self.client.get(base + "?segment_ids=unknown")
        self.assertEqual(422, unknown.status_code)

    def test_recast_pin_selects_exact_before_and_after_take_without_active_fallback(self):
        pinned_before_id, _ = self.add_take(self.segment_ids[0], active=False)
        active_id, _ = self.add_take(self.segment_ids[0], active=True)
        other_active_id, _ = self.add_take(self.segment_ids[1], active=True)
        run_id = self.add_recast_run(
            {self.segment_ids[0]: pinned_before_id},
            {self.segment_ids[0]: active_id},
        )

        for side, expected_take_id in (
            ("before", pinned_before_id),
            ("after", active_id),
        ):
            with self.subTest(side=side):
                full = self.generation.list_segments(
                    self.session_id,
                    generation_run_id=run_id,
                    audio_version_side=side,
                )
                selected = self.generation.list_segments(
                    self.session_id,
                    generation_run_id=run_id,
                    audio_version_side=side,
                    take_history="selected",
                )
                first = selected["items"][0]
                self.assertEqual([expected_take_id], [take["id"] for take in first["takes"]])
                self.assertEqual(expected_take_id, first["history_take_id"])
                self.assertEqual(active_id, first["active_take_id"])
                self.assertEqual(
                    full["items"][0]["audio_reuse_reason"],
                    first["audio_reuse_reason"],
                )
                self.assertEqual(
                    full["items"][0]["has_reusable_take"],
                    first["has_reusable_take"],
                )

                missing_pin = selected["items"][1]
                self.assertEqual([], missing_pin["takes"])
                self.assertIsNone(missing_pin["history_take_id"])
                self.assertEqual(other_active_id, missing_pin["active_take_id"])
                self.assertFalse(missing_pin["take_history_loaded"])
                self.assertEqual(
                    full["items"][1]["audio_reuse_reason"],
                    missing_pin["audio_reuse_reason"],
                )

    def test_selected_mode_hydrates_only_page_selection_and_its_artifact(self):
        selected_id, selected_artifact_id = self.add_take(
            self.segment_ids[0], active=True
        )
        _, history_artifact_id = self.add_take(
            self.segment_ids[0], active=False, metadata_size=12000
        )
        _, unrelated_artifact_id = self.add_take(
            self.segment_ids[1], active=True, metadata_size=16000
        )
        statements: list[tuple[str, object]] = []

        def record_sql(_connection, _cursor, statement, parameters, _context, _many):
            normalized = statement.lower()
            if "from audio_takes" in normalized or "from artifacts" in normalized:
                statements.append((normalized, parameters))

        event.listen(self.database.engine, "before_cursor_execute", record_sql)
        try:
            page = self.generation.list_segments(
                self.session_id,
                limit=1,
                fields=["takes"],
                take_history="selected",
            )
        finally:
            event.remove(self.database.engine, "before_cursor_execute", record_sql)

        item = page["items"][0]
        self.assertEqual(self.segment_ids[0], item["id"])
        self.assertEqual([selected_id], [take["id"] for take in item["takes"]])
        take_queries = [sql for sql, _ in statements if "from audio_takes" in sql]
        thin_queries = [
            sql
            for sql in take_queries
            if "audio_takes.generation_segment_id in" in sql
        ]
        hydrated_queries = [
            (sql, params)
            for sql, params in statements
            if "from audio_takes" in sql and "where audio_takes.id in" in sql
        ]
        self.assertEqual(1, len(thin_queries), take_queries)
        self.assertEqual(
            [
                "audio_takes.id",
                "audio_takes.generation_segment_id",
                "audio_takes.is_active",
                "audio_takes.status",
                "audio_takes.artifact_id",
            ],
            [
                column.strip()
                for column in thin_queries[0]
                .split("from audio_takes", maxsplit=1)[0]
                .removeprefix("select ")
                .split(",")
            ],
        )
        where_clause = thin_queries[0].split("where", maxsplit=1)[1]
        self.assertNotIn("audio_takes.is_active =", where_clause)
        self.assertEqual(1, len(hydrated_queries), take_queries)
        self.assertIn(selected_id, repr(hydrated_queries[0][1]))
        self.assertNotIn(unrelated_artifact_id, repr(hydrated_queries[0][1]))
        artifact_queries = [
            (sql, params)
            for sql, params in statements
            if "from artifacts" in sql and "where artifacts.id in" in sql
        ]
        self.assertEqual(1, len(artifact_queries), artifact_queries)
        artifact_columns = artifact_queries[0][0].split(
            "from artifacts", maxsplit=1
        )[0]
        self.assertIn("artifacts.id", artifact_columns)
        self.assertIn("artifacts.state", artifact_columns)
        self.assertIn("artifacts.metadata_json", artifact_columns)
        self.assertIn(selected_artifact_id, repr(artifact_queries[0][1]))
        self.assertNotIn(unrelated_artifact_id, repr(artifact_queries[0][1]))
        self.assertNotIn(history_artifact_id, repr(artifact_queries[0][1]))

    def test_invalid_history_is_rejected_before_settings_lookup_and_openapi_documents_option(self):
        with patch.object(WorkspaceSettingsService, "resolve") as resolve:
            response = self.client.get(
                f"/api/v1/sessions/{self.session_id}/generation-segments",
                query_string={"take_history": "everything"},
            )
        self.assertEqual(422, response.status_code, response.get_json())
        resolve.assert_not_called()

        parameters = build_openapi_document()["paths"][
            "/api/v1/sessions/{sessionId}/generation-segments"
        ]["get"]["parameters"]
        take_history = next(
            item for item in parameters if item["name"] == "take_history"
        )
        self.assertEqual(["selected", "full"], take_history["schema"]["enum"])
        self.assertEqual("full", take_history["schema"]["default"])


if __name__ == "__main__":
    unittest.main()
