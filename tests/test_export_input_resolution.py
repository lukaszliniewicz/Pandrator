"""Queued exports retain their selected inputs and reject invalid sources early."""

import tempfile
import threading
import unittest
import wave
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest import mock

from sqlalchemy import func, select

from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.export_audio_version import (
    MESSAGE_CANDIDATE_LIMIT,
    ambiguous_versions_message,
    describe_export_audio,
)
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

    def test_unpinned_export_uses_the_assembly_of_the_active_speech_plan(self):
        """A timing-only change is assembled again without a run; no run names it."""

        first = self.prepare_audiobook()
        second = self.session_dir / "assembled-current-takes.wav"
        with wave.open(str(second), "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\x01\x00" * 320)
        current = self.artifacts.register(
            second,
            kind="audio",
            role="assembled_audio",
            session_id=self.session.id,
        )
        with self.database.session() as session:
            plan = GenerationPlan(session_id=self.session.id)
            session.add(plan)
            session.flush()
            revisions = []
            for number in (1, 2):
                revision = GenerationPlanRevision(
                    plan_id=plan.id,
                    revision_number=number,
                    settings_json={},
                    content_hash=f"assembly-revision-{number}",
                )
                session.add(revision)
                session.flush()
                revisions.append(revision.id)
            plan.active_revision_id = revisions[1]
            older = session.scalar(
                select(Artifact).where(
                    Artifact.session_id == self.session.id,
                    Artifact.role == "assembled_audio",
                    Artifact.id != current.id,
                )
            )
            for artifact_id in (older.id, current.id):
                session.add(
                    OutputAssembly(
                        session_id=self.session.id,
                        status="completed",
                        artifact_id=artifact_id,
                        settings_json={"plan_revision_id": revisions[1]},
                    )
                )
        payload = self.queued_payload()
        payload.pop("export_contract")
        # Two assemblies of the active plan are still a choice the caller makes.
        self.assert_rejected_before_output(
            payload,
            "Multiple assembled audio versions are available",
        )
        with self.database.session() as session:
            older_assembly = session.scalar(
                select(OutputAssembly).where(OutputAssembly.artifact_id == older.id)
            )
            older_assembly.settings_json = {"plan_revision_id": revisions[0]}
        # A plan that measured the older assembly must not render the one a
        # later timing edit made current while the export was queued.
        self.assert_rejected_before_output(
            {**payload, "expected_assembly_artifact_id": older.id},
            "planned with is no longer current",
        )
        result = self.handlers.export(
            {**payload, "expected_assembly_artifact_id": current.id},
            mock.Mock(),
            threading.Event(),
        )
        _exported, output = self.artifacts.resolve(result["artifact_ids"][0])
        self.assertEqual(second.read_bytes(), output.read_bytes())
        self.assertNotEqual(first.read_bytes(), output.read_bytes())

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

    def add_plan_with_revisions(self, count=2):
        with self.database.session() as session:
            plan = GenerationPlan(session_id=self.session.id)
            session.add(plan)
            session.flush()
            ids = []
            for number in range(1, count + 1):
                revision = GenerationPlanRevision(
                    plan_id=plan.id,
                    revision_number=number,
                    settings_json={},
                    content_hash=f"describe-revision-{number}",
                )
                session.add(revision)
                session.flush()
                ids.append(revision.id)
            plan.active_revision_id = ids[-1]
        return ids

    def add_assembly(
        self, name, revision_id, *, minutes, run_number=None, store_revision=True
    ):
        """An assembled audio artifact, its assembly and (optionally) its run."""

        path = self.session_dir / f"{name}.wav"
        with wave.open(str(path), "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(minutes.to_bytes(2, "little", signed=True) * 320)
        artifact = self.artifacts.register(
            path, kind="audio", role="assembled_audio", session_id=self.session.id
        )
        moment = datetime(2026, 11, 21, 10, 0, tzinfo=UTC) + timedelta(minutes=minutes)
        with self.database.session() as session:
            run_id = None
            if run_number is not None:
                run = GenerationRun(
                    session_id=self.session.id,
                    plan_revision_id=revision_id,
                    sequence_number=run_number,
                    status="completed",
                )
                session.add(run)
                session.flush()
                run_id = run.id
            assembly = OutputAssembly(
                session_id=self.session.id,
                generation_run_id=run_id,
                status="completed",
                artifact_id=artifact.id,
                settings_json=(
                    {"plan_revision_id": revision_id} if store_revision else {}
                ),
                created_at=moment,
            )
            session.add(assembly)
            session.flush()
            return artifact, assembly.id, run_id, path

    def new_audiobook_session(self, title):
        self.session = SessionService(self.database).create(
            title, workflow_kind="audiobook"
        )
        self.session_dir = self.paths.sessions / self.session.storage_key
        self.session_dir.mkdir()

    def describe(self):
        with self.database.session() as session:
            return describe_export_audio(session, self.session.id)

    def unpinned_payload(self):
        payload = self.queued_payload()
        payload.pop("export_contract")
        return payload

    def test_export_audio_names_the_active_assembly_among_older_ones(self):
        for older_first in (True, False):
            with self.subTest(older_first=older_first):
                self.new_audiobook_session(f"Active among older {older_first}")
                revisions = self.add_plan_with_revisions()
                specs = [("older", revisions[0], 5, 1), ("active", revisions[1], 9, 2)]
                for name, revision, minutes, number in (
                    specs if older_first else reversed(specs)
                ):
                    made = self.add_assembly(
                        name, revision, minutes=minutes, run_number=number
                    )
                    if name == "active":
                        active = made
                description = self.describe()
                self.assertEqual("named", description["status"])
                self.assertEqual([], description["candidates"])
                version = description["version"]
                self.assertEqual(active[0].id, version["artifact_id"])
                self.assertEqual(active[1], version["assembly_id"])
                self.assertEqual(active[2], version["generation_run_id"])
                self.assertEqual(2, version["run_sequence_number"])
                self.assertEqual(revisions[1], version["plan_revision_id"])
                self.assertEqual(2, version["plan_revision_number"])
                self.assertTrue(version["active_revision"])
                # Stored times come back naive from SQLite; the field says UTC.
                self.assertEqual("2026-11-21T10:09:00+00:00", version["assembled_at"])
                result = self.handlers.export(
                    self.unpinned_payload(), mock.Mock(), threading.Event()
                )
                _exported, output = self.artifacts.resolve(result["artifact_ids"][0])
                self.assertEqual(active[3].read_bytes(), output.read_bytes())

    def test_export_audio_names_a_single_assembly_of_another_revision(self):
        self.new_audiobook_session("Single")
        revisions = self.add_plan_with_revisions()
        made = self.add_assembly("single", revisions[0], minutes=3)
        description = self.describe()
        self.assertEqual("named", description["status"])
        self.assertEqual(made[0].id, description["version"]["artifact_id"])
        self.assertFalse(description["version"]["active_revision"])
        self.assertEqual(1, description["version"]["plan_revision_number"])
        self.assertIsNone(description["version"]["generation_run_id"])
        self.assertIsNone(description["version"]["run_sequence_number"])

    def test_export_audio_lists_candidates_when_none_is_singled_out(self):
        for label, placements in (
            ("two on the active revision", ((1, 4), (1, 7))),
            ("two on others, none on the active one", ((0, 4), (0, 7))),
        ):
            with self.subTest(label):
                self.new_audiobook_session(label)
                revisions = self.add_plan_with_revisions(3 if placements[0][0] == 0 else 2)
                made = [
                    self.add_assembly(
                        f"candidate-{index}",
                        revisions[revision],
                        minutes=minutes,
                        run_number=index + 1,
                    )
                    for index, (revision, minutes) in enumerate(placements)
                ]
                description = self.describe()
                self.assertEqual("ambiguous", description["status"])
                self.assertIsNone(description["version"])
                self.assertEqual(
                    [made[1][0].id, made[0][0].id],
                    [item["artifact_id"] for item in description["candidates"]],
                )
                with self.assertRaises(ValueError) as refused:
                    self.handlers.export(
                        self.unpinned_payload(), mock.Mock(), threading.Event()
                    )
                message = str(refused.exception)
                self.assertTrue(message.startswith(
                    "Multiple assembled audio versions are available. Select a "
                    "completed audio version and export again."
                ))
                for item in description["candidates"]:
                    self.assertIn(item["generation_run_id"], message)
                    self.assertIn(item["assembled_at"], message)
                    self.assertIn(f"run {item['run_sequence_number']} (", message)
                    self.assertIn(f"plan revision {item['plan_revision_number']}", message)
                self.assertLess(
                    message.index(made[1][2]),
                    message.index(made[0][2]),
                    "newest candidate first",
                )
                # Neither the description nor the refusal names a file or path.
                combined = str(description) + message
                self.assertNotIn(".wav", combined)
                self.assertNotIn(str(self.paths.sessions), combined)

    def test_refusal_lists_the_newest_candidates_and_counts_the_rest(self):
        candidates = [
            {
                "generation_run_id": f"run-{index}",
                "run_sequence_number": index,
                "assembled_at": f"2026-11-21T10:{index:02d}:00+00:00",
                "plan_revision_number": 1,
            }
            for index in range(MESSAGE_CANDIDATE_LIMIT + 2, 0, -1)
        ]
        message = ambiguous_versions_message(candidates)
        for item in candidates[:MESSAGE_CANDIDATE_LIMIT]:
            self.assertIn(f"({item['generation_run_id']})", message)
        for item in candidates[MESSAGE_CANDIDATE_LIMIT:]:
            self.assertNotIn(f"({item['generation_run_id']})", message)
        self.assertTrue(message.endswith(
            "; and 2 more, which the export plan and the Output page list."
        ))
        self.assertNotIn("more", ambiguous_versions_message(candidates[:2]))

    def test_export_audio_reports_no_assembly(self):
        self.new_audiobook_session("Empty")
        self.assertEqual(
            {"status": "none", "version": None, "candidates": []}, self.describe()
        )

    def test_candidate_without_a_run_is_listed_as_no_run(self):
        self.new_audiobook_session("No run")
        revisions = self.add_plan_with_revisions()
        self.add_assembly("with-run", revisions[0], minutes=2, run_number=1)
        without_run, *_ = self.add_assembly("without-run", revisions[0], minutes=6)
        with self.assertRaises(ValueError) as refused:
            self.handlers.export(self.unpinned_payload(), mock.Mock(), threading.Event())
        # The artifact id is what removing a run-less candidate needs.
        self.assertRegex(
            str(refused.exception),
            rf"no run \(artifact {without_run.id}\), assembled .*plan revision 1",
        )

    def test_active_revision_follows_the_stored_revision_the_rule_counts(self):
        """A run on the active revision does not make an unstored assembly active."""

        self.new_audiobook_session("Unstored revision")
        revisions = self.add_plan_with_revisions()
        unstored = self.add_assembly(
            "unstored", revisions[1], minutes=8, run_number=2, store_revision=False
        )
        self.add_assembly("older", revisions[0], minutes=4, run_number=1)
        description = self.describe()
        # The rule counts only the stored revision, so nothing is singled out.
        self.assertEqual("ambiguous", description["status"])
        candidate = next(
            item
            for item in description["candidates"]
            if item["artifact_id"] == unstored[0].id
        )
        # The run still fills the revision it belongs to, but it is not "active".
        self.assertEqual(revisions[1], candidate["plan_revision_id"])
        self.assertEqual(2, candidate["plan_revision_number"])
        self.assertFalse(candidate["active_revision"])
