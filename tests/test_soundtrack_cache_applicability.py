"""Cache identity coverage for settings consumed only by mixed soundtracks."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.models import Artifact
from pandrator.web.sessions import SessionService
from pandrator.web.soundtrack_export import MIX_KEYS, ensure_soundtrack_master
from pandrator.web.workflow_output_context import OutputWorkflowContext
from tests.web_test_support import prepare_web_test_data_root

_DEFAULT = object()


class SoundtrackCacheApplicabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="soundtrack-cache-")
        self.addCleanup(temporary.cleanup)
        self.paths = prepare_web_test_data_root(temporary.name)
        self.database = Database(self.paths.database)
        self.addCleanup(self.database.dispose)
        self.artifacts = ArtifactService(self.database, self.paths)
        self.session = SessionService(self.database).create(
            "Soundtrack cache applicability", workflow_kind="voiceover"
        )
        self.session_dir = self.paths.sessions / self.session.storage_key
        self.session_dir.mkdir()

        self.source = self._artifact("source.mp4", b"source sentinel", "video")
        self.speech = self._artifact("speech.wav", b"speech sentinel", "audio")
        self.probe_durations = {
            self.artifacts.resolve(self.source.id)[1]: 3.0,
            self.artifacts.resolve(self.speech.id)[1]: 2.0,
        }
        self.render_calls: list[list[str]] = []
        self.operation_count = 0

        def operation_dir(_session_id: str, stage_key: str) -> Path:
            self.operation_count += 1
            destination = (
                self.session_dir
                / "stage-runs"
                / f"{stage_key}-{self.operation_count}"
            )
            destination.mkdir(parents=True)
            return destination

        self.handler = OutputWorkflowContext(
            database=self.database,
            paths=self.paths,
            artifacts=self.artifacts,
            _resolve_input=self.artifacts.resolve,
            _session_dir=lambda _session_id: self.session_dir,
            _session_record=lambda _session_id: self.session,
            _operation_dir=operation_dir,
        )
        self.settings = {
            "audio_match_source_duration": True,
            "mix_source_gain_db": -3.0,
            "mix_voice_gain_db": 0.0,
            "mix_voice_lufs": -16.0,
            "mix_ducking": "strong",
            "mix_attack_ms": 25,
            "mix_release_ms": 350,
        }

        probe_patch = patch(
            "pandrator.web.soundtrack_export.probe_soundtrack_media",
            side_effect=self._probe,
        )
        render_patch = patch(
            "pandrator.web.soundtrack_export.run_audio_command",
            side_effect=self._render,
        )
        executable_patch = patch(
            "pandrator.web.soundtrack_export._executable",
            side_effect=lambda name: name,
        )
        for patcher in (probe_patch, render_patch, executable_patch):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _artifact(self, name: str, content: bytes, kind: str):
        path = self.session_dir / name
        path.write_bytes(content)
        return self.artifacts.register(
            path,
            kind=kind,
            role="upload" if kind == "video" else "assembled_audio",
            session_id=self.session.id,
        )

    def _probe(self, path: Path, **_kwargs) -> dict[str, object]:
        duration = self.probe_durations[path]
        return {
            "duration": duration,
            "has_audio": True,
            "has_video": path.suffix == ".mp4",
            "video_duration": None,
            "audio_duration": duration,
        }

    def _render(
        self,
        command: list[str],
        cancel_event: threading.Event,
        *,
        before_io=None,
    ) -> None:
        if cancel_event.is_set():
            raise InterruptedError("Soundtrack export cancelled.")
        if before_io is not None:
            before_io()
        Path(command[-1]).write_bytes(f"render-{len(self.render_calls)}".encode())
        self.render_calls.append(command)

    def _master(
        self,
        *,
        audio_mode: str = "mixed",
        settings: dict | None = None,
        source=_DEFAULT,
        speech=_DEFAULT,
        tail_extension_ms: int = 0,
    ):
        source = self.source if source is _DEFAULT else source
        speech = self.speech if speech is _DEFAULT else speech
        assert source is None or isinstance(source, Artifact)
        assert speech is None or isinstance(speech, Artifact)
        return ensure_soundtrack_master(
            self.handler,
            session_id=self.session.id,
            source=source,
            speech=speech,
            audio_mode=audio_mode,
            settings=self.settings if settings is None else settings,
            cancel_event=threading.Event(),
            tail_extension_ms=tail_extension_ms,
        )

    def _changed_mix_value(self, key: str):
        value = self.settings[key]
        if isinstance(value, (int, float)):
            return value + 1
        return "light" if value == "strong" else "strong"

    def test_source_mode_reuses_master_when_all_mix_settings_change(self):
        master = self._master(audio_mode="source")
        definition = master.metadata_json["soundtrack"]
        self.assertEqual(2, definition["version"])

        for key in MIX_KEYS:
            with self.subTest(key=key):
                changed = {**self.settings, key: self._changed_mix_value(key)}
                reused = self._master(audio_mode="source", settings=changed)
                self.assertEqual(master.id, reused.id)
                self.assertEqual(1, len(self.render_calls))
        self.assertEqual({}, definition["mix"])

    def test_dubbed_mode_reuses_master_when_all_mix_settings_change(self):
        master = self._master(audio_mode="dubbed")
        definition = master.metadata_json["soundtrack"]
        self.assertEqual(2, definition["version"])

        for key in MIX_KEYS:
            with self.subTest(key=key):
                changed = {**self.settings, key: self._changed_mix_value(key)}
                reused = self._master(audio_mode="dubbed", settings=changed)
                self.assertEqual(master.id, reused.id)
                self.assertEqual(1, len(self.render_calls))
        self.assertEqual({}, definition["mix"])

    def test_mixed_mode_keys_mix_settings_but_ignores_final_export_preferences(self):
        baseline = self._master()
        definition = baseline.metadata_json["soundtrack"]
        self.assertEqual(2, definition["version"])
        self.assertEqual(set(MIX_KEYS), set(definition["mix"]))
        self.assertEqual(1, len(self.render_calls))

        for index, key in enumerate(MIX_KEYS, start=1):
            with self.subTest(key=key):
                changed = {**self.settings, key: self._changed_mix_value(key)}
                rendered = self._master(settings=changed)
                self.assertNotEqual(baseline.id, rendered.id)
                self.assertEqual(index + 1, len(self.render_calls))
                self.assertEqual(rendered.id, self._master(settings=changed).id)
                self.assertEqual(index + 1, len(self.render_calls))

        final_export_changes = {
            **self.settings,
            "bitrate": "192k",
            "subtitle_format": "srt",
            "export_mode": "media",
            "format": "mp3",
        }
        reused = self._master(settings=final_export_changes)
        self.assertEqual(baseline.id, reused.id)
        self.assertEqual(1 + len(MIX_KEYS), len(self.render_calls))

    def test_source_speech_mode_match_and_tail_inputs_remain_in_master_identity(self):
        baseline = self._master()
        alternate_source = self._artifact(
            "alternate-source.mp4", b"different source sentinel", "video"
        )
        self.probe_durations[self.artifacts.resolve(alternate_source.id)[1]] = 3.0
        source_changed = self._master(source=alternate_source)
        self.assertNotEqual(baseline.id, source_changed.id)

        alternate_speech = self._artifact(
            "alternate-speech.wav", b"different speech sentinel", "audio"
        )
        self.probe_durations[self.artifacts.resolve(alternate_speech.id)[1]] = 2.0
        speech_changed = self._master(speech=alternate_speech)
        self.assertNotEqual(baseline.id, speech_changed.id)

        mode_changed = self._master(audio_mode="dubbed")
        self.assertNotEqual(baseline.id, mode_changed.id)

        match_changed = self._master(
            settings={**self.settings, "audio_match_source_duration": False}
        )
        self.assertNotEqual(baseline.id, match_changed.id)

        tail_extended = self._master(tail_extension_ms=500)
        self.assertNotEqual(baseline.id, tail_extended.id)
        self.assertEqual(168_000, tail_extended.metadata_json["soundtrack"]["samples"])
        self.assertEqual(500, tail_extended.metadata_json["soundtrack"]["tail_extension_ms"])

    def test_generated_duration_change_invalidates_target_samples(self):
        baseline = self._master(audio_mode="dubbed", source=None)
        self.assertEqual(96_000, baseline.metadata_json["soundtrack"]["samples"])

        speech_path = self.artifacts.resolve(self.speech.id)[1]
        self.probe_durations[speech_path] = 2.25
        changed = self._master(audio_mode="dubbed", source=None)
        self.assertNotEqual(baseline.id, changed.id)
        self.assertEqual(108_000, changed.metadata_json["soundtrack"]["samples"])
        self.assertEqual(2, len(self.render_calls))


if __name__ == "__main__":
    unittest.main()
