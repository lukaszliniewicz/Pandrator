"""Historical generation settings can seed a new synthesis run safely."""

from __future__ import annotations

import copy
import tempfile
import unittest
import uuid

from sqlalchemy import func, select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import GenerationRun, GenerationSegment
from pandrator.web.workspace import RevisionConflict, stable_hash


class GenerationSettingsSourceTests(unittest.TestCase):
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
        bootstrap_result = self.client.post(
            "/api/v1/auth/bootstrap", json={"token": token}
        )
        self.headers = {"X-CSRF-Token": bootstrap_result.get_json()["csrf_token"]}
        self.session_id, self.revision_id, self.segment_ids = self._create_plan()

    def _create_session(self, name: str) -> str:
        response = self.client.post(
            "/api/v1/sessions",
            json={"name": f"{name} {uuid.uuid4().hex[:8]}", "workflow_kind": "voiceover"},
            headers=self.headers,
        )
        self.assertEqual(201, response.status_code, response.get_json())
        return str(response.get_json()["id"])

    def _create_plan(self, session_id: str | None = None):
        target_session_id = session_id or self._create_session("Settings source")
        plan = self.generation.create_plan(
            target_session_id,
            source_revision_id=None,
            settings={},
            segments=[{"text": "Original spoken text."}, {"text": "Another line."}],
        )
        with self.database.session() as session:
            segment_ids = list(
                session.scalars(
                    select(GenerationSegment.id)
                    .where(
                        GenerationSegment.plan_revision_id
                        == plan["active_revision_id"]
                    )
                    .order_by(GenerationSegment.ordinal)
                )
            )
        return target_session_id, plan["active_revision_id"], segment_ids

    @staticmethod
    def _historical_snapshot(revision_id: str, segment_id: str) -> dict:
        return {
            "tts": {
                "service": "openai",
                "model": "historical-model",
                "voice": "historical-voice",
                "language": "en",
                "speed": 1.37,
                "tts_batch_size": 1,
            },
            "output": {"format": "flac", "bitrate": 192},
            "speech_plan_revision_id": revision_id,
            "speech_plan_frozen": True,
            "speech_plan_signature": "old-plan-signature",
            "speech_boundaries": {segment_id: {"silence_after_ms": 999}},
            "generation_audio_identities": {segment_id: {"fingerprint": "old"}},
            "generation_selection_guards": {segment_id: {"revision": 1}},
            "generation_request_segment_ids": [segment_id],
            "selected_segment_override": {"tts": {"voice": "old-selected-voice"}},
            "interrupted_generation_run_id": "old-interrupted-run",
            "stale_only": True,
            "missing_only": True,
        }

    def _create_run(
        self,
        session_id: str,
        revision_id: str,
        snapshot: dict | None = None,
        *,
        operation: str = "generate",
    ) -> str:
        value = copy.deepcopy(snapshot or self._historical_snapshot(revision_id, "old-segment"))
        with self.database.session() as session:
            sequence = int(
                session.scalar(
                    select(func.max(GenerationRun.sequence_number)).where(
                        GenerationRun.session_id == session_id
                    )
                )
                or 0
            ) + 1
            run = GenerationRun(
                session_id=session_id,
                plan_revision_id=revision_id,
                sequence_number=sequence,
                operation=operation,
                status="completed",
                settings_snapshot_json=value,
                settings_hash=stable_hash(value),
            )
            session.add(run)
            session.flush()
            return run.id

    def _edit_segment(self, session_id: str, segment_id: str, text: str) -> dict:
        current = next(
            item
            for item in self.generation.list_segments(session_id)["items"]
            if item["id"] == segment_id
        )
        response = self.client.patch(
            f"/api/v1/generation-segments/{segment_id}",
            json={"text": text},
            headers={**self.headers, "If-Match": f'"{current["revision"]}"'},
        )
        self.assertEqual(200, response.status_code, response.get_json())
        return response.get_json()

    def _run(self, run_id: str) -> GenerationRun:
        with self.database.session() as session:
            result = session.get(GenerationRun, run_id)
            self.assertIsNotNone(result)
            return result

    def _run_snapshot(self, run_id: str) -> dict:
        return copy.deepcopy(self._run(run_id).settings_snapshot_json)

    def test_edited_descendant_uses_old_settings_without_inheriting_run_ownership(self):
        source_snapshot = self._historical_snapshot(self.revision_id, self.segment_ids[0])
        source_id = self._create_run(self.session_id, self.revision_id, source_snapshot)
        source_before = self._run_snapshot(source_id)
        edited = self._edit_segment(
            self.session_id, self.segment_ids[0], "Edited spoken text."
        )
        target_revision_id = edited["plan_revision_id"]
        self.assertNotEqual(self.revision_id, target_revision_id)

        started = self.generation.start(
            self.session_id,
            segment_ids=[edited["id"]],
            operation="regenerate",
            settings_source_run_id=source_id,
        )
        queued = self._run(started["id"])
        snapshot = queued.settings_snapshot_json
        self.assertEqual(target_revision_id, queued.plan_revision_id)
        self.assertNotEqual(source_id, queued.source_generation_run_id)
        self.assertNotEqual(source_id, queued.output_generation_run_id)
        self.assertEqual("openai", snapshot["tts"]["service"])
        self.assertEqual("historical-model", snapshot["tts"]["model"])
        self.assertEqual("historical-voice", snapshot["tts"]["voice"])
        self.assertEqual(1.37, snapshot["tts"]["speed"])
        self.assertEqual({"format": "flac", "bitrate": 192}, snapshot["output"])
        self.assertEqual(target_revision_id, snapshot["speech_plan_revision_id"])
        self.assertEqual([edited["id"]], snapshot["generation_request_segment_ids"])
        self.assertIn(edited["id"], snapshot["generation_audio_identities"])
        self.assertNotIn(self.segment_ids[0], snapshot["generation_audio_identities"])
        self.assertNotIn(self.segment_ids[0], snapshot["generation_selection_guards"])
        self.assertNotIn("old-plan-signature", snapshot.get("speech_plan_signature", ""))
        self.assertNotIn("speech_boundaries", snapshot)
        self.assertNotIn("selected_segment_override", snapshot)
        self.assertNotIn("interrupted_generation_run_id", snapshot)
        self.assertNotIn("stale_only", snapshot)
        self.assertNotIn("missing_only", snapshot)
        self.assertEqual(source_before, self._run_snapshot(source_id))

    def test_target_run_ownership_remains_independent_of_settings_source(self):
        source_id = self._create_run(
            self.session_id,
            self.revision_id,
            self._historical_snapshot(self.revision_id, self.segment_ids[0]),
        )
        edited = self._edit_segment(
            self.session_id, self.segment_ids[0], "Edited spoken text."
        )
        owner_id = self._create_run(
            self.session_id,
            edited["plan_revision_id"],
            {"tts": {"service": "openai", "model": "target-model", "voice": "target-voice"}},
        )

        started = self.generation.start(
            self.session_id,
            segment_ids=[edited["id"]],
            generation_run_id=owner_id,
            settings_source_run_id=source_id,
            operation="regenerate",
        )
        queued = self._run(started["id"])
        self.assertEqual(owner_id, queued.source_generation_run_id)
        self.assertEqual(owner_id, queued.output_generation_run_id)
        self.assertEqual("historical-model", queued.settings_snapshot_json["tts"]["model"])

    def test_preview_hash_and_confirmed_snapshot_share_inherited_settings_for_each_mode(self):
        source_snapshot = self._historical_snapshot(self.revision_id, self.segment_ids[0])
        source_id = self._create_run(self.session_id, self.revision_id, source_snapshot)
        source_before = self._run_snapshot(source_id)
        run_override = {
            "tts": {"model": "explicit-model", "voice": "explicit-voice", "speed": 1.43}
        }
        for mode_flags in ({}, {"missing_only": True}, {"stale_only": True}):
            with self.subTest(mode_flags=mode_flags):
                payload = {
                    "settings_source_run_id": source_id,
                    "run_override": run_override,
                    **mode_flags,
                }
                preview_response = self.client.post(
                    f"/api/v1/sessions/{self.session_id}/generation-runs/preview",
                    json=payload,
                    headers=self.headers,
                )
                self.assertEqual(200, preview_response.status_code, preview_response.get_json())
                preview = preview_response.get_json()
                self.assertEqual("openai", preview["settings_summary"]["service"])
                self.assertEqual("explicit-model", preview["settings_summary"]["model"])
                self.assertEqual("explicit-voice", preview["settings_summary"]["voice"])

                start_response = self.client.post(
                    f"/api/v1/sessions/{self.session_id}/generation-runs",
                    json={
                        **payload,
                        "expected_selection_hash": preview["selection_hash"],
                    },
                    headers=self.headers,
                )
                self.assertEqual(202, start_response.status_code, start_response.get_json())
                snapshot = self._run_snapshot(start_response.get_json()["id"])
                self.assertEqual("openai", snapshot["tts"]["service"])
                self.assertEqual("explicit-model", snapshot["tts"]["model"])
                self.assertEqual("explicit-voice", snapshot["tts"]["voice"])
                self.assertEqual(1.43, snapshot["tts"]["speed"])
                self.assertNotIn("old-plan-signature", snapshot.get("speech_plan_signature", ""))
                self.assertEqual(
                    set(self.segment_ids), set(snapshot["generation_audio_identities"])
                )
                self.assertNotEqual(
                    {"fingerprint": "old"},
                    snapshot["generation_audio_identities"][self.segment_ids[0]],
                )
        self.assertEqual(source_before, self._run_snapshot(source_id))

    def test_historical_full_plan_binding_requires_matching_settings_source(self):
        source_id = self._create_run(
            self.session_id,
            self.revision_id,
            self._historical_snapshot(self.revision_id, self.segment_ids[0]),
        )
        first_edit = self._edit_segment(
            self.session_id, self.segment_ids[0], "Edited descendant text."
        )
        active_revision_id = first_edit["plan_revision_id"]
        self.assertNotEqual(self.revision_id, active_revision_id)

        historical = self.generation.start(
            self.session_id,
            settings_source_run_id=source_id,
            speech_plan_revision_id=self.revision_id,
        )
        self.assertEqual(self.revision_id, self._run(historical["id"]).plan_revision_id)
        with self.assertRaises(RevisionConflict):
            self.generation.prepare_start(
                self.session_id,
                settings_source_run_id=source_id,
                speech_plan_revision_id=f"stale-{uuid.uuid4().hex}",
            )

    def test_source_must_be_same_session_non_rvc_and_present(self):
        foreign_session_id, foreign_revision_id, foreign_segment_ids = self._create_plan()
        foreign_id = self._create_run(
            foreign_session_id,
            foreign_revision_id,
            self._historical_snapshot(foreign_revision_id, foreign_segment_ids[0]),
        )
        rvc_id = self._create_run(
            self.session_id,
            self.revision_id,
            self._historical_snapshot(self.revision_id, self.segment_ids[0]),
            operation="rvc",
        )
        for source_id in (foreign_id, rvc_id, f"missing-{uuid.uuid4().hex}"):
            with self.subTest(source_id=source_id):
                with self.assertRaises(ValueError):
                    self.generation.preview_selection(
                        self.session_id,
                        settings_source_run_id=source_id,
                        missing_only=True,
                    )
                with self.assertRaises(ValueError):
                    self.generation.prepare_start(
                        self.session_id, settings_source_run_id=source_id
                    )

    def test_source_snapshot_change_or_deletion_after_prepare_fails_closed(self):
        changed_id = self._create_run(
            self.session_id,
            self.revision_id,
            self._historical_snapshot(self.revision_id, self.segment_ids[0]),
        )
        deleted_id = self._create_run(
            self.session_id,
            self.revision_id,
            self._historical_snapshot(self.revision_id, self.segment_ids[1]),
        )
        for source_id, action in ((changed_id, "change"), (deleted_id, "delete")):
            with self.subTest(action=action):
                preview = self.generation.preview_selection(
                    self.session_id,
                    settings_source_run_id=source_id,
                    missing_only=True,
                )
                prepared = self.generation.prepare_start(
                    self.session_id,
                    settings_source_run_id=source_id,
                    missing_only=True,
                    expected_selection_hash=preview["selection_hash"],
                )
                before_count = self._run_count()
                with self.database.session() as session:
                    source = session.get(GenerationRun, source_id)
                    if action == "change":
                        changed = copy.deepcopy(source.settings_snapshot_json)
                        changed["tts"]["voice"] = "changed-after-preview"
                        source.settings_snapshot_json = changed
                    else:
                        session.delete(source)
                with self.assertRaises(RevisionConflict):
                    with self.database.immediate_session() as session:
                        self.generation.start_in_session(
                            session, self.session_id, prepared=prepared
                        )
                expected_count = before_count - 1 if action == "delete" else before_count
                self.assertEqual(expected_count, self._run_count())

    def _run_count(self) -> int:
        with self.database.session() as session:
            return int(
                session.scalar(
                    select(func.count()).select_from(GenerationRun).where(
                        GenerationRun.session_id == self.session_id
                    )
                )
                or 0
            )


if __name__ == "__main__":
    unittest.main()
