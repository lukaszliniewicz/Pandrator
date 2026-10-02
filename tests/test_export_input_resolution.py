"""Queued exports retain their selected inputs and reject invalid sources early."""

import tempfile
import threading
import unittest
import wave
from copy import deepcopy
from unittest import mock

from sqlalchemy import func, select

from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.export_contract import build_export_contract
from pandrator.web.models import (
    Artifact,
    ArtifactEdge,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    OutputAssembly,
)
from pandrator.web.output_settings_snapshot import build_output_settings_snapshot
from pandrator.web.sessions import SessionService
from pandrator.web.settings_policy import stable_hash
from pandrator.web.source_resolution import resolve_media_source
from pandrator.web.workflow_handlers import WorkflowHandlers
from pandrator.web.workspace import SourceLibraryService
from tests.web_test_support import prepare_web_test_data_root


class OutputSettingsSnapshotTests(unittest.TestCase):
    def test_flat_runtime_overrides_update_resolved_output_snapshot(self):
        settings = {
            "export_mode": "subtitles",
            "subtitle_mode": "translation",
            "subtitle_format": "vtt",
            "silence_between_sentences": 500,
            "sentence_silence_ms": 100,
            "subtitle_max_lines": 3,
            "max_lines": 1,
            "subtitle_max_chars_per_line": 42,
            "max_chars_per_line": 60,
            "api_key": "must-not-be-captured",
            "xtts_model": "unrelated-tts-setting",
        }
        resolved_snapshot = {
            "output": {
                "export_mode": "media",
                "subtitle_mode": "none",
                "subtitle_format": "srt",
                "video_transcode": True,
                "resolved_marker": {"kept": True},
            },
            "audio": {
                "sentence_silence_ms": 250,
                "paragraph_silence_ms": 800,
                "resolved_marker": {"kept": True},
            },
            "subtitles": {
                "max_lines": 2,
                "max_chars_per_line": 56,
                "language_defaults": False,
                "resolved_marker": {"kept": True},
            },
        }
        original_settings = deepcopy(settings)
        original_resolved_snapshot = deepcopy(resolved_snapshot)

        snapshot = build_output_settings_snapshot(settings, resolved_snapshot)
        sections = snapshot["sections"]

        self.assertEqual("subtitles", sections["output"]["export_mode"])
        self.assertEqual("translation", sections["output"]["subtitle_mode"])
        self.assertEqual("vtt", sections["output"]["subtitle_format"])
        self.assertTrue(sections["output"]["video_transcode"])
        self.assertEqual({"kept": True}, sections["output"]["resolved_marker"])
        self.assertEqual(500, sections["audio"]["sentence_silence_ms"])
        self.assertEqual(800, sections["audio"]["paragraph_silence_ms"])
        self.assertEqual({"kept": True}, sections["audio"]["resolved_marker"])
        self.assertEqual(3, sections["subtitles"]["max_lines"])
        self.assertEqual(42, sections["subtitles"]["max_chars_per_line"])
        self.assertFalse(sections["subtitles"]["language_defaults"])
        self.assertEqual({"kept": True}, sections["subtitles"]["resolved_marker"])
        self.assertNotIn("api_key", str(sections))
        self.assertNotIn("xtts_model", str(sections))
        self.assertEqual(stable_hash(sections), snapshot["settings_hash"])
        self.assertNotEqual(
            build_output_settings_snapshot({}, resolved_snapshot)["settings_hash"],
            snapshot["settings_hash"],
        )
        self.assertEqual(original_settings, settings)
        self.assertEqual(original_resolved_snapshot, resolved_snapshot)


class ExportInputResolutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = prepare_web_test_data_root(temporary.name)
        self.database = Database(self.paths.database)
        self.addCleanup(self.database.dispose)
        self.artifacts = ArtifactService(self.database, self.paths)
        self.sources = SourceLibraryService(self.database, self.artifacts)
        self.handlers = WorkflowHandlers(self.database, self.paths)
        self.session = SessionService(self.database).create(
            "Queued source", workflow_kind="voiceover"
        )
        self.session_dir = self.paths.sessions / self.session.storage_key
        self.session_dir.mkdir()

    def attach_audio(self, name, sample):
        path = self.session_dir / name
        with wave.open(str(path), "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(sample.to_bytes(2, "little", signed=True) * 320)
        artifact = self.artifacts.register(
            path, kind="source", role="upload", session_id=self.session.id
        )
        asset = self.sources.ensure_for_artifact(artifact.id)
        self.sources.attach(self.session.id, asset.id)
        return artifact, path

    def queued_payload(self):
        settings = {"export_mode": "media", "audio_mode": "preserve"}
        with self.database.session() as session:
            contract = build_export_contract(
                workflow_kind=self.session.workflow_kind,
                settings=settings,
                source=resolve_media_source(session, self.session.id),
            )
        return {
            "session_id": self.session.id,
            "settings": settings,
            "export_contract": contract,
        }

    def test_worker_exports_pinned_source_after_another_source_is_attached(self):
        original, original_path = self.attach_audio("original.wav", 100)
        payload = self.queued_payload()
        replacement, replacement_path = self.attach_audio("replacement.wav", 200)
        with self.database.session() as session:
            self.assertEqual(
                replacement.id, resolve_media_source(session, self.session.id).artifact.id
            )

        result = self.handlers.export(payload, mock.Mock(), threading.Event())

        self.assertEqual(1, len(result["artifact_ids"]))
        exported, output_path = self.artifacts.resolve(result["artifact_ids"][0])
        self.assertEqual(original_path.read_bytes(), output_path.read_bytes())
        self.assertNotEqual(replacement_path.read_bytes(), output_path.read_bytes())
        with self.database.session() as session:
            parents = set(session.scalars(select(ArtifactEdge.parent_artifact_id).where(
                ArtifactEdge.child_artifact_id == exported.id
            )))
        self.assertEqual({original.id}, parents)

    def test_preserve_export_ignores_irrelevant_generation_run_id(self):
        original, original_path = self.attach_audio("preserve-source.wav", 123)
        with self.database.session() as session:
            plan = GenerationPlan(session_id=self.session.id)
            session.add(plan)
            session.flush()
            revision = GenerationPlanRevision(
                plan_id=plan.id,
                revision_number=1,
                settings_json={},
                content_hash="preserve-run",
            )
            session.add(revision)
            session.flush()
            plan.active_revision_id = revision.id
            run = GenerationRun(
                session_id=self.session.id,
                plan_revision_id=revision.id,
                sequence_number=1,
                status="completed",
            )
            session.add(run)
            session.flush()
            run_id = run.id
        payload = self.queued_payload()
        payload["settings"]["generation_run_id"] = run_id

        result = self.handlers.export(payload, mock.Mock(), threading.Event())

        self.assertEqual(1, len(result["artifact_ids"]))
        exported, output_path = self.artifacts.resolve(result["artifact_ids"][0])
        self.assertEqual(original_path.read_bytes(), output_path.read_bytes())
        with self.database.session() as session:
            parents = set(
                session.scalars(
                    select(ArtifactEdge.parent_artifact_id).where(
                        ArtifactEdge.child_artifact_id == exported.id
                    )
                )
            )
        self.assertEqual({original.id}, parents)

    def assert_rejected_before_output(self, payload, message):
        with (
            mock.patch.object(self.handlers, "_resolve_input") as resolve,
            mock.patch.object(self.handlers.artifacts, "register") as register,
            mock.patch("pandrator.web.workflow_export.shutil.copy2") as copy,
            mock.patch("pandrator.web.workflow_export.subprocess.run") as render,
            mock.patch("pandrator.logic.dubbing.audio_sync.media_has_audio_stream") as probe,
        ):
            with self.assertRaisesRegex(ValueError, message):
                self.handlers.export(payload, mock.Mock(), threading.Event())
        resolve.assert_not_called()
        register.assert_not_called()
        copy.assert_not_called()
        render.assert_not_called()
        probe.assert_not_called()
        self.assertFalse((self.session_dir / "exports").exists())
        with self.database.session() as session:
            self.assertEqual(0, session.scalar(
                select(func.count()).select_from(Artifact).where(Artifact.kind == "export")
            ))

    def test_worker_rejects_missing_pinned_source_before_output(self):
        self.attach_audio("original.wav", 100)
        payload = self.queued_payload()
        payload["export_contract"]["source_artifact_id"] = "missing-source"
        self.assert_rejected_before_output(payload, "source.*no longer available")

    def test_worker_rejects_changed_pinned_source_hash_before_output(self):
        artifact, _path = self.attach_audio("original.wav", 100)
        payload = self.queued_payload()
        with self.database.session() as session:
            session.get(Artifact, artifact.id).content_hash = "changed-content-hash"
        self.assert_rejected_before_output(payload, "source.*changed")

    def test_worker_rejects_malformed_contract_before_output(self):
        for malformed in ([], "contract", 1):
            with self.subTest(contract=malformed):
                self.assert_rejected_before_output(
                    {"session_id": self.session.id, "export_contract": malformed},
                    "contract is malformed",
                )

    def prepare_audiobook(self):
        self.session = SessionService(self.database).create(
            "Queued audiobook", workflow_kind="audiobook"
        )
        self.session_dir = self.paths.sessions / self.session.storage_key
        self.session_dir.mkdir()
        path = self.session_dir / "assembled.wav"
        with wave.open(str(path), "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\x00\x00" * 320)
        self.artifacts.register(
            path, kind="audio", role="assembled_audio", session_id=self.session.id
        )
        return path

    def test_audiobook_rejects_incompatible_contract_before_output(self):
        self.prepare_audiobook()
        for field, value, message in (
            ("version", 999, "contract version is unsupported"),
            ("workflow_kind", "voiceover", "does not match this session workflow"),
            ("export_mode", "audio", "mode does not match its immutable export contract"),
        ):
            with self.subTest(field=field):
                payload = self.queued_payload()
                payload["export_contract"][field] = value
                self.assert_rejected_before_output(payload, message)

    def test_unpinned_single_durable_audiobook_assembly_remains_compatible(self):
        original = self.prepare_audiobook()
        with self.database.session() as session:
            artifact = session.scalar(
                select(Artifact)
                .where(
                    Artifact.session_id == self.session.id,
                    Artifact.role == "assembled_audio",
                    Artifact.state == "current",
                )
                .order_by(Artifact.created_at.desc())
            )
            self.assertIsNotNone(artifact)
            session.add(
                OutputAssembly(
                    session_id=self.session.id,
                    status="completed",
                    artifact_id=artifact.id,
                    settings_json={},
                )
            )
        payload = self.queued_payload()
        payload.pop("export_contract")
        result = self.handlers.export(payload, mock.Mock(), threading.Event())
        self.assertEqual(1, len(result["artifact_ids"]))
        _exported, output = self.artifacts.resolve(result["artifact_ids"][0])
        self.assertEqual(original.read_bytes(), output.read_bytes())

    def test_unpinned_audiobook_export_rejects_ambiguous_assemblies(self):
        self.prepare_audiobook()
        second = self.session_dir / "assembled-second.wav"
        with wave.open(str(second), "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\x01\x00" * 320)
        self.artifacts.register(
            second,
            kind="audio",
            role="assembled_audio",
            session_id=self.session.id,
        )
        payload = self.queued_payload()
        payload.pop("export_contract")
        self.assert_rejected_before_output(
            payload,
            "Multiple assembled audio versions are available",
        )

    def test_audiobook_exports_with_valid_contract_or_legacy_payload(self):
        original = self.prepare_audiobook()
        payload = self.queued_payload()
        for with_contract in (True, False):
            with self.subTest(with_contract=with_contract):
                if not with_contract:
                    payload.pop("export_contract")
                result = self.handlers.export(payload, mock.Mock(), threading.Event())
                self.assertEqual(1, len(result["artifact_ids"]))
                _exported, output = self.artifacts.resolve(result["artifact_ids"][0])
                self.assertEqual(original.read_bytes(), output.read_bytes())
