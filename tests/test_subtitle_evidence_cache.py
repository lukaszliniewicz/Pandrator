import hashlib
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import (
    Artifact,
    ArtifactEdge,
    Document,
    DocumentRevision,
    Provider,
    ProviderModel,
    Segment,
    SubtitleEvidence,
)
from pandrator.web.subtitle_evidence import SubtitleEvidenceService
from tests.web_test_support import prepare_web_test_data_root


class SubtitleEvidenceCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        prepare_web_test_data_root(self.temporary.name)
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=BootstrapTokenStore(),
            background_maintenance=False,
        )
        self.services = self.app.extensions["pandrator"]["services"]
        self.session = self.services.sessions.create(
            "Evidence cache", workflow_kind="subtitles"
        )
        source_dir = self.services.paths.sessions / self.session.storage_key
        source_dir.mkdir(parents=True, exist_ok=True)
        self.media_path = source_dir / "media.mp4"
        self.media_path.write_bytes(b"pinned media bytes")
        self.media = self.services.artifacts.register(
            self.media_path,
            kind="mp4",
            role="upload",
            session_id=self.session.id,
        )
        self.subtitle_path = source_dir / "source.srt"
        self.subtitle_path.write_text(
            "1\n00:00:10,000 --> 00:00:12,000\nhello\n", encoding="utf-8"
        )
        self.subtitle = self.services.artifacts.register(
            self.subtitle_path,
            kind="srt",
            role="transcription",
            session_id=self.session.id,
            metadata={},
        )
        with self.services.database.session() as session:
            document = Document(
                session_id=self.session.id,
                stage="transcription",
                language="en",
            )
            session.add(document)
            session.flush()
            revision = DocumentRevision(
                document_id=document.id,
                revision_number=1,
                content_hash="cache-revision-1",
            )
            session.add(revision)
            session.flush()
            segment = Segment(
                revision_id=revision.id,
                ordinal=0,
                start_ms=10_000,
                end_ms=12_000,
                text="hello",
            )
            session.add(segment)
            session.flush()
            document.active_revision_id = revision.id
            session.get(Artifact, self.subtitle.id).metadata_json = {
                "revision_id": revision.id
            }
        self.document_id = document.id
        self.revision_id = revision.id
        self.config_variant = "config-a"
        self.transcription_calls: list[str] = []
        self.fail_next_transcription = False
        self.audio_calls: list[str] = []
        self.audio_settings: dict[str, object] = {"temperature": 0.0}

    def tearDown(self):
        self.services.database.dispose()
        self.temporary.cleanup()

    def _request(
        self,
        *,
        routes=("whisper",),
        audio_model_ids=None,
        source_artifact_id=None,
        padding_before_ms=2_000,
        force_refresh=False,
    ):
        return self.services.subtitle_evidence.request(
            self.session.id,
            {
                "source_artifact_id": source_artifact_id or self.subtitle.id,
                "cue_id": 1,
                "reason": "Verify this cue against its pinned media.",
                "routes": list(routes),
                "audio_model_ids": list(audio_model_ids or []),
                "padding_before_ms": padding_before_ms,
                "force_refresh": force_refresh,
            },
        )

    def _new_revision_source(self, *, language=None):
        with self.services.database.immediate_session() as session:
            document = session.get(Document, self.document_id)
            if language is not None:
                document.language = language
            revision_number = session.scalar(
                select(DocumentRevision.revision_number)
                .where(DocumentRevision.document_id == self.document_id)
                .order_by(DocumentRevision.revision_number.desc())
            )
            revision = DocumentRevision(
                document_id=self.document_id,
                revision_number=int(revision_number or 0) + 1,
                content_hash=f"cache-revision-{int(revision_number or 0) + 1}",
            )
            session.add(revision)
            session.flush()
            session.add(
                Segment(
                    revision_id=revision.id,
                    ordinal=0,
                    start_ms=10_000,
                    end_ms=12_000,
                    text="hello",
                )
            )
            document.active_revision_id = revision.id
            session.flush()

        path = (
            self.services.paths.sessions
            / self.session.storage_key
            / f"revision-{revision.revision_number}.srt"
        )
        path.write_text(
            "1\n00:00:10,000 --> 00:00:12,000\nhello\n", encoding="utf-8"
        )
        return self.services.artifacts.register(
            path,
            kind="srt",
            role="correction",
            session_id=self.session.id,
            parent_ids=[self.media.id],
            metadata={"revision_id": revision.id},
        )

    def _mock_extract(self, _source_path, output_dir, basename, *_args, **_kwargs):
        output = Path(output_dir) / f"{basename}.wav"
        output.write_bytes(b"RIFF bounded audio")
        return str(output)

    def _resolve_stt_settings(self, _session_id, _sections, *, run_override):
        route = run_override["stt"]["stt_engine"]
        settings = {
            "stt_engine": route,
            "stt_language": "en",
            "stt_model": "mock-model",
            "test_option": self.config_variant,
        }
        digest = hashlib.sha256(
            f"{route}:{self.config_variant}".encode("utf-8")
        ).hexdigest()
        return {"stt": settings}, digest

    def _hydrate_stt(self, _database, _paths, settings):
        return {
            **settings,
            "provider_configs": [
                {
                    "id": settings["stt_engine"],
                    "api_key": "cache-test-secret-must-not-persist",
                    "api_base": f"https://example.test/{self.config_variant}",
                }
            ],
        }

    def _mock_transcribe(self, output_dir, _clip_path, settings, **_kwargs):
        route = str(settings["stt_engine"])
        self.transcription_calls.append(route)
        if self.fail_next_transcription:
            self.fail_next_transcription = False
            raise RuntimeError("mock route failure")
        output = Path(output_dir) / "transcript.json"
        output.write_text(
            json.dumps(
                {
                    "schema": "pandrator.transcript.v1",
                    "language": "en",
                    "metadata": {"provider": "mock", "model": "mock-model"},
                    "segments": [
                        {
                            "id": "witness",
                            "start_ms": 2_000,
                            "end_ms": 4_000,
                            "text": f"hello from {route}",
                            "words": [
                                {
                                    "text": f"hello-{route}",
                                    "start_ms": 2_000,
                                    "end_ms": 4_000,
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(
            word_timestamps_path=str(output),
            engine=route,
            compute_backend="cpu",
        )

    def _stt_mocks(self):
        return (
            patch(
                "pandrator.web.subtitle_evidence.extract_audio_excerpt",
                side_effect=self._mock_extract,
            ),
            patch(
                "pandrator.web.subtitle_evidence.transcribe_source_file_with_metadata",
                side_effect=self._mock_transcribe,
            ),
            patch.object(
                self.services.subtitle_evidence.workspace_settings,
                "resolve",
                side_effect=self._resolve_stt_settings,
            ),
            patch(
                "pandrator.web.subtitle_evidence.hydrate_stt_settings",
                side_effect=self._hydrate_stt,
            ),
        )

    def _run(self, created, *, force_refresh=None):
        payload = self.services.jobs.get(created["record"]["job_id"]).payload_json
        return self.services.subtitle_evidence.run_request(
            created["record"]["id"],
            lambda *_args: None,
            threading.Event(),
            force_refresh=(
                payload.get("force_refresh", False)
                if force_refresh is None
                else force_refresh
            ),
        )

    def _add_audio_model(self):
        with self.services.database.session() as session:
            provider = Provider(
                kind="llm",
                provider_key="gemini",
                label="Evidence cache test provider",
                enabled=True,
                base_url="https://example.test/v1",
                options_json={},
            )
            session.add(provider)
            session.flush()
            model = ProviderModel(
                provider_id=provider.id,
                model_id="audio-cache-test",
                is_active=True,
                is_default=True,
                default_temperature=0.0,
                input_modalities_json=["audio", "text"],
                output_modalities_json=["text"],
            )
            session.add(model)
            session.flush()
            return model.id

    def _mock_audio_runtime(self, model_record_id):
        return {
            "record_id": model_record_id,
            "model_id": "audio-cache-test",
            "provider_id": "cache-provider",
            "provider_key": "gemini",
            "provider_label": "Evidence cache provider",
            "canonical_model": "gemini/audio-cache-test",
            "resolved_model": "gemini/audio-cache-test",
            "llm_settings": {
                "temperature": self.audio_settings["temperature"],
                "api_key": "audio-cache-secret-must-not-persist",
                "provider_configs": [
                    {"id": "gemini", "api_key": "nested-secret"}
                ],
            },
            "openai_compatible_custom": False,
        }

    def _mock_audio_transcription(self, _clip, prompt, *_args, **_kwargs):
        self.audio_calls.append(prompt)
        return SimpleNamespace(
            transcript="hello from audio",
            transport_metadata={"transport": "mock"},
            completion=SimpleNamespace(usage={}, cost=None, cost_source=""),
        )

    def test_partial_reuse_scopes_new_record_and_ignores_editorial_resolution(self):
        with self._stt_mocks()[0], self._stt_mocks()[1], self._stt_mocks()[2], self._stt_mocks()[3]:
            first = self._request(routes=("parakeet",))
            first_result = self._run(first)
            original = first_result["candidates"][0]
            self.services.subtitle_evidence.resolve(
                self.session.id,
                first["record"]["id"],
                {"action": "accepted", "candidate_id": original["id"]},
            )

            next_source = self._new_revision_source()
            second = self._request(
                routes=("whisper", "parakeet"),
                source_artifact_id=next_source.id,
            )
            second_result = self._run(second)

        self.assertEqual(["parakeet", "whisper"], self.transcription_calls)
        self.assertEqual("completed", second_result["status"])
        reused = next(item for item in second_result["candidates"] if item["route"] == "parakeet")
        self.assertEqual("whisper", next(item for item in second_result["candidates"] if item["route"] == "whisper")["route"])
        self.assertNotEqual(original["id"], reused["id"])
        self.assertNotEqual(
            original["transcript_artifact_id"], reused["transcript_artifact_id"]
        )
        self.assertEqual(first["record"]["id"], reused["reused_from"]["evidence_id"])
        self.assertNotIn("resolution", reused)
        self.assertEqual(
            "accepted",
            self.services.subtitle_evidence.get(first["record"]["id"])["record"][
                "resolution"
            ]["action"],
        )
        self.assertEqual(
            {},
            self.services.subtitle_evidence.get(second["record"]["id"])["record"][
                "resolution"
            ],
        )
        with self.services.database.session() as session:
            new_record = session.get(SubtitleEvidence, second["record"]["id"])
            new_transcript = session.get(Artifact, reused["transcript_artifact_id"])
            parent_ids = set(
                session.scalars(
                    select(ArtifactEdge.parent_artifact_id).where(
                        ArtifactEdge.child_artifact_id == new_transcript.id
                    )
                ).all()
            )
            self.assertEqual(next_source.id, new_record.source_artifact_id)
            self.assertEqual(next_source.metadata_json["revision_id"], new_record.source_revision_id)
            self.assertEqual(new_record.id, new_transcript.metadata_json["evidence_id"])
            self.assertIn(next_source.id, parent_ids)
            self.assertIn(new_record.clip_artifact_id, parent_ids)

    def test_config_window_media_and_language_changes_miss(self):
        with self._stt_mocks()[0], self._stt_mocks()[1], self._stt_mocks()[2], self._stt_mocks()[3]:
            first = self._request()
            self._run(first)

            self.config_variant = "config-b"
            changed_config = self._request()
            self._run(changed_config)
            self.assertEqual(["whisper", "whisper"], self.transcription_calls)

            self.config_variant = "config-a"
            changed_window = self._request(padding_before_ms=1_000)
            self._run(changed_window)
            self.assertEqual(3, len(self.transcription_calls))

            self.media_path.write_bytes(b"changed pinned media")
            changed_media = self._request()
            self._run(changed_media)
            self.assertEqual(4, len(self.transcription_calls))

            with self.services.database.immediate_session() as session:
                session.get(Document, self.document_id).language = "de"
            changed_language = self._request()
            self._run(changed_language)
            self.assertEqual(5, len(self.transcription_calls))

    def test_failed_candidate_is_not_reused_and_force_refresh_bypasses_cache(self):
        with self._stt_mocks()[0], self._stt_mocks()[1], self._stt_mocks()[2], self._stt_mocks()[3]:
            self.fail_next_transcription = True
            failed = self._request()
            self.assertEqual("failed", self._run(failed)["status"])

            retried = self._request()
            self.assertEqual("completed", self._run(retried)["status"])
            self.assertEqual(["whisper", "whisper"], self.transcription_calls)

            refreshed = self._request(force_refresh=True)
            payload = self.services.jobs.get(refreshed["record"]["job_id"]).payload_json
            self.assertTrue(payload["force_refresh"])
            self.assertEqual("completed", self._run(refreshed)["status"])
            self.assertEqual(["whisper", "whisper", "whisper"], self.transcription_calls)

    def test_cache_does_not_cross_session_boundaries(self):
        with self._stt_mocks()[0], self._stt_mocks()[1], self._stt_mocks()[2], self._stt_mocks()[3]:
            self._run(self._request())
            other = self.services.sessions.create(
                "Separate evidence session", workflow_kind="subtitles"
            )
            other_dir = self.services.paths.sessions / other.storage_key
            other_dir.mkdir(parents=True, exist_ok=True)
            media_path = other_dir / "media.mp4"
            media_path.write_bytes(self.media_path.read_bytes())
            self.services.artifacts.register(
                media_path,
                kind="mp4",
                role="upload",
                session_id=other.id,
            )
            subtitle_path = other_dir / "source.srt"
            subtitle_path.write_text(
                "1\n00:00:10,000 --> 00:00:12,000\nhello\n", encoding="utf-8"
            )
            subtitle = self.services.artifacts.register(
                subtitle_path,
                kind="srt",
                role="transcription",
                session_id=other.id,
                metadata={},
            )
            with self.services.database.session() as session:
                document = Document(
                    session_id=other.id,
                    stage="transcription",
                    language="en",
                )
                session.add(document)
                session.flush()
                revision = DocumentRevision(
                    document_id=document.id,
                    revision_number=1,
                    content_hash="other-session-revision",
                )
                session.add(revision)
                session.flush()
                session.add(
                    Segment(
                        revision_id=revision.id,
                        ordinal=0,
                        start_ms=10_000,
                        end_ms=12_000,
                        text="hello",
                    )
                )
                session.flush()
                document.active_revision_id = revision.id
                session.get(Artifact, subtitle.id).metadata_json = {
                    "revision_id": revision.id
                }
            request = self.services.subtitle_evidence.request(
                other.id,
                {
                    "source_artifact_id": subtitle.id,
                    "cue_id": 1,
                    "reason": "Keep another session's evidence private.",
                    "routes": ["whisper"],
                },
            )
            self._run(request)

        self.assertEqual(["whisper", "whisper"], self.transcription_calls)

    def test_cache_public_metadata_contains_only_hashes_and_model_prompt_changes_miss(self):
        model_id = self._add_audio_model()
        with (
            patch(
                "pandrator.web.subtitle_evidence.extract_audio_excerpt",
                side_effect=self._mock_extract,
            ),
            patch.object(
                self.services.subtitle_evidence,
                "_audio_model_runtime",
                side_effect=self._mock_audio_runtime,
            ),
            patch(
                "pandrator.web.subtitle_evidence.transcribe_audio_evidence",
                side_effect=self._mock_audio_transcription,
            ),
        ):
            first = self._request(routes=("audio_llm",), audio_model_ids=(model_id,))
            first_result = self._run(first)
            reused = self._request(routes=("audio_llm",), audio_model_ids=(model_id,))
            reused_result = self._run(reused)
            self.assertEqual(1, len(self.audio_calls))

            with patch.object(
                self.services.subtitle_evidence,
                "_audio_prompt",
                return_value="changed prompt text",
            ):
                prompt_changed = self._request(
                    routes=("audio_llm",), audio_model_ids=(model_id,)
                )
                self._run(prompt_changed)
                self.assertEqual(2, len(self.audio_calls))

                self.audio_settings["temperature"] = 0.5
                model_settings_changed = self._request(
                    routes=("audio_llm",), audio_model_ids=(model_id,)
                )
                self._run(model_settings_changed)
                self.assertEqual(3, len(self.audio_calls))

        candidate = reused_result["candidates"][0]
        self.assertNotEqual(
            first_result["candidates"][0]["id"], candidate["id"]
        )
        self.assertTrue(re.search(r"^[a-f0-9]{64}$", candidate["cache"]["key_sha256"]))
        self.assertTrue(
            re.search(r"^[a-f0-9]{64}$", candidate["cache"]["configuration_sha256"])
        )
        serialized = json.dumps(reused_result["candidates"])
        self.assertNotIn("cache-test-secret-must-not-persist", serialized)
        self.assertNotIn("audio-cache-secret-must-not-persist", serialized)
        self.assertNotIn("nested-secret", serialized)

        safe_provider_config = SubtitleEvidenceService._fingerprint_value(
            {
                "api_base": (
                    "https://user:pass@example.test/v1?api_key=key-secret"
                    "&access_token=token-secret&version=v2"
                )
            }
        )
        safe_provider_config_text = json.dumps(safe_provider_config)
        self.assertNotIn("user:pass", safe_provider_config_text)
        self.assertNotIn("key-secret", safe_provider_config_text)
        self.assertNotIn("token-secret", safe_provider_config_text)
        self.assertIn("version=v2", safe_provider_config_text)


if __name__ == "__main__":
    unittest.main()
