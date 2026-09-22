"""Regression coverage for safe missing-only generation (issue 117)."""

import tempfile
import threading
import unittest
import uuid
import wave
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import func, select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.generation_audio_identity import IDENTITY_KEY
from pandrator.web.models import (
    Artifact,
    AudioTake,
    GenerationRun,
    GenerationSegment,
    Job,
    utcnow,
)
from pandrator.web.tts_providers import TtsCapabilities
from pandrator.web.workspace import RevisionConflict


class GenerationMissingOnlyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=bootstrap,
        )
        self.client = self.app.test_client()
        self.headers = {
            "X-CSRF-Token": self.client.post(
                "/api/v1/auth/bootstrap", json={"token": token}
            ).get_json()["csrf_token"]
        }
        self.services = self.app.extensions["pandrator"]
        self.database = self.services["database"]
        self.generation = self.services["generation"]
        self.handlers = self.services["workflow_handlers"]
        self.addCleanup(self.database.dispose)

    @staticmethod
    def _override(**tts):
        return {
            "tts": {
                "service": "openai",
                "model": "model-a",
                "voice": "voice-a",
                "language": "en",
                "temperature": 0.75,
                "speed": 1.0,
                "tts_batch_size": 1,
                **tts,
            }
        }

    def _create_case(self, texts=None, name="missing-only"):
        created = self.client.post(
            "/api/v1/sessions",
            json={
                "name": f"{name} {uuid.uuid4().hex[:8]}",
                "workflow_kind": "audiobook",
            },
            headers=self.headers,
        )
        self.assertEqual(201, created.status_code, created.get_json())
        session_id = created.get_json()["id"]
        plan = self.generation.create_plan(
            session_id,
            source_revision_id=None,
            settings={},
            segments=[
                {"text": text} for text in (texts or ["One", "Two", "Three", "Four"])
            ],
        )
        with self.database.session() as session:
            ids = list(
                session.scalars(
                    select(GenerationSegment.id)
                    .where(
                        GenerationSegment.plan_revision_id == plan["active_revision_id"]
                    )
                    .order_by(GenerationSegment.ordinal)
                )
            )
        return session_id, plan["active_revision_id"], ids

    def _resolved(self, session_id, override):
        snapshot, _ = self.services["workspace_settings"].resolve(
            session_id, run_override=override
        )
        return snapshot

    def _wave_path(self, label):
        directory = self.services["paths"].uploads / "generation-missing-only"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{label}-{uuid.uuid4().hex}.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\x00\x00" * 1600)
        return path

    def _seed_completed(
        self, session_id, segment_ids, override, *, include_identity=True
    ):
        from pandrator.web.generation_audio_identity import AudioIdentityContext

        snapshot = self._resolved(session_id, override)
        with self.database.session() as session:
            context = AudioIdentityContext(session, snapshot)
            expected = {
                segment.id: context.for_segment(segment)
                for segment in session.scalars(
                    select(GenerationSegment).where(
                        GenerationSegment.id.in_(segment_ids)
                    )
                )
            }
        take_ids = {}
        for segment_id in segment_ids:
            metadata = {IDENTITY_KEY: expected[segment_id]} if include_identity else {}
            artifact = self.services["artifacts"].register(
                self._wave_path(segment_id),
                kind="audio",
                role="generation_take",
                session_id=session_id,
                metadata=metadata,
            )
            with self.database.session() as session:
                segment = session.get(GenerationSegment, segment_id)
                segment.status = "completed"
                take = AudioTake(
                    generation_segment_id=segment_id,
                    artifact_id=artifact.id,
                    status="completed",
                    is_active=True,
                    duration_ms=100,
                )
                session.add(take)
                session.flush()
                take_ids[segment_id] = take.id
        return take_ids

    def _segment_revision(self, segment_id):
        with self.database.session() as session:
            return session.get(GenerationSegment, segment_id).revision

    def _job_payload(self, job_id):
        with self.database.session() as session:
            return dict(session.get(Job, job_id).payload_json or {})

    def _counts(self, session_id):
        with self.database.session() as session:
            runs = int(
                session.scalar(
                    select(func.count())
                    .select_from(GenerationRun)
                    .where(GenerationRun.session_id == session_id)
                )
                or 0
            )
            jobs = int(session.scalar(select(func.count()).select_from(Job)) or 0)
        return runs, jobs

    # --- selection semantics ---

    def test_many_completed_one_failed_generates_only_failed(self):
        session_id, _revision, segment_ids = self._create_case()
        self._seed_completed(session_id, segment_ids[:-1], self._override())
        with self.database.session() as session:
            failed = session.get(GenerationSegment, segment_ids[-1])
            failed.status = "failed"

        preview = self.client.post(
            f"/api/v1/sessions/{session_id}/generation-runs/preview",
            json={"missing_only": True, "run_override": self._override()},
            headers=self.headers,
        )
        self.assertEqual(200, preview.status_code, preview.get_json())
        body = preview.get_json()
        self.assertEqual("missing", body["mode"])
        self.assertEqual(4, body["total_count"])
        self.assertEqual(1, body["generate_count"])
        self.assertEqual(3, body["preserve_count"])
        self.assertEqual(0, body["replace_count"])
        self.assertEqual(1, body["missing_count"])
        self.assertEqual(1, body["reasons"]["missing_audio"])

        result = self.generation.start(
            session_id,
            run_override=self._override(),
            missing_only=True,
            expected_selection_hash=body["selection_hash"],
        )
        self.assertEqual(
            [segment_ids[-1]], self._job_payload(result["job_id"])["segment_ids"]
        )

    def test_edited_block_reports_replacement_not_missing(self):
        session_id, _revision, segment_ids = self._create_case(
            texts=["Keep", "Edit me"]
        )
        old_takes = self._seed_completed(session_id, segment_ids, self._override())
        self.generation.update_segment(
            segment_ids[1],
            self._segment_revision(segment_ids[1]),
            {"text": "Edited text"},
        )
        with self.database.session() as session:
            segment = session.get(GenerationSegment, segment_ids[1])
            take = session.get(AudioTake, old_takes[segment_ids[1]])
            self.assertEqual("stale", segment.status)
            self.assertEqual("stale", take.status)

        body = self.generation.preview_selection(
            session_id, run_override=self._override(), missing_only=True
        )
        self.assertEqual(1, body["generate_count"])
        self.assertEqual(1, body["preserve_count"])
        self.assertEqual(1, body["replace_count"])
        self.assertEqual(0, body["missing_count"])
        self.assertEqual(1, body["reasons"]["edited_or_failed"])
        self.assertEqual(0, body["reasons"]["missing_audio"])

        result = self.generation.start(
            session_id,
            run_override=self._override(),
            missing_only=True,
            expected_selection_hash=body["selection_hash"],
        )
        self.assertEqual(
            [segment_ids[1]], self._job_payload(result["job_id"])["segment_ids"]
        )

    def test_unknown_identity_preserved_by_missing_regenerated_by_stale(self):
        session_id, _revision, segment_ids = self._create_case(texts=["Legacy"])
        self._seed_completed(
            session_id, segment_ids, self._override(), include_identity=False
        )

        missing = self.generation.preview_selection(
            session_id, run_override=self._override(), missing_only=True
        )
        self.assertEqual(0, missing["generate_count"])
        self.assertEqual(1, missing["preserve_count"])
        self.assertIn("blocked_reason", missing)

        stale = self.generation.preview_selection(
            session_id, run_override=self._override(), stale_only=True
        )
        self.assertEqual(1, stale["generate_count"])
        self.assertEqual(1, stale["reasons"]["identity_unknown"])

    def test_settings_change_preserved_by_missing_regenerated_by_stale(self):
        session_id, _revision, segment_ids = self._create_case(texts=["Stable"])
        self._seed_completed(session_id, segment_ids, self._override())

        missing = self.generation.preview_selection(
            session_id, run_override=self._override(voice="voice-b"), missing_only=True
        )
        self.assertEqual(0, missing["generate_count"])

        stale = self.generation.preview_selection(
            session_id, run_override=self._override(voice="voice-b"), stale_only=True
        )
        self.assertEqual(1, stale["generate_count"])
        self.assertEqual(1, stale["reasons"]["settings_changed"])

    def test_all_mode_uses_requested_bucket(self):
        session_id, _revision, segment_ids = self._create_case(texts=["A", "B"])
        self._seed_completed(session_id, segment_ids, self._override())
        body = self.generation.preview_selection(
            session_id, run_override=self._override()
        )
        self.assertEqual("all", body["mode"])
        self.assertEqual(2, body["generate_count"])
        self.assertEqual(0, body["preserve_count"])
        self.assertEqual(2, body["reasons"]["requested"])
        self.assertEqual(0, body["reasons"]["identity_unknown"])

    def test_duplicate_active_rows_do_not_broaden_stale_selection(self):
        # A newer failed active take must not hide the older completed active
        # take: legacy semantics keep the row reusable.
        session_id, _revision, segment_ids = self._create_case(texts=["Dup", "Other"])
        takes = self._seed_completed(session_id, segment_ids[:1], self._override())
        with self.database.session() as session:
            old_take = session.get(AudioTake, takes[segment_ids[0]])
            old_take.created_at = utcnow() - timedelta(hours=1)
            newer = AudioTake(
                generation_segment_id=segment_ids[0],
                artifact_id=old_take.artifact_id,
                status="failed",
                is_active=True,
                duration_ms=50,
                created_at=utcnow(),
            )
            session.add(newer)

        stale = self.generation.preview_selection(
            session_id, run_override=self._override(), stale_only=True
        )
        self.assertEqual(1, stale["generate_count"])
        prepared = self.generation.prepare_start(
            session_id, run_override=self._override(), stale_only=True
        )
        self.assertEqual([segment_ids[1]], prepared["requested_segment_ids"])
        self.assertEqual(
            takes[segment_ids[0]], prepared["reusable_take_ids"][segment_ids[0]]
        )

    # --- preview endpoint behaviour ---

    def test_preview_has_no_side_effects(self):
        session_id, revision_id, segment_ids = self._create_case()
        self._seed_completed(session_id, segment_ids[:1], self._override())
        before = self._counts(session_id)
        response = self.client.post(
            f"/api/v1/sessions/{session_id}/generation-runs/preview",
            json={"missing_only": True, "run_override": self._override()},
            headers=self.headers,
        )
        self.assertEqual(200, response.status_code, response.get_json())
        body = response.get_json()
        self.assertEqual(revision_id, body["speech_plan_revision_id"])
        self.assertEqual(before, self._counts(session_id))
        with self.database.session() as session:
            from pandrator.web.models import GenerationPlan

            plan = session.scalar(
                select(GenerationPlan).where(GenerationPlan.session_id == session_id)
            )
            self.assertEqual(revision_id, plan.active_revision_id)

    def test_preview_rejects_invalid_combinations(self):
        session_id, _revision, segment_ids = self._create_case()
        for payload in (
            {"stale_only": True, "missing_only": True},
            {"missing_only": True, "segment_ids": segment_ids[:1]},
            {"missing_only": True, "operation": "rvc"},
            {"stale_only": True, "operation": "regenerate"},
            {"segment_ids": segment_ids[:1]},
        ):
            response = self.client.post(
                f"/api/v1/sessions/{session_id}/generation-runs/preview",
                json=payload,
                headers=self.headers,
            )
            self.assertEqual(409, response.status_code, payload)

    def test_start_rejects_invalid_missing_combinations(self):
        session_id, _revision, segment_ids = self._create_case()
        with self.assertRaises(ValueError):
            self.generation.prepare_start(
                session_id, stale_only=True, missing_only=True
            )
        with self.assertRaises(ValueError):
            self.generation.prepare_start(
                session_id, missing_only=True, segment_ids=segment_ids[:1]
            )
        with self.assertRaises(ValueError):
            self.generation.prepare_start(
                session_id, missing_only=True, operation="rvc"
            )

    # --- optimism guard ---

    def test_guarded_start_conflicts_after_take_mutation(self):
        session_id, _revision, segment_ids = self._create_case(texts=["A", "B"])
        body = self.generation.preview_selection(
            session_id, run_override=self._override()
        )
        self._seed_completed(session_id, segment_ids[:1], self._override())
        with self.assertRaises(RevisionConflict):
            self.generation.prepare_start(
                session_id,
                run_override=self._override(),
                expected_selection_hash=body["selection_hash"],
            )
        response = self.client.post(
            f"/api/v1/sessions/{session_id}/generation-runs",
            json={
                "run_override": self._override(),
                "expected_selection_hash": body["selection_hash"],
            },
            headers=self.headers,
        )
        self.assertEqual(409, response.status_code, response.get_json())

    def test_guarded_start_conflicts_after_settings_change(self):
        session_id, _revision, segment_ids = self._create_case(texts=["A"])
        self._seed_completed(session_id, segment_ids, self._override())
        body = self.generation.preview_selection(
            session_id, run_override=self._override(), stale_only=True
        )
        with self.assertRaises(RevisionConflict):
            self.generation.prepare_start(
                session_id,
                run_override=self._override(voice="voice-b"),
                stale_only=True,
                expected_selection_hash=body["selection_hash"],
            )

    def test_all_mode_guarded_start_conflicts_on_mutation(self):
        session_id, _revision, segment_ids = self._create_case(texts=["A", "B"])
        body = self.generation.preview_selection(
            session_id, run_override=self._override()
        )
        self._seed_completed(session_id, segment_ids[:1], self._override())
        with self.assertRaises(RevisionConflict):
            self.generation.prepare_start(
                session_id,
                run_override=self._override(),
                expected_selection_hash=body["selection_hash"],
            )

    def test_second_start_with_same_hash_conflicts_once_queued(self):
        session_id, _revision, _segment_ids = self._create_case(texts=["A", "B"])
        body = self.generation.preview_selection(
            session_id, run_override=self._override(), missing_only=True
        )
        first = self.generation.start(
            session_id,
            run_override=self._override(),
            missing_only=True,
            expected_selection_hash=body["selection_hash"],
        )
        self.assertIn("id", first)
        with self.assertRaises(RevisionConflict):
            self.generation.prepare_start(
                session_id,
                run_override=self._override(),
                missing_only=True,
                expected_selection_hash=body["selection_hash"],
            )

    def test_identity_context_stays_lazy_outside_stale_preview(self):
        session_id, _revision, segment_ids = self._create_case(texts=["A"])
        self._seed_completed(session_id, segment_ids, self._override())
        with patch(
            "pandrator.web.generation_audio_identity.AudioIdentityContext",
            side_effect=AssertionError("must stay lazy"),
        ):
            missing = self.generation.preview_selection(
                session_id, run_override=self._override(), missing_only=True
            )
            full = self.generation.preview_selection(
                session_id, run_override=self._override()
            )
        self.assertEqual(0, missing["generate_count"])
        self.assertEqual(1, full["generate_count"])
        stale = self.generation.preview_selection(
            session_id, run_override=self._override(), stale_only=True
        )
        self.assertEqual(0, stale["generate_count"])

    def test_guarded_full_with_no_rows_rejects_start(self):
        session_id, _revision, segment_ids = self._create_case(texts=["A", "B"])
        revisions = {}
        with self.database.session() as session:
            for segment_id in segment_ids:
                revisions[segment_id] = session.get(
                    GenerationSegment, segment_id
                ).revision
        self.generation.update_segments(
            session_id,
            [
                {
                    "id": segment_id,
                    "revision": revisions[segment_id],
                    "changes": {"removed": True},
                }
                for segment_id in segment_ids
            ],
        )
        body = self.generation.preview_selection(
            session_id, run_override=self._override()
        )
        self.assertEqual(0, body["total_count"])
        self.assertEqual(0, body["generate_count"])
        self.assertIn("blocked_reason", body)
        with self.assertRaises(ValueError):
            self.generation.prepare_start(
                session_id,
                run_override=self._override(),
                expected_selection_hash=body["selection_hash"],
            )
        response = self.client.post(
            f"/api/v1/sessions/{session_id}/generation-runs",
            json={
                "run_override": self._override(),
                "expected_selection_hash": body["selection_hash"],
            },
            headers=self.headers,
        )
        self.assertEqual(409, response.status_code, response.get_json())

    def test_targeted_start_and_paused_resume_unchanged(self):
        session_id, _revision, segment_ids = self._create_case(texts=["A", "B"])
        full = self.generation.start(session_id, run_override=self._override())
        with self.database.session() as session:
            run = session.get(GenerationRun, full["id"])
            run.status = "paused"
            run.pause_requested = False
        resumed = self.generation.resume(full["id"])
        self.assertEqual("queued", resumed["status"])
        with self.database.session() as session:
            payload = dict(session.get(Job, resumed["job_id"]).payload_json or {})
        self.assertEqual("resume", payload["operation"])

        targeted = self.generation.start(
            session_id,
            run_override=self._override(),
            segment_ids=segment_ids[:1],
            generation_run_id=full["id"],
            operation="regenerate",
        )
        self.assertEqual(
            segment_ids[:1], self._job_payload(targeted["job_id"])["segment_ids"]
        )

    # --- central acceptance: worker executes only the edited block ---

    def test_worker_synthesizes_only_edited_block_and_keeps_history(self):
        from pydub import AudioSegment

        texts = ["Alpha", "Bravo", "Charlie", "Delta"]
        session_id, _revision, segment_ids = self._create_case(texts=texts)
        base = self._override()
        # Legacy unknown-identity recording plus mismatched-identity recordings.
        self._seed_completed(session_id, segment_ids[:1], base, include_identity=False)
        self._seed_completed(session_id, segment_ids[1:3], base, include_identity=True)
        self._seed_completed(session_id, segment_ids[3:], base, include_identity=True)
        with self.database.session() as session:
            before_artifacts = {
                take.generation_segment_id: take.artifact_id
                for take in session.scalars(
                    select(AudioTake).where(
                        AudioTake.generation_segment_id.in_(segment_ids),
                        AudioTake.is_active.is_(True),
                    )
                )
            }
            before_metadata = {
                artifact_id: dict(
                    session.get(Artifact, artifact_id).metadata_json or {}
                )
                for artifact_id in before_artifacts.values()
            }
            old_take_ids = {
                take.generation_segment_id: take.id
                for take in session.scalars(
                    select(AudioTake).where(
                        AudioTake.generation_segment_id.in_(segment_ids),
                        AudioTake.is_active.is_(True),
                    )
                )
            }
        # New settings mismatch every stored identity; the edit marks one row stale.
        changed = self._override(voice="voice-b")
        self.generation.update_segment(
            segment_ids[2],
            self._segment_revision(segment_ids[2]),
            {"text": "Charlie edited"},
        )

        preview = self.generation.preview_selection(
            session_id, run_override=changed, missing_only=True
        )
        self.assertEqual(
            [segment_ids[2]],
            self.generation.prepare_start(
                session_id,
                run_override=changed,
                missing_only=True,
                expected_selection_hash=preview["selection_hash"],
            )["requested_segment_ids"],
        )
        started = self.generation.start(
            session_id,
            run_override=changed,
            missing_only=True,
            expected_selection_hash=preview["selection_hash"],
        )
        payload = self._job_payload(started["job_id"])
        self.assertEqual([segment_ids[2]], payload["segment_ids"])
        self.assertEqual("generate", payload["operation"])

        calls = []
        capabilities = TtsCapabilities(batch_synthesis=False, streaming_batch=False)

        def synthesize(text, _settings, **_kwargs):
            calls.append(text)
            return AudioSegment.silent(duration=20)

        with (
            patch.object(
                self.handlers.tts_providers, "synthesize", side_effect=synthesize
            ),
            patch.object(
                self.handlers.tts_providers,
                "synthesis_capabilities",
                return_value=capabilities,
            ),
        ):
            finished = self.handlers.run_generation(
                {
                    "generation_run_id": started["id"],
                    "segment_ids": payload["segment_ids"],
                    "operation": payload["operation"],
                },
                lambda *_: None,
                threading.Event(),
            )
        self.assertEqual("completed", finished["status"])
        self.assertEqual(["Charlie edited"], calls)

        with self.database.session() as session:
            run_takes = list(
                session.scalars(
                    select(AudioTake).where(
                        AudioTake.generation_run_id == started["id"]
                    )
                )
            )
            self.assertEqual(
                set(segment_ids),
                {take.generation_segment_id for take in run_takes},
            )
            for take in run_takes:
                self.assertEqual("completed", take.status)
                self.assertTrue(take.is_active)
            by_segment = {take.generation_segment_id: take for take in run_takes}
            # Preserved rows keep their historical artifact and parent linkage.
            for preserved_id in (segment_ids[0], segment_ids[1], segment_ids[3]):
                self.assertEqual(
                    before_artifacts[preserved_id], by_segment[preserved_id].artifact_id
                )
                self.assertEqual(
                    old_take_ids[preserved_id], by_segment[preserved_id].parent_take_id
                )
            # The edited row got fresh audio.
            self.assertNotEqual(
                before_artifacts[segment_ids[2]],
                by_segment[segment_ids[2]].artifact_id,
            )
            # Old takes and artifacts survive untouched.
            for take_id in old_take_ids.values():
                self.assertIsNotNone(session.get(AudioTake, take_id))
            for artifact_id, metadata in before_metadata.items():
                artifact = session.get(Artifact, artifact_id)
                self.assertIsNotNone(artifact)
                self.assertNotEqual("deleted", artifact.state)
                if IDENTITY_KEY in metadata:
                    self.assertEqual(
                        metadata[IDENTITY_KEY],
                        (artifact.metadata_json or {}).get(IDENTITY_KEY),
                    )
            edited = session.get(GenerationSegment, segment_ids[2])
            self.assertEqual("completed", edited.status)


if __name__ == "__main__":
    unittest.main()
