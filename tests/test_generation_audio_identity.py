"""Regression coverage for generation-audio identity and fail-closed reuse."""

import json
import tempfile
import threading
import unittest
import uuid
import wave
from datetime import timedelta
from unittest.mock import patch

from pydub import AudioSegment
from sqlalchemy import select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.generation_audio_identity import (
    IDENTITY_KEY,
    AudioIdentityContext,
)
from pandrator.web.models import (
    Artifact,
    AudioTake,
    GenerationRun,
    GenerationSegment,
    Job,
    Voice,
    VoiceSample,
    utcnow,
)
from pandrator.web.tts_providers import TtsCapabilities


class GenerationAudioIdentityTests(unittest.TestCase):
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

    def _create_case(self, segments=None, *, name="Audio identity"):
        created = self.client.post(
            "/api/v1/sessions",
            json={"name": f"{name} {uuid.uuid4().hex[:8]}", "workflow_kind": "audiobook"},
            headers=self.headers,
        )
        self.assertEqual(201, created.status_code, created.get_json())
        session_id = created.get_json()["id"]
        plan = self.generation.create_plan(
            session_id,
            source_revision_id=None,
            settings={},
            segments=segments
            or [
                {"text": "One"},
                {"text": "Two"},
                {"text": "Three"},
            ],
        )
        with self.database.session() as session:
            ids = list(
                session.scalars(
                    select(GenerationSegment.id)
                    .where(GenerationSegment.plan_revision_id == plan["active_revision_id"])
                    .order_by(GenerationSegment.ordinal)
                )
            )
        return session_id, plan["active_revision_id"], ids

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

    def _resolved(self, session_id, override):
        snapshot, _ = self.services["workspace_settings"].resolve(
            session_id, run_override=override
        )
        return snapshot

    def _wave_path(self, label):
        directory = self.services["paths"].uploads / "generation-audio-identity"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{label}-{uuid.uuid4().hex}.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\x00\x00" * 1600)
        return path

    def _seed_takes(
        self,
        session_id,
        segment_ids,
        override,
        *,
        include_identity=True,
    ):
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
            metadata = (
                {IDENTITY_KEY: expected[segment_id]} if include_identity else {}
            )
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

    def _start_stale(self, session_id, override):
        return self.generation.start(
            session_id,
            run_override=override,
            stale_only=True,
        )

    def _job_segment_ids(self, job_id):
        with self.database.session() as session:
            return session.get(Job, job_id).payload_json["segment_ids"]

    def test_completed_take_is_copied_while_missing_block_is_requested(self):
        session_id, _revision_id, segment_ids = self._create_case()
        take_ids = self._seed_takes(session_id, segment_ids[:1], self._override())

        result = self._start_stale(session_id, self._override())

        self.assertEqual(segment_ids[1:], self._job_segment_ids(result["job_id"]))
        with self.database.session() as session:
            copied = session.scalar(
                select(AudioTake).where(
                    AudioTake.generation_run_id == result["id"],
                    AudioTake.generation_segment_id == segment_ids[0],
                )
            )
            self.assertIsNotNone(copied)
            self.assertEqual(take_ids[segment_ids[0]], copied.parent_take_id)

    def test_voice_change_makes_all_completed_takes_stale(self):
        session_id, _revision_id, segment_ids = self._create_case()
        self._seed_takes(session_id, segment_ids, self._override())

        changed = self._override(voice="voice-b")
        result = self._start_stale(session_id, changed)

        self.assertCountEqual(segment_ids, self._job_segment_ids(result["job_id"]))
        with self.database.session() as session:
            self.assertEqual(
                0,
                session.scalar(
                    select(AudioTake.id).where(
                        AudioTake.generation_run_id == result["id"]
                    ).limit(1)
                )
                is not None,
            )

    def test_audio_setting_changes_request_every_affected_completed_block(self):
        for label, changed in (
            ("model", {"model": "model-b"}),
            ("temperature", {"temperature": 0.9}),
            ("speed", {"speed": 1.2}),
        ):
            with self.subTest(label=label):
                session_id, _revision_id, segment_ids = self._create_case(name=label)
                base = self._override()
                self._seed_takes(session_id, segment_ids, base)
                result = self._start_stale(session_id, self._override(**changed))
                self.assertCountEqual(
                    segment_ids, self._job_segment_ids(result["job_id"])
                )

    def test_language_change_only_stales_blocks_following_session_language(self):
        session_id, _revision_id, segment_ids = self._create_case(
            [
                {"text": "Inherited language"},
                {"text": "Explicit English", "language": "en"},
                {"text": "Explicit Polish", "language": "pl"},
            ]
        )
        base = self._override(language="en")
        self._seed_takes(session_id, segment_ids, base)

        result = self._start_stale(session_id, self._override(language="pl"))

        self.assertEqual([segment_ids[0]], self._job_segment_ids(result["job_id"]))
        with self.database.session() as session:
            copied = list(
                session.scalars(
                    select(AudioTake).where(
                        AudioTake.generation_run_id == result["id"]
                    )
                )
            )
            self.assertEqual(
                {segment_ids[1], segment_ids[2]},
                {take.generation_segment_id for take in copied},
            )

    def test_per_segment_voice_override_keeps_effective_voice_reusable(self):
        session_id, _revision_id, segment_ids = self._create_case(
            [
                {"text": "Pinned voice", "voice": "fixed-voice"},
                {"text": "Missing"},
            ]
        )
        self._seed_takes(session_id, segment_ids[:1], self._override(voice="voice-a"))

        result = self._start_stale(session_id, self._override(voice="voice-b"))

        self.assertEqual([segment_ids[1]], self._job_segment_ids(result["job_id"]))
        with self.database.session() as session:
            copied = session.scalar(
                select(AudioTake).where(
                    AudioTake.generation_run_id == result["id"],
                    AudioTake.generation_segment_id == segment_ids[0],
                )
            )
            self.assertIsNotNone(copied)

    def test_topology_split_regenerates_children_and_reuses_untouched_block(self):
        session_id, revision_id, segment_ids = self._create_case(
            [{"text": "Split this block"}, {"text": "Untouched"}]
        )
        self._seed_takes(session_id, segment_ids, self._override())
        response = self.client.post(
            f"/api/v1/sessions/{session_id}/generation-plan/topology/batch",
            json={
                "expected_revision_id": revision_id,
                "operations": [
                    {
                        "action": "split",
                        "segment_id": segment_ids[0],
                        "boundary": {"after_text": "Split"},
                    }
                ],
            },
            headers={
                **self.headers,
                "If-Match": f'"{revision_id}"',
                "Idempotency-Key": uuid.uuid4().hex,
            },
        )
        self.assertEqual(201, response.status_code, response.get_json())
        split = response.get_json()
        children = split["lineage"][segment_ids[0]]
        untouched_id = split["lineage"][segment_ids[1]][0]

        result = self._start_stale(session_id, self._override())

        self.assertCountEqual(children, self._job_segment_ids(result["job_id"]))
        with self.database.session() as session:
            copied = session.scalar(
                select(AudioTake).where(
                    AudioTake.generation_run_id == result["id"],
                    AudioTake.generation_segment_id == untouched_id,
                )
            )
            self.assertIsNotNone(copied)

    def test_replaced_managed_voice_sample_hash_prevents_reuse(self):
        session_id, _revision_id, segment_ids = self._create_case(
            [{"text": "Voice sample", "voice": "Display voice"}]
        )
        sample_one_path = self._wave_path("voice-sample-one")
        sample_one = self.services["artifacts"].register(
            sample_one_path,
            kind="audio",
            role="voice_sample",
            session_id=session_id,
        )
        with self.database.session() as session:
            voice = Voice(
                name="Display voice",
                metadata_json={
                    "providers": {"provider-a": {"voice_id": "provider-voice"}}
                },
            )
            session.add(voice)
            session.flush()
            sample = VoiceSample(
                voice_id=voice.id,
                artifact_id=sample_one.id,
                transcript="sample",
                transcript_reviewed=True,
            )
            session.add(sample)
            segment = session.get(GenerationSegment, segment_ids[0])
            segment.voice_id = voice.id
        base = self._override(voice="Display voice")
        self._seed_takes(session_id, segment_ids, base)
        sample_two = self.services["artifacts"].register(
            self._wave_path("voice-sample-two"),
            kind="audio",
            role="voice_sample_replacement",
            session_id=session_id,
        )
        with self.database.session() as session:
            sample = session.scalar(select(VoiceSample).where(VoiceSample.voice_id == voice.id))
            sample.artifact_id = sample_two.id

        result = self._start_stale(session_id, base)

        self.assertEqual(segment_ids, self._job_segment_ids(result["job_id"]))

    def test_linked_voice_reuse_follows_only_the_effective_reference(self):
        session_id, _revision_id, segment_ids = self._create_case()
        samples = []
        with self.database.session() as session:
            voice = Voice(name="Linked voice", metadata_json={"providers": {"audio_cpp": {
                "resource_kind": "linked_reference", "status": "ready", "voice_id": "linked-voice",
            }}})
            session.add(voice)
            session.flush()
            voice_id = voice.id
        for index in range(2):
            artifact = self.services["artifacts"].register(
                self._wave_path(f"sample-{index}"), kind="audio", role=f"voice_sample_{index}", session_id=session_id,
            )
            with self.database.session() as session:
                sample = VoiceSample(voice_id=voice_id, artifact_id=artifact.id,
                                    created_at=utcnow() - timedelta(days=1-index))
                session.add(sample)
                session.flush()
                samples.append(sample.id)
        override = self._override(service="audio_cpp", model="qwen3_tts_1_7b_base_q8_0", voice="linked-voice")
        self._seed_takes(session_id, segment_ids[:1], override)
        snapshot = self._resolved(session_id, override)
        before = self.handlers.prepare_audio_cpp_voice_reference(snapshot["tts"])
        # Editing an unused older reference must not discard current audio.
        with self.database.session() as session:
            old = session.get(VoiceSample, samples[0])
            old.transcript = "Unused replacement transcript"
            old.transcript_reviewed = True
            artifact = session.get(Artifact, old.artifact_id)
            artifact.content_hash = "different-unused-sample-hash"
        prepared = self.generation.prepare_start(session_id, run_override=override, stale_only=True)
        self.assertEqual(segment_ids[1:], prepared["requested_segment_ids"])
        after = self.handlers.prepare_audio_cpp_voice_reference(snapshot["tts"])
        self.assertEqual(before["audio_cpp_voice_ref_hash"], after["audio_cpp_voice_ref_hash"])
        # A changed effective reference is rejected even if the display voice
        # and provider ID are unchanged, including between queue and execution.
        queued = self._start_stale(session_id, override)
        with self.database.session() as session:
            newest = session.get(VoiceSample, samples[1])
            newest.transcript = "A changed reviewed reference"
            newest.transcript_reviewed = True
        with patch.object(self.handlers.tts_providers, "synthesize") as synthesize:
            with self.assertRaisesRegex(ValueError, "voice reference changed"):
                self.handlers.run_generation({"generation_run_id": queued["id"]}, lambda *_: None, threading.Event())
            synthesize.assert_not_called()

    def test_output_settings_do_not_change_reuse(self):
        session_id, _revision_id, segment_ids = self._create_case()
        override = self._override()
        self._seed_takes(session_id, segment_ids[:1], override)
        changed = {**override, "output": {"format": "mp3", "mix_voice_gain_db": -5},
                   "audio": {"sentence_silence_ms": 500, "synchronization_delay_ms": 1200}}
        prepared = self.generation.prepare_start(session_id, run_override=changed, stale_only=True)
        self.assertEqual(segment_ids[1:], prepared["requested_segment_ids"])

    def test_legacy_take_identity_is_playable_but_not_reused(self):
        session_id, _revision_id, segment_ids = self._create_case()
        self._seed_takes(
            session_id,
            segment_ids,
            self._override(),
            include_identity=False,
        )

        result = self._start_stale(session_id, self._override())

        self.assertCountEqual(segment_ids, self._job_segment_ids(result["job_id"]))
        with self.database.session() as session:
            self.assertEqual(
                0,
                session.scalar(
                    select(AudioTake.id).where(
                        AudioTake.generation_run_id == result["id"]
                    ).limit(1)
                )
                is not None,
            )

    def test_history_reports_audio_identity_staleness_without_new_plan_revision(self):
        session_id, revision_id, segment_ids = self._create_case()
        self._seed_takes(session_id, segment_ids, {})
        current = self.client.get(
            f"/api/v1/sessions/{session_id}/settings/tts",
            headers=self.headers,
        ).get_json()
        changed = self.client.put(
            f"/api/v1/sessions/{session_id}/settings/tts",
            json={"value": {**current["effective"], "model": "model-b"}},
            headers={**self.headers, "If-Match": f'"{current["revision"]}"'},
        )
        self.assertEqual(200, changed.status_code, changed.get_json())

        history = self.client.get(
            f"/api/v1/sessions/{session_id}/generation-plan/revisions",
            headers=self.headers,
        ).get_json()
        item = history["items"][0]
        self.assertEqual(revision_id, history["active_revision_id"])
        self.assertEqual(0, item["reusable_segment_count"])
        self.assertEqual(len(segment_ids), item["stale_segment_count"])
        self.assertEqual(len(segment_ids), item["audio_settings_stale_segment_count"])
        self.assertEqual(0, item["audio_identity_unknown_segment_count"])

    def test_ordinary_workflow_generation_persists_identity_in_each_take(self):
        session_id, _revision_id, segment_ids = self._create_case()
        prepared_path = self.services["paths"].uploads / "ordinary-prepared.json"
        prepared_path.write_text(
            json.dumps([{"text": "One"}, {"text": "Two"}, {"text": "Three"}]),
            encoding="utf-8",
        )
        source = self.services["artifacts"].register(
            prepared_path,
            kind="json",
            role="prepared_text",
            session_id=session_id,
        )
        settings = self._override()["tts"]
        calls = []

        def synthesize(text, _settings, **_kwargs):
            calls.append(text)
            return AudioSegment.silent(duration=20)

        capabilities = TtsCapabilities(batch_synthesis=False, streaming_batch=False)
        with patch.object(
            self.handlers.tts_providers,
            "synthesize",
            side_effect=synthesize,
        ), patch.object(
            self.handlers.tts_providers,
            "synthesis_capabilities",
            return_value=capabilities,
        ):
            result = self.handlers._run_reviewable_generation(
                {
                    "session_id": session_id,
                    "source_artifact_id": source.id,
                    "settings": settings,
                },
                lambda *_: None,
                threading.Event(),
                resolved_snapshot=self._resolved(session_id, self._override()),
            )

        self.assertEqual("completed", result["status"])
        self.assertEqual(["One", "Two", "Three"], calls)
        with self.database.session() as session:
            run = session.get(GenerationRun, result["generation_run_id"])
            expected = run.settings_snapshot_json["generation_audio_identities"]
            takes = list(
                session.scalars(
                    select(AudioTake).where(
                        AudioTake.generation_run_id == result["generation_run_id"]
                    )
                )
            )
            self.assertEqual(set(segment_ids), {take.generation_segment_id for take in takes})
            for take in takes:
                artifact = session.get(Artifact, take.artifact_id)
                self.assertEqual(expected[take.generation_segment_id], artifact.metadata_json[IDENTITY_KEY])

        with self.assertRaisesRegex(ValueError, "no missing or stale"):
            self.generation.prepare_start(session_id, run_override=self._override(), stale_only=True)
        changed = self.generation.prepare_start(session_id, run_override=self._override(voice="voice-b"), stale_only=True)
        self.assertCountEqual(segment_ids, changed["requested_segment_ids"])

    def test_worker_persists_identity_and_resume_regenerates_unknown_take(self):
        session_id, _revision_id, segment_ids = self._create_case()
        override = self._override()
        result = self.generation.start(session_id, run_override=override)
        calls = []

        def synthesize(text, _settings, **_kwargs):
            calls.append(text)
            return AudioSegment.silent(duration=20)

        capabilities = TtsCapabilities(batch_synthesis=False, streaming_batch=False)
        with patch.object(
            self.handlers.tts_providers,
            "synthesize",
            side_effect=synthesize,
        ), patch.object(
            self.handlers.tts_providers,
            "synthesis_capabilities",
            return_value=capabilities,
        ):
            generated = self.handlers.run_generation(
                {"generation_run_id": result["id"]},
                lambda *_: None,
                threading.Event(),
            )
        self.assertEqual("completed", generated["status"])
        with self.database.session() as session:
            run = session.get(GenerationRun, result["id"])
            expected = run.settings_snapshot_json["generation_audio_identities"]
            takes = list(
                session.scalars(
                    select(AudioTake).where(
                        AudioTake.generation_run_id == result["id"]
                    )
                )
            )
            for take in takes:
                artifact = session.get(Artifact, take.artifact_id)
                self.assertEqual(expected[take.generation_segment_id], artifact.metadata_json[IDENTITY_KEY])
                if take.generation_segment_id == segment_ids[0]:
                    artifact.metadata_json = {
                        key: value
                        for key, value in (artifact.metadata_json or {}).items()
                        if key != IDENTITY_KEY
                    }
            run.status = "paused"
            run.pause_requested = False

        calls.clear()
        with patch.object(
            self.handlers.tts_providers,
            "synthesize",
            side_effect=synthesize,
        ), patch.object(
            self.handlers.tts_providers,
            "synthesis_capabilities",
            return_value=capabilities,
        ):
            resumed = self.handlers.run_generation(
                {
                    "generation_run_id": result["id"],
                    "operation": "resume",
                    "segment_ids": [],
                },
                lambda *_: None,
                threading.Event(),
            )
        self.assertEqual("completed", resumed["status"])
        self.assertEqual(2, resumed["skipped"])
        self.assertEqual(1, resumed["generated"])
        self.assertEqual(["One"], calls)


if __name__ == "__main__":
    unittest.main()
