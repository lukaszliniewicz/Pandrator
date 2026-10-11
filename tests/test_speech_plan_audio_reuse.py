"""Safe TTS take inheritance for rebuilt speech plans."""

from __future__ import annotations

import copy
import io
import tempfile
import unittest
import uuid
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select

from pandrator.web import models as m
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.generation_audio_identity import IDENTITY_KEY, AudioIdentityContext
from pandrator.web.speech_plan_audio_reuse import inherit_rebuilt_plan_audio
from pandrator.web.speech_plan_preparation import run_speech_preparation
from pandrator.web.tts_optimization import OptimizationUsage

SRT = (
    "1\n00:00:01,000 --> 00:00:03,000\nFirst cue.\n\n"
    "2\n00:00:04,000 --> 00:00:06,000\nSecond cue.\n"
)


class SpeechPlanAudioReuseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        bootstrap = BootstrapTokenStore()
        cls.app = create_app(
            data_root=cls.temporary.name,
            testing=True,
            bootstrap_tokens=bootstrap,
        )
        cls.services = cls.app.extensions["pandrator"]
        cls.database = cls.services["database"]
        cls.client = cls.app.test_client()
        token = bootstrap.issue()
        cls.headers = {
            "X-CSRF-Token": cls.client.post(
                "/api/v1/auth/bootstrap", json={"token": token}
            ).get_json()["csrf_token"]
        }

    @classmethod
    def tearDownClass(cls):
        cls.database.dispose()
        cls.temporary.cleanup()

    def setUp(self):
        self.root = Path(self.temporary.name)

    def _new_session(self, name="audio reuse"):
        response = self.client.post(
            "/api/v1/sessions",
            json={"name": f"{name} {uuid.uuid4().hex[:8]}", "workflow_kind": "voiceover"},
            headers=self.headers,
        )
        self.assertEqual(201, response.status_code, response.get_json())
        return response.get_json()["id"]

    def _make_case(
        self,
        *,
        count=1,
        timing_change=False,
        different_document=False,
        narration=False,
        target_refs=None,
        changed_text_indexes=(),
    ):
        session_id = self._new_session()
        case = {"session_id": session_id, "artifact_ids": [], "paths": []}
        with self.database.session() as session:
            plan = m.GenerationPlan(session_id=session_id)
            session.add(plan)
            session.flush()

            source_document_revision_id = None
            target_document_revision_id = None
            source_cues = []
            target_cues = []
            if not narration:
                source_document = m.Document(session_id=session_id, stage="subtitles")
                target_document = (
                    m.Document(session_id=session_id, stage="subtitles")
                    if different_document
                    else source_document
                )
                session.add_all([source_document, target_document] if different_document else [source_document])
                session.flush()
                source_document_revision = m.DocumentRevision(
                    document_id=source_document.id,
                    revision_number=1,
                    content_hash="source-cues",
                )
                target_document_revision = m.DocumentRevision(
                    document_id=target_document.id,
                    revision_number=1 if different_document else 2,
                    content_hash="target-cues",
                )
                session.add_all([source_document_revision, target_document_revision])
                session.flush()
                source_document_revision_id = source_document_revision.id
                target_document_revision_id = target_document_revision.id
                for index in range(count):
                    start = (index + 1) * 1000
                    source_cue = m.Segment(
                        revision_id=source_document_revision.id,
                        ordinal=index,
                        node_kind="subtitle_cue",
                        start_ms=start,
                        end_ms=start + 1500,
                        text=f"Source cue {index + 1}",
                    )
                    target_start = start + (100 if timing_change and index == 0 else 0)
                    target_cue = m.Segment(
                        revision_id=target_document_revision.id,
                        ordinal=index,
                        node_kind="subtitle_cue",
                        start_ms=target_start,
                        end_ms=target_start + 1500,
                        text=f"Target cue {index + 1}",
                    )
                    source_cues.append(source_cue)
                    target_cues.append(target_cue)
                session.add_all([*source_cues, *target_cues])
                session.flush()

            source_settings = {"_source_artifact_id": "narration-source"} if narration else {}
            target_settings = copy.deepcopy(source_settings)
            source_revision = m.GenerationPlanRevision(
                plan_id=plan.id,
                revision_number=1,
                settings_json=source_settings,
                content_hash="source-plan",
                source_revision_id=source_document_revision_id,
            )
            session.add(source_revision)
            session.flush()
            target_revision = m.GenerationPlanRevision(
                plan_id=plan.id,
                parent_revision_id=source_revision.id,
                revision_number=2,
                settings_json=target_settings,
                content_hash="target-plan",
                source_revision_id=target_document_revision_id,
            )
            session.add(target_revision)
            session.flush()
            plan.active_revision_id = target_revision.id

            source_segments = []
            target_segments = []
            for index in range(count):
                if narration:
                    source_refs = ["source-unit-1"]
                    target_segment_refs = list(target_refs) if target_refs is not None else source_refs.copy()
                else:
                    source_refs = [source_cues[index].id]
                    target_segment_refs = (
                        list(target_refs)
                        if target_refs is not None and count == 1
                        else [target_cues[index].id]
                    )
                source_segment = m.GenerationSegment(
                    plan_revision_id=source_revision.id,
                    ordinal=index,
                    source_segment_ids_json=source_refs,
                    alignment_group="group-a",
                    node_kind="subtitle_cue",
                    text=f"Source wording {index + 1}",
                    optimized_text=f"Spoken wording {index + 1}",
                    voice="voice-a",
                    language="en",
                    status="completed",
                )
                target_spoken = (
                    f"Changed wording {index + 1}"
                    if index in changed_text_indexes
                    else f"Spoken wording {index + 1}"
                )
                target_segment = m.GenerationSegment(
                    plan_revision_id=target_revision.id,
                    ordinal=index,
                    source_segment_ids_json=target_segment_refs,
                    alignment_group="group-a",
                    node_kind="subtitle_cue",
                    text=f"Target wording {index + 1}",
                    optimized_text=target_spoken,
                    speech_plan_json={"speech_xml": f"<speak>Target {index + 1}</speak>"},
                    voice="voice-a",
                    language="en",
                    status="ready",
                )
                source_segments.append(source_segment)
                target_segments.append(target_segment)
            session.add_all([*source_segments, *target_segments])
            session.flush()

            snapshot, _ = self.services["workspace_settings"].resolve_in_session(
                session, session_id
            )
            audio_context = AudioIdentityContext(session, snapshot)
            source_takes = []
            for index, source_segment in enumerate(source_segments):
                identity = audio_context.for_segment(source_segment)
                relative_path = f"artifacts/audio-{uuid.uuid4().hex}.wav"
                path = self.root / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture audio bytes")
                artifact = m.Artifact(
                    session_id=session_id,
                    kind="audio",
                    role="tts",
                    relative_path=relative_path,
                    mime_type="audio/wav",
                    state="current",
                    metadata_json={
                        IDENTITY_KEY: identity,
                        "synthesized_text": source_segment.optimized_text or source_segment.text,
                    },
                )
                session.add(artifact)
                session.flush()
                take = m.AudioTake(
                    generation_segment_id=source_segment.id,
                    generation_run_id=None,
                    artifact_id=artifact.id,
                    kind="tts",
                    status="completed",
                    settings_hash=identity["settings_hash"],
                    duration_ms=1234 + index,
                    is_active=True,
                    revision=7,
                )
                session.add(take)
                source_takes.append(take)
                case["artifact_ids"].append(artifact.id)
                case["paths"].append(path)
            session.flush()
            case.update(
                plan_id=plan.id,
                source_revision_id=source_revision.id,
                target_revision_id=target_revision.id,
                source_segment_ids=[segment.id for segment in source_segments],
                target_segment_ids=[segment.id for segment in target_segments],
                source_take_ids=[take.id for take in source_takes],
                source_cue_ids=[cue.id for cue in source_cues],
                target_cue_ids=[cue.id for cue in target_cues],
            )
        return case

    def _snapshot(self, session, session_id):
        return self.services["workspace_settings"].resolve_in_session(session, session_id)[0]

    def _inherit(self, case, source_revision_id=None, snapshot=None):
        with self.database.session() as session:
            return inherit_rebuilt_plan_audio(
                session,
                source_revision_id or case["source_revision_id"],
                case["target_revision_id"],
                snapshot or self._snapshot(session, case["session_id"]),
            )

    def test_same_document_reuses_only_unchanged_spoken_text_and_preserves_source(self):
        case = self._make_case(count=2, changed_text_indexes=(1,))
        with self.database.session() as session:
            source_take = session.get(m.AudioTake, case["source_take_ids"][0])
            source_artifact = session.get(m.Artifact, case["artifact_ids"][0])
            source_segment = session.get(m.GenerationSegment, case["source_segment_ids"][0])
            target_segment = session.get(m.GenerationSegment, case["target_segment_ids"][0])
            source_take_before = (
                source_take.id,
                source_take.generation_segment_id,
                source_take.artifact_id,
                source_take.parent_take_id,
                source_take.is_active,
                source_take.status,
                source_take.revision,
            )
            source_artifact_before = copy.deepcopy(source_artifact.metadata_json)
            target_before = (
                target_segment.text,
                target_segment.optimized_text,
                target_segment.speech_plan_json,
                target_segment.voice,
            )
            source_status = source_segment.status
            source_revision_id = case["source_revision_id"]
            target_revision_id = case["target_revision_id"]
            snapshot = self._snapshot(session, case["session_id"])
            receipt = inherit_rebuilt_plan_audio(
                session, source_revision_id, target_revision_id, snapshot
            )
            inherited = session.scalar(
                select(m.AudioTake).where(
                    m.AudioTake.generation_segment_id == target_segment.id
                )
            )
            self.assertEqual(
                (1, 1, {"synthesized_text_changed": 1}),
                (
                    receipt["reused_audio_count"],
                    receipt["new_audio_needed_count"],
                    receipt["reason_counts"],
                ),
            )
            self.assertEqual(
                source_take_before,
                (
                    source_take.id,
                    source_take.generation_segment_id,
                    source_take.artifact_id,
                    source_take.parent_take_id,
                    source_take.is_active,
                    source_take.status,
                    source_take.revision,
                ),
            )
            self.assertEqual(source_artifact_before, source_artifact.metadata_json)
            self.assertEqual(source_status, source_segment.status)
            self.assertEqual(
                target_before,
                (
                    target_segment.text,
                    target_segment.optimized_text,
                    target_segment.speech_plan_json,
                    target_segment.voice,
                ),
            )
            self.assertEqual(target_segment.status, "completed")
            self.assertEqual(inherited.artifact_id, source_take.artifact_id)
            self.assertEqual(inherited.parent_take_id, source_take.id)
            self.assertEqual(inherited.kind, "tts")
            self.assertEqual(inherited.status, "completed")
            self.assertTrue(inherited.is_active)
            self.assertIsNone(inherited.generation_run_id)
            self.assertEqual(inherited.settings_hash, source_take.settings_hash)
            self.assertEqual(inherited.duration_ms, source_take.duration_ms)
            self.assertEqual(inherited.revision, source_take.revision)
            self.assertNotIn(str(case["paths"][0]), str(receipt))

    def test_changed_timing_skips_only_the_cue_whose_exact_boundary_changed(self):
        case = self._make_case(count=2, timing_change=True)
        with self.database.session() as session:
            source_times_before = list(
                session.scalars(
                    select(m.Segment.start_ms).where(
                        m.Segment.id.in_(case["source_cue_ids"])
                    )
                )
            )
            target_times_before = list(
                session.scalars(
                    select(m.Segment.start_ms).where(
                        m.Segment.id.in_(case["target_cue_ids"])
                    )
                )
            )
            receipt = inherit_rebuilt_plan_audio(
                session,
                case["source_revision_id"],
                case["target_revision_id"],
                self._snapshot(session, case["session_id"]),
            )
            self.assertEqual(1, receipt["reused_audio_count"])
            self.assertEqual(1, receipt["new_audio_needed_count"])
            self.assertEqual({"provenance_unavailable": 1}, receipt["reason_counts"])
            self.assertEqual(
                source_times_before,
                list(
                    session.scalars(
                        select(m.Segment.start_ms).where(
                            m.Segment.id.in_(case["source_cue_ids"])
                        )
                    )
                ),
            )
            self.assertEqual(
                target_times_before,
                list(
                    session.scalars(
                        select(m.Segment.start_ms).where(
                            m.Segment.id.in_(case["target_cue_ids"])
                        )
                    )
                ),
            )

    def test_identity_context_and_unknown_identity_mismatches_are_rejected(self):
        case = self._make_case()
        with self.database.session() as session:
            artifact = session.get(m.Artifact, case["artifact_ids"][0])
            original = copy.deepcopy(artifact.metadata_json)
            identity = copy.deepcopy(original[IDENTITY_KEY])
            for key, value, reason in (
                ("settings_hash", "different-model-or-delivery", "generation_settings_changed"),
                ("voice_reference_hash", "different-voice-reference", "voice_reference_changed"),
                ("performance_request_hash", "different-context", "performance_changed"),
            ):
                with self.subTest(identity_field=key):
                    artifact.metadata_json = copy.deepcopy(original)
                    artifact.metadata_json[IDENTITY_KEY][key] = value
                    receipt = inherit_rebuilt_plan_audio(
                        session,
                        case["source_revision_id"],
                        case["target_revision_id"],
                        self._snapshot(session, case["session_id"]),
                    )
                    self.assertEqual({reason: 1}, receipt["reason_counts"])
                    self.assertEqual(0, receipt["reused_audio_count"])
            artifact.metadata_json = copy.deepcopy(original)
            artifact.metadata_json.pop(IDENTITY_KEY)
            receipt = inherit_rebuilt_plan_audio(
                session,
                case["source_revision_id"],
                case["target_revision_id"],
                self._snapshot(session, case["session_id"]),
            )
            self.assertEqual({"audio_identity_unknown": 1}, receipt["reason_counts"])
            self.assertEqual(identity["schema_version"], original[IDENTITY_KEY]["schema_version"])

    def test_ambiguous_and_unresolved_provenance_never_guess(self):
        case = self._make_case()
        with self.database.session() as session:
            source = session.get(m.GenerationSegment, case["source_segment_ids"][0])
            duplicate_source = m.GenerationSegment(
                plan_revision_id=case["source_revision_id"],
                ordinal=1,
                source_segment_ids_json=list(source.source_segment_ids_json),
                alignment_group=source.alignment_group,
                node_kind=source.node_kind,
                text="Duplicate source block",
                optimized_text=source.optimized_text,
                voice=source.voice,
                language=source.language,
                status="completed",
            )
            session.add(duplicate_source)
            target = session.get(m.GenerationSegment, case["target_segment_ids"][0])
            source_refs_before = list(source.source_segment_ids_json)
            receipt = inherit_rebuilt_plan_audio(
                session,
                case["source_revision_id"],
                case["target_revision_id"],
                self._snapshot(session, case["session_id"]),
            )
            self.assertEqual({"provenance_ambiguous": 1}, receipt["reason_counts"])
            self.assertEqual(0, receipt["reused_audio_count"])
            session.delete(duplicate_source)
            target.source_segment_ids_json = ["unresolved-cue-id"]
            receipt = inherit_rebuilt_plan_audio(
                session,
                case["source_revision_id"],
                case["target_revision_id"],
                self._snapshot(session, case["session_id"]),
            )
            self.assertEqual({"provenance_unavailable": 1}, receipt["reason_counts"])
            self.assertEqual(source_refs_before, source.source_segment_ids_json)
            self.assertEqual(["unresolved-cue-id"], target.source_segment_ids_json)

        duplicate_target = self._make_case()
        with self.database.session() as session:
            target = session.get(m.GenerationSegment, duplicate_target["target_segment_ids"][0])
            session.add(
                m.GenerationSegment(
                    plan_revision_id=duplicate_target["target_revision_id"],
                    ordinal=1,
                    source_segment_ids_json=list(target.source_segment_ids_json),
                    alignment_group=target.alignment_group,
                    node_kind=target.node_kind,
                    text="Duplicate target block",
                    optimized_text=target.optimized_text,
                    voice=target.voice,
                    language=target.language,
                    status="ready",
                )
            )
            session.flush()
            receipt = inherit_rebuilt_plan_audio(
                session,
                duplicate_target["source_revision_id"],
                duplicate_target["target_revision_id"],
                self._snapshot(session, duplicate_target["session_id"]),
            )
            self.assertEqual(2, receipt["new_audio_needed_count"])
            self.assertEqual({"provenance_ambiguous": 2}, receipt["reason_counts"])
            self.assertEqual(0, receipt["reused_audio_count"])

    def test_duplicate_target_take_is_not_cloned_or_overwritten(self):
        case = self._make_case()
        with self.database.session() as session:
            target_id = case["target_segment_ids"][0]
            session.add(
                m.AudioTake(
                    generation_segment_id=target_id,
                    kind="manual",
                    status="failed",
                    is_active=False,
                )
            )
            session.flush()
            receipt = inherit_rebuilt_plan_audio(
                session,
                case["source_revision_id"],
                case["target_revision_id"],
                self._snapshot(session, case["session_id"]),
            )
            self.assertEqual({"target_has_take": 1}, receipt["reason_counts"])
            self.assertEqual(0, receipt["reused_audio_count"])
            target_takes = list(
                session.scalars(
                    select(m.AudioTake).where(
                        m.AudioTake.generation_segment_id == target_id
                    )
                ).all()
            )
            self.assertEqual(
                1,
                len(target_takes),
            )
            self.assertEqual("manual", target_takes[0].kind)

    def test_missing_or_deleted_artifact_is_rejected(self):
        case = self._make_case()
        with self.database.session() as session:
            artifact = session.get(m.Artifact, case["artifact_ids"][0])
            case["paths"][0].unlink()
            receipt = inherit_rebuilt_plan_audio(
                session,
                case["source_revision_id"],
                case["target_revision_id"],
                self._snapshot(session, case["session_id"]),
            )
            self.assertEqual({"audio_unavailable": 1}, receipt["reason_counts"])
            case["paths"][0].write_bytes(b"restored test audio")
            artifact.state = "deleted"
            receipt = inherit_rebuilt_plan_audio(
                session,
                case["source_revision_id"],
                case["target_revision_id"],
                self._snapshot(session, case["session_id"]),
            )
            self.assertEqual({"audio_unavailable": 1}, receipt["reason_counts"])
            self.assertEqual(0, receipt["reused_audio_count"])

    def test_artifact_path_escape_and_cross_session_artifact_are_rejected(self):
        case = self._make_case()
        outside_path = self.root.parent / f"speech-plan-reuse-{uuid.uuid4().hex}.wav"
        outside_path.write_bytes(b"outside root fixture")
        self.addCleanup(lambda: outside_path.unlink(missing_ok=True))
        other_session_id = self._new_session("foreign audio owner")
        with self.database.session() as session:
            artifact = session.get(m.Artifact, case["artifact_ids"][0])
            artifact.relative_path = f"../{outside_path.name}"
            receipt = inherit_rebuilt_plan_audio(
                session,
                case["source_revision_id"],
                case["target_revision_id"],
                self._snapshot(session, case["session_id"]),
            )
            self.assertEqual({"audio_unavailable": 1}, receipt["reason_counts"])

            artifact.relative_path = case["paths"][0].relative_to(self.root).as_posix()
            artifact.session_id = other_session_id
            receipt = inherit_rebuilt_plan_audio(
                session,
                case["source_revision_id"],
                case["target_revision_id"],
                self._snapshot(session, case["session_id"]),
            )
            self.assertEqual({"audio_unavailable": 1}, receipt["reason_counts"])

    def test_only_completed_active_tts_from_completed_segments_can_be_cloned(self):
        case = self._make_case()
        with self.database.session() as session:
            source_segment = session.get(m.GenerationSegment, case["source_segment_ids"][0])
            source_take = session.get(m.AudioTake, case["source_take_ids"][0])
            snapshot = self._snapshot(session, case["session_id"])

            source_segment.status = "ready"
            receipt = inherit_rebuilt_plan_audio(
                session,
                case["source_revision_id"],
                case["target_revision_id"],
                snapshot,
            )
            self.assertEqual({"not_completed": 1}, receipt["reason_counts"])

            source_segment.status = "completed"
            source_take.status = "failed"
            receipt = inherit_rebuilt_plan_audio(
                session,
                case["source_revision_id"],
                case["target_revision_id"],
                snapshot,
            )
            self.assertEqual({"not_completed": 1}, receipt["reason_counts"])

            source_take.status = "completed"
            source_take.is_active = False
            receipt = inherit_rebuilt_plan_audio(
                session,
                case["source_revision_id"],
                case["target_revision_id"],
                snapshot,
            )
            self.assertEqual({"audio_unavailable": 1}, receipt["reason_counts"])
            self.assertEqual(0, receipt["reused_audio_count"])

    def test_rvc_global_and_selected_override_disable_inheritance(self):
        case = self._make_case()
        with self.database.session() as session:
            snapshot = self._snapshot(session, case["session_id"])
            for changed_snapshot in (
                {**snapshot, "rvc": {"enabled": True}},
                {
                    **snapshot,
                    "selected_segment_override": {"rvc": {"enabled": True}},
                },
            ):
                receipt = inherit_rebuilt_plan_audio(
                    session,
                    case["source_revision_id"],
                    case["target_revision_id"],
                    changed_snapshot,
                )
                self.assertEqual({"rvc_enabled": 1}, receipt["reason_counts"])
                self.assertEqual(0, receipt["reused_audio_count"])

    def test_different_document_and_wrong_immediate_parent_are_rejected(self):
        different_document = self._make_case(different_document=True)
        receipt = self._inherit(different_document)
        self.assertEqual({"source_document_changed": 1}, receipt["reason_counts"])

        wrong_parent = self._make_case()
        receipt = self._inherit(wrong_parent, source_revision_id="not-the-parent")
        self.assertEqual({"source_revision_unavailable": 1}, receipt["reason_counts"])

    def test_narration_requires_same_source_artifact_and_exact_typed_refs(self):
        same_source = self._make_case(narration=True)
        receipt = self._inherit(same_source)
        self.assertEqual(1, receipt["reused_audio_count"])

        typed_mismatch = self._make_case(narration=True, target_refs=["1"])
        with self.database.session() as session:
            target = session.get(m.GenerationSegment, typed_mismatch["target_segment_ids"][0])
            target.source_segment_ids_json = [1]
        receipt = self._inherit(typed_mismatch)
        self.assertEqual({"provenance_unavailable": 1}, receipt["reason_counts"])

        missing_refs = self._make_case(narration=True)
        with self.database.session() as session:
            target = session.get(m.GenerationSegment, missing_refs["target_segment_ids"][0])
            target.source_segment_ids_json = []
        receipt = self._inherit(missing_refs)
        self.assertEqual({"provenance_unavailable": 1}, receipt["reason_counts"])

    def test_prepare_response_reports_reuse_and_stale_only_start_preserves_baseline(self):
        session_id = self._new_session("prepare integration")
        with self.database.session() as session:
            outcome = session.get(m.OutcomePlan, session_id)
            if outcome is None:
                outcome = m.OutcomePlan(session_id=session_id, value_json={})
                session.add(outcome)
            outcome.value_json = {
                **dict(outcome.value_json or {}),
                "inputs": {"generation": "source"},
            }
        upload = self.client.post(
            "/api/v1/uploads",
            data={"file": (io.BytesIO(SRT.encode()), "source.srt"), "session_id": session_id},
            headers=self.headers,
        )
        self.assertEqual(201, upload.status_code, upload.get_json())

        def prepare_request():
            state = self.client.get(
                f"/api/v1/sessions/{session_id}/generation-plan/status"
            ).get_json()
            return self.client.post(
                f"/api/v1/sessions/{session_id}/generation-plan/prepare",
                json={
                    "expected_revision": state["session_revision"],
                    "expected_plan_revision_id": state["selected_revision_id"],
                    "source_artifact_id": state["current_input"]["artifact_id"],
                },
                headers={**self.headers, "Idempotency-Key": uuid.uuid4().hex},
            )

        initial = prepare_request()
        self.assertEqual(200, initial.status_code, initial.get_json())
        source_revision_id = initial.get_json()["selected_revision_id"]
        with self.database.session() as session:
            source_segments = list(
                session.scalars(
                    select(m.GenerationSegment)
                    .where(m.GenerationSegment.plan_revision_id == source_revision_id)
                    .order_by(m.GenerationSegment.ordinal)
                ).all()
            )
            snapshot = self._snapshot(session, session_id)
            context = AudioIdentityContext(session, snapshot)
            for index, source_segment in enumerate(source_segments):
                identity = context.for_segment(source_segment)
                relative_path = f"artifacts/integration-{uuid.uuid4().hex}.wav"
                path = self.root / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"integration fixture audio")
                artifact = m.Artifact(
                    session_id=session_id,
                    kind="audio",
                    role="tts",
                    relative_path=relative_path,
                    mime_type="audio/wav",
                    state="current",
                    metadata_json={
                        IDENTITY_KEY: identity,
                        "synthesized_text": source_segment.optimized_text or source_segment.text,
                    },
                )
                session.add(artifact)
                session.flush()
                source_segment.status = "completed"
                session.add(
                    m.AudioTake(
                        generation_segment_id=source_segment.id,
                        artifact_id=artifact.id,
                        kind="tts",
                        status="completed",
                        settings_hash=identity["settings_hash"],
                        duration_ms=2000 + index,
                        is_active=True,
                    )
                )

        with self.database.session() as session:
            text_setting = session.get(m.SessionSetting, (session_id, "text"))
            if text_setting is None:
                text_setting = m.SessionSetting(session_id=session_id, section="text", value_json={})
                session.add(text_setting)
            text_setting.value_json = {
                **dict(text_setting.value_json or {}),
                "llm_tts_optimization": True,
            }
            outcome = session.get(m.OutcomePlan, session_id)
            outcome.value_json = {
                **dict(outcome.value_json or {}),
                "transformations": {"llm_tts_optimization": True},
            }
        def model_settings(*_args, **_kwargs):
            return SimpleNamespace(
                provider_configs=[], default_model="fake/model", request_timeout_seconds=1
            ), "fake/model"

        with patch(
            "pandrator.web.speech_plan_preparation.build_llm_settings",
            side_effect=model_settings,
        ):
            queued = prepare_request()
        self.assertEqual(200, queued.status_code, queued.get_json())
        payload = queued.get_json()
        with self.database.session() as session:
            job = session.get(m.Job, payload["job_id"])
            job_payload = dict(job.payload_json)
            job.status = "running"

        def optimize(texts, *_args, **_kwargs):
            return [texts[0], f"Changed spoken text: {texts[1]}"], OptimizationUsage(
                cost=0.0,
                response_count=1,
                usage={},
                cost_sources=["test"],
            )

        with patch(
            "pandrator.web.speech_plan_preparation.build_llm_settings",
            side_effect=model_settings,
        ), patch("pandrator.web.tts_optimization.optimize_texts", side_effect=optimize):
            prepared = run_speech_preparation(
                self.services["workflow_handlers"],
                job_payload,
                lambda *_args: None,
                Event(),
            )
        self.assertEqual(
            {"reused_audio_count": 1, "new_audio_needed_count": 1, "reason_counts": {"synthesized_text_changed": 1}},
            prepared["audio_reuse"],
        )
        target_revision_id = prepared["selected_revision_id"]
        with self.database.session() as session:
            target_segments = list(
                session.scalars(
                    select(m.GenerationSegment)
                    .where(m.GenerationSegment.plan_revision_id == target_revision_id)
                    .order_by(m.GenerationSegment.ordinal)
                ).all()
            )
            inherited = session.scalar(
                select(m.AudioTake).where(
                    m.AudioTake.generation_segment_id == target_segments[0].id
                )
            )
            unchanged_target = target_segments[0].id
            changed_target = target_segments[1].id
            self.assertEqual(inherited.kind, "tts")
            self.assertEqual(inherited.status, "completed")
            self.assertTrue(inherited.is_active)

        started = self.services["generation"].start(
            session_id,
            stale_only=True,
            speech_plan_revision_id=target_revision_id,
        )
        with self.database.session() as session:
            queued_run = session.get(m.GenerationRun, started["id"])
            generation_job = session.get(m.Job, queued_run.job_id)
            selected_ids = list((generation_job.payload_json or {}).get("segment_ids") or [])
            self.assertEqual([changed_target], selected_ids)
            self.assertEqual("queued", generation_job.status)
            inherited_after_start = session.scalar(
                select(m.AudioTake).where(
                    m.AudioTake.generation_run_id == queued_run.id,
                    m.AudioTake.generation_segment_id == unchanged_target,
                )
            )
            self.assertIsNotNone(inherited_after_start)
            self.assertEqual(inherited_after_start.parent_take_id, inherited.id)
            self.assertEqual(inherited_after_start.artifact_id, inherited.artifact_id)
            self.assertTrue(inherited_after_start.is_active)


if __name__ == "__main__":
    unittest.main()
