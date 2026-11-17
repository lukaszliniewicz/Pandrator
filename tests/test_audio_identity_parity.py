"""Audio identity parity between preview snapshots and frozen run snapshots."""

from __future__ import annotations

import copy
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from pydub import AudioSegment
from sqlalchemy import select

from pandrator.web import models as m
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.generation_audio_identity import (
    IDENTITY_KEY,
    AudioIdentityContext,
    _hash,
    _material_settings,
    identity_for_storage,
    identity_matches,
    plan_audio_identities,
    stored_take_reuse_reason,
)
from pandrator.web.projects.artifact_identity import resolve_artifact_ownership
from pandrator.web.settings_policy import BUILTIN_DEFAULTS, adapt_runtime_settings
from pandrator.web.speech_plan_audio_reuse import inherit_rebuilt_plan_audio
from pandrator.web.tts_providers import TtsCapabilities

OVERRIDE = {
    "tts": {
        "service": "openai",
        "model": "model-a",
        "voice": "voice-a",
        "language": "en",
        "temperature": 0.75,
        "speed": 1.0,
        "tts_batch_size": 1,
    }
}


def _sections() -> dict[str, dict]:
    return {name: copy.deepcopy(values) for name, values in BUILTIN_DEFAULTS.items()}


def _flattened(sections: dict[str, dict]) -> dict:
    values: dict = {}
    for section in sorted(sections):
        values.update(adapt_runtime_settings(section, sections[section]))
    return values


def _run_style(sections: dict[str, dict]) -> dict:
    """A frozen run snapshot: every section's flattened values merged into tts/audio."""
    flattened = _flattened(sections)
    return {
        "tts": {**sections["tts"], **flattened},
        "audio": {**sections["audio"], **flattened},
    }


class AudioIdentitySettingsParityTests(unittest.TestCase):
    def test_preview_and_run_snapshots_share_material_settings(self):
        sections = _sections()
        self.assertIn("timing_context_mode", sections["correction"])
        self.assertIn("timing_context_mode", sections["translation"])
        preview = {"tts": sections["tts"], "audio": sections["audio"]}
        run = _run_style(sections)
        self.assertIn("timing_context_enabled", run["tts"])

        preview_material = _material_settings(preview)
        run_material = _material_settings(run)
        self.assertEqual(preview_material, run_material)
        self.assertEqual(
            _hash({"tts": preview_material, "rvc": {}}),
            _hash({"tts": run_material, "rvc": {}}),
        )

    def test_no_derived_section_key_survives_material_settings(self):
        sections = _sections()
        material = _material_settings(_run_style(sections))
        for section, defaults in sections.items():
            if section in {"tts", "audio"}:
                continue
            derived = set(adapt_runtime_settings(section, defaults)) - set(defaults)
            self.assertEqual(
                set(),
                derived.intersection(material),
                f"{section} derives keys that now reach the audio identity: "
                f"{sorted(derived.intersection(material))}",
            )


class AudioIdentityParityTests(unittest.TestCase):
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

    def _new_session(self, name="audio identity parity"):
        response = self.client.post(
            "/api/v1/sessions",
            json={"name": f"{name} {uuid.uuid4().hex[:8]}", "workflow_kind": "voiceover"},
            headers=self.headers,
        )
        self.assertEqual(201, response.status_code, response.get_json())
        return response.get_json()["id"]

    def _resolved(self, session_id, override=None):
        with self.database.session() as session:
            snapshot, _ = self.services["workspace_settings"].resolve_in_session(
                session, session_id, run_override=override
            )
        return snapshot

    def _plan_case(self):
        session_id = self._new_session()
        self.services["generation"].create_plan(
            session_id,
            source_revision_id=None,
            settings={},
            segments=[{"text": "One"}],
        )
        with self.database.session() as session:
            revision_id = session.scalar(
                select(m.GenerationPlan.active_revision_id)
                .join(m.SessionRecord, m.SessionRecord.id == m.GenerationPlan.session_id)
                .where(m.GenerationPlan.session_id == session_id)
            )
            segment_id = session.scalar(
                select(m.GenerationSegment.id)
                .where(m.GenerationSegment.plan_revision_id == revision_id)
                .order_by(m.GenerationSegment.ordinal)
            )
        return session_id, revision_id, segment_id

    def _identity(self, snapshot, segment_id):
        with self.database.session() as session:
            return AudioIdentityContext(session, snapshot).for_segment(
                session.get(m.GenerationSegment, segment_id)
            )

    def test_preview_and_run_identities_are_equal(self):
        session_id, _revision_id, segment_id = self._plan_case()
        preview = self._resolved(session_id, copy.deepcopy(OVERRIDE))
        self.assertIn("timing_context_enabled", _run_style(preview)["tts"])

        preview_identity = self._identity(preview, segment_id)
        run_identity = self._identity(_run_style(preview), segment_id)
        self.assertEqual(preview_identity, run_identity)
        self.assertEqual(preview_identity["settings_hash"], run_identity["settings_hash"])
        self.assertEqual(2, len(preview_identity["legacy_settings_hashes"]))

    def test_legacy_timing_context_hash_is_reusable(self):
        session_id, _revision_id, segment_id = self._plan_case()
        expected = self._identity(self._resolved(session_id, copy.deepcopy(OVERRIDE)), segment_id)
        stored = identity_for_storage(expected)
        for legacy_hash in expected["legacy_settings_hashes"]:
            self.assertEqual(
                "reusable",
                stored_take_reuse_reason(
                    "completed",
                    "completed",
                    "current",
                    {IDENTITY_KEY: {**stored, "settings_hash": legacy_hash}},
                    expected,
                ),
            )

    def test_different_voice_is_not_reusable(self):
        session_id, _revision_id, segment_id = self._plan_case()
        expected = self._identity(self._resolved(session_id, copy.deepcopy(OVERRIDE)), segment_id)
        other_override = copy.deepcopy(OVERRIDE)
        other_override["tts"]["voice"] = "voice-b"
        other = self._identity(self._resolved(session_id, other_override), segment_id)
        self.assertNotEqual(expected["settings_hash"], other["settings_hash"])
        stored = identity_for_storage(expected)
        for legacy_hash in other["legacy_settings_hashes"]:
            self.assertEqual(
                "generation_settings_changed",
                stored_take_reuse_reason(
                    "completed",
                    "completed",
                    "current",
                    {IDENTITY_KEY: {**stored, "settings_hash": legacy_hash}},
                    expected,
                ),
            )

    def test_plan_identities_keep_legacy_hashes(self):
        session_id, revision_id, segment_id = self._plan_case()
        preview = self._resolved(session_id, copy.deepcopy(OVERRIDE))
        with self.database.session() as session:
            planned = plan_audio_identities(session, revision_id, _run_style(preview))
        self.assertIn(segment_id, planned)
        self.assertEqual(2, len(planned[segment_id]["legacy_settings_hashes"]))

    def test_run_path_accepts_a_pre_fix_stamped_take(self):
        session_id, revision_id, segment_id = self._plan_case()
        preview = self._resolved(session_id, copy.deepcopy(OVERRIDE))
        with self.database.session() as session:
            expected = plan_audio_identities(session, revision_id, _run_style(preview))[segment_id]
        stamped = {
            IDENTITY_KEY: {
                **identity_for_storage(expected),
                "settings_hash": expected["legacy_settings_hashes"][0],
            }
        }
        self.assertEqual(
            "reusable",
            stored_take_reuse_reason("completed", "completed", "current", stamped, expected),
        )

    def test_identity_matches_accepts_legacy_hashes_on_either_side(self):
        base = {
            "schema_version": 1,
            "settings_hash": "a" * 64,
            "voice_reference_hash": "b" * 64,
            "performance_request_hash": "c" * 64,
        }
        legacy = "d" * 64
        current = {**base, "legacy_settings_hashes": [legacy, "e" * 64]}
        old_stored = {**base, "settings_hash": legacy}
        self.assertTrue(identity_matches(old_stored, current))
        self.assertTrue(identity_matches(current, old_stored))
        self.assertFalse(
            identity_matches({**old_stored, "performance_request_hash": "9" * 64}, current)
        )
        self.assertFalse(
            identity_matches({**old_stored, "voice_reference_hash": "f" * 64}, current)
        )
        self.assertFalse(
            identity_matches(
                {**old_stored, "settings_hash": "1" * 64},
                {**base, "settings_hash": "2" * 64, "legacy_settings_hashes": ["3" * 64]},
            )
        )

    def test_stamped_take_metadata_drops_legacy_hashes(self):
        session_id, _revision_id, segment_id = self._plan_case()
        result = self.services["generation"].start(session_id, run_override=copy.deepcopy(OVERRIDE))

        def synthesize(text, _settings, **_kwargs):
            return AudioSegment.silent(duration=20)

        capabilities = TtsCapabilities(batch_synthesis=False, streaming_batch=False)
        handlers = self.services["workflow_handlers"]
        with (
            patch.object(handlers.tts_providers, "synthesize", side_effect=synthesize),
            patch.object(
                handlers.tts_providers,
                "synthesis_capabilities",
                return_value=capabilities,
            ),
        ):
            finished = handlers.run_generation(
                {"generation_run_id": result["id"]},
                lambda *_: None,
                threading.Event(),
            )
        self.assertEqual("completed", finished["status"])
        with self.database.session() as session:
            run = session.get(m.GenerationRun, result["id"])
            frozen = run.settings_snapshot_json["generation_audio_identities"]
            self.assertIn("legacy_settings_hashes", frozen[segment_id])
            takes = list(
                session.execute(
                    select(m.AudioTake, m.Artifact)
                    .join(m.Artifact, m.Artifact.id == m.AudioTake.artifact_id)
                    .where(m.AudioTake.generation_run_id == result["id"])
                )
            )
            self.assertTrue(takes)
            for take, artifact in takes:
                identity = artifact.metadata_json[IDENTITY_KEY]
                self.assertNotIn("legacy_settings_hashes", identity)
                self.assertEqual(
                    identity_for_storage(frozen[take.generation_segment_id]),
                    identity,
                )

    def test_rebuilt_plan_reuses_takes_stamped_with_the_legacy_hash(self):
        session_id = self._new_session("legacy take reuse")
        with self.database.session() as session:
            plan = m.GenerationPlan(session_id=session_id)
            session.add(plan)
            session.flush()
            settings_json = {"_source_artifact_id": "narration-source"}
            source_revision = m.GenerationPlanRevision(
                plan_id=plan.id,
                revision_number=1,
                settings_json=settings_json,
                content_hash="source-plan",
            )
            session.add(source_revision)
            session.flush()
            target_revision = m.GenerationPlanRevision(
                plan_id=plan.id,
                parent_revision_id=source_revision.id,
                revision_number=2,
                settings_json=copy.deepcopy(settings_json),
                content_hash="target-plan",
            )
            session.add(target_revision)
            session.flush()
            plan.active_revision_id = target_revision.id
            source_segment = m.GenerationSegment(
                plan_revision_id=source_revision.id,
                ordinal=0,
                source_segment_ids_json=["source-unit-1"],
                alignment_group="group-a",
                node_kind="subtitle_cue",
                text="Source wording 1",
                optimized_text="Spoken wording 1",
                voice="voice-a",
                language="en",
                status="completed",
            )
            target_segment = m.GenerationSegment(
                plan_revision_id=target_revision.id,
                ordinal=0,
                source_segment_ids_json=["source-unit-1"],
                alignment_group="group-a",
                node_kind="subtitle_cue",
                text="Target wording 1",
                optimized_text="Spoken wording 1",
                voice="voice-a",
                language="en",
                status="ready",
            )
            session.add_all([source_segment, target_segment])
            session.flush()
            source_revision_id = source_revision.id
            target_revision_id = target_revision.id
            source_segment_id = source_segment.id
            source_text = source_segment.optimized_text

        preview = self._resolved(session_id, copy.deepcopy(OVERRIDE))
        run_identity = self._identity(_run_style(preview), source_segment_id)
        legacy_hash = run_identity["legacy_settings_hashes"][0]
        stamped = {**identity_for_storage(run_identity), "settings_hash": legacy_hash}

        with self.database.session() as session:
            ownership = resolve_artifact_ownership(session, session_id=session_id)
            relative_path = f"artifacts/legacy-{uuid.uuid4().hex}.wav"
            path = self.root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture audio bytes")
            artifact = m.Artifact(
                workspace_id=ownership.workspace_id,
                project_id=ownership.project_id,
                session_id=session_id,
                kind="audio",
                role="tts",
                relative_path=relative_path,
                mime_type="audio/wav",
                state="current",
                metadata_json={
                    IDENTITY_KEY: stamped,
                    "synthesized_text": source_text,
                },
            )
            session.add(artifact)
            session.flush()
            session.add(
                m.AudioTake(
                    generation_segment_id=source_segment_id,
                    generation_run_id=None,
                    artifact_id=artifact.id,
                    kind="tts",
                    status="completed",
                    settings_hash=legacy_hash,
                    duration_ms=1234,
                    is_active=True,
                    revision=7,
                )
            )
            session.flush()
            receipt = inherit_rebuilt_plan_audio(
                session,
                source_revision_id,
                target_revision_id,
                self._resolved(session_id, copy.deepcopy(OVERRIDE)),
            )
        self.assertEqual(1, receipt["reused_audio_count"])
        self.assertNotIn("generation_settings_changed", receipt["reason_counts"])
