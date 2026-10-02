"""Native automatic-output recovery and assembly compatibility controls."""

import json
import threading
import unittest
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from pydub import AudioSegment
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from pandrator.web import audio_assembly
from pandrator.web.credentials import tts_service_credential_key, upsert_credential
from pandrator.web.media_process import MediaProcessCancelled
from pandrator.web.models import Artifact, ArtifactEdge, AudioTake, UsageEvent
from tests import test_web_workflow_handlers as fixtures


class AutomaticGenerationOutputTests(unittest.TestCase):
    @contextmanager
    def native_case(self):
        case = fixtures.WebWorkflowHandlerTests()
        case.setUp()
        try:
            yield case
        finally:
            case.tearDown()

    def prepare_source(self, case):
        path = case.session_dir / "automatic-output.json"
        content = json.dumps([{"text": "First narration."}, {"text": "Second narration."}]).encode()
        path.write_bytes(content)
        source = case.artifacts.register(
            path, kind="json", role="prepared_text", session_id=case.session.id
        )
        with case.database.session() as db:
            upsert_credential(
                db,
                tts_service_credential_key("elevenlabs"),
                "Synthetic output fixture",
                "synthetic-output-key",
            )
        settings = {
            "service": "ElevenLabs",
            "model": "eleven_multilingual_v2",
            "voice": "fixture-voice",
            "language": "en",
            "max_attempts": 1,
            "tts_concurrent_requests": 1,
            "sentence_silence_ms": 30,
        }
        return source, path, content, settings

    def check_publication_failure(self, stage, expected):
        with self.native_case() as case:
            source, path, content, settings = self.prepare_source(case)
            original_register = case.handlers.artifacts.register

            def register(output_path, **kwargs):
                role = kwargs.get("role")
                if (stage == "take_artifact" and role == "generation_take") or (
                    stage == "output_artifact" and role == "controlled_automatic_audio"
                ):
                    raise RuntimeError("injected " + stage)
                return original_register(output_path, **kwargs)

            def reject_take(db, _context, _instances):
                if any(isinstance(row, AudioTake) for row in db.new):
                    raise RuntimeError("injected take_flush")

            original_assemble = audio_assembly.assemble_audio_plan

            def render_then_fail(*args, **kwargs):
                original_assemble(*args, **kwargs)
                raise RuntimeError("injected assembly_after_render")

            with ExitStack() as stack:
                stack.enter_context(
                    patch(
                        "pandrator.logic.tts_handler.text_to_audio",
                        return_value=AudioSegment.silent(duration=40),
                    )
                )
                stack.enter_context(
                    patch.object(case.handlers.artifacts, "register", side_effect=register)
                )
                if stage == "usage":
                    stack.enter_context(
                        patch.object(
                            case.handlers,
                            "_record_tts_usage",
                            side_effect=RuntimeError("injected usage"),
                        )
                    )
                if stage == "take_flush":
                    event.listen(Session, "before_flush", reject_take)
                    stack.callback(event.remove, Session, "before_flush", reject_take)
                if stage == "assembly_after_render":
                    stack.enter_context(
                        patch(
                            "pandrator.web.audio_assembly.assemble_audio_plan",
                            side_effect=render_then_fail,
                        )
                    )
                with self.assertRaisesRegex(RuntimeError, "injected " + stage):
                    case.handlers._generate_audio(
                        case.session.id,
                        source,
                        path,
                        settings,
                        case.progress,
                        threading.Event(),
                        role="controlled_automatic_audio",
                    )
            with case.database.session() as db:
                artifacts = list(
                    db.scalars(select(Artifact).where(Artifact.role == "generation_take"))
                )
                takes = list(db.scalars(select(AudioTake)))
                usage = list(db.scalars(select(UsageEvent)))
                self.assertEqual(expected[:3], (len(artifacts), len(takes), len(usage)))
                self.assertEqual(expected[3], len(list(case.session_dir.rglob("*.wav"))))
                self.assertTrue(
                    all(take.is_active and take.status == "completed" for take in takes)
                )
                self.assertTrue(all(row.provider_key == "elevenlabs" for row in usage))
                self.assertEqual(
                    0,
                    len(
                        list(
                            db.scalars(
                                select(Artifact).where(
                                    Artifact.role == "controlled_automatic_audio"
                                )
                            )
                        )
                    ),
                )
                artifact_ids = [row.id for row in artifacts]
            for artifact_id in artifact_ids:
                _artifact, take_path = case.artifacts.resolve(artifact_id)
                with take_path.open("rb") as stream:
                    self.assertEqual(40, len(AudioSegment.from_wav(stream)))
            self.assertEqual(content, path.read_bytes())

    def test_failures_retain_independently_committed_audio_and_accounting(self):
        for stage, expected in (
            ("take_artifact", (0, 0, 0, 1)),
            ("usage", (1, 0, 0, 1)),
            ("take_flush", (1, 0, 1, 1)),
            ("assembly_after_render", (2, 2, 2, 3)),
            ("output_artifact", (2, 2, 2, 3)),
        ):
            with self.subTest(stage=stage):
                self.check_publication_failure(stage, expected)

    def test_reporting_failure_keeps_registered_output_and_parent_lineage(self):
        with self.native_case() as case:
            source, path, content, settings = self.prepare_source(case)

            def progress(_value, detail=None):
                if detail == "Audio ready":
                    raise RuntimeError("injected final reporting failure")

            with patch(
                "pandrator.logic.tts_handler.text_to_audio",
                return_value=AudioSegment.silent(duration=40),
            ):
                with self.assertRaisesRegex(RuntimeError, "injected final reporting"):
                    case.handlers._generate_audio(
                        case.session.id,
                        source,
                        path,
                        settings,
                        progress,
                        threading.Event(),
                        role="controlled_automatic_audio",
                    )
            with case.database.session() as db:
                output = db.scalar(
                    select(Artifact).where(Artifact.role == "controlled_automatic_audio")
                )
                takes = list(db.scalars(select(AudioTake)))
                self.assertEqual(2, len(takes))
                self.assertEqual(2, len(list(db.scalars(select(UsageEvent)))))
                parents = set(
                    db.scalars(
                        select(ArtifactEdge.parent_artifact_id).where(
                            ArtifactEdge.child_artifact_id == output.id
                        )
                    )
                )
                self.assertEqual({source.id, *(take.artifact_id for take in takes)}, parents)
                output_id = output.id
            _artifact, output_path = case.artifacts.resolve(output_id)
            with output_path.open("rb") as stream:
                self.assertEqual(110, len(AudioSegment.from_wav(stream)))
            self.assertEqual(content, path.read_bytes())

    def test_assembly_cancellation_preserves_completed_takes_and_paid_usage(self):
        with self.native_case() as case:
            source, path, _content, settings = self.prepare_source(case)
            cancel = threading.Event()

            def cancel_assembly(_plan, _destination, *, cancel_event):
                self.assertIs(cancel, cancel_event)
                cancel.set()
                raise MediaProcessCancelled("injected assembly cancellation")

            with (
                patch(
                    "pandrator.logic.tts_handler.text_to_audio",
                    return_value=AudioSegment.silent(duration=40),
                ),
                patch(
                    "pandrator.web.audio_assembly.assemble_audio_plan", side_effect=cancel_assembly
                ),
            ):
                result = case.handlers._generate_audio(
                    case.session.id,
                    source,
                    path,
                    settings,
                    case.progress,
                    cancel,
                    role="controlled_automatic_audio",
                )
            self.assertEqual({}, result)
            with case.database.session() as db:
                self.assertEqual(2, len(list(db.scalars(select(AudioTake)))))
                self.assertEqual(2, len(list(db.scalars(select(UsageEvent)))))
                self.assertIsNone(
                    db.scalar(select(Artifact).where(Artifact.role == "controlled_automatic_audio"))
                )

    def test_roles_fade_aliases_and_last_pause_preserve_native_assembly(self):
        for role, filename in (
            ("audiobook_audio", "audiobook_audio.wav"),
            ("dubbing_audio", "dubbing_audio.wav"),
        ):
            with self.subTest(role=role), self.native_case() as case:
                source, path, _content, settings = self.prepare_source(case)
                settings.update(enable_fade=True, fade_in_duration=2, fade_out_duration=4)
                with (
                    patch(
                        "pandrator.logic.tts_handler.text_to_audio",
                        return_value=AudioSegment.silent(duration=40),
                    ),
                    patch(
                        "pandrator.web.audio_assembly.build_audio_assembly_plan",
                        wraps=audio_assembly.build_audio_assembly_plan,
                    ) as build_plan,
                ):
                    result = case.handlers._generate_audio(
                        case.session.id,
                        source,
                        path,
                        settings,
                        case.progress,
                        threading.Event(),
                        role=role,
                    )
                parts = build_plan.call_args.args[0]
                self.assertEqual([30, 0], [part.silence_after_ms for part in parts])
                self.assertEqual(
                    [(2, 4), (2, 4)], [(part.fade_in_ms, part.fade_out_ms) for part in parts]
                )
                artifact, output_path = case.artifacts.resolve(result["artifact_id"])
                self.assertEqual(filename, output_path.name)
                self.assertEqual(role, artifact.role)
                self.assertEqual(110, artifact.metadata_json["duration_ms"])
                with output_path.open("rb") as stream:
                    self.assertEqual(110, len(AudioSegment.from_wav(stream)))
