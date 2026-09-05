import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select

from pandrator.logic.dubbing import cloud_stt
from pandrator.logic.dubbing.transcription import (
    ExternalToolError,
    extract_audio_excerpt,
)
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import Document, DocumentRevision, Segment, SubtitleEvidence
from pandrator.web.schemas import (
    SubtitleEvidenceCreateRequest,
    SubtitleEvidenceResolveRequest,
)
from pandrator.web.source_resolution import resolve_primary_source
from tests.web_test_support import prepare_web_test_data_root


class SubtitleEvidenceBackendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        prepare_web_test_data_root(self.temporary.name)
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=bootstrap,
            background_maintenance=False,
        )
        self.client = self.app.test_client()
        csrf = self.client.post(
            "/api/v1/auth/bootstrap", json={"token": token}
        ).get_json()["csrf_token"]
        self.headers = {"X-CSRF-Token": csrf}
        self.services = self.app.extensions["pandrator"]["services"]
        self.session = self.services.sessions.create(
            "Evidence", workflow_kind="subtitles"
        )
        self.subtitle_path = (
            self.services.paths.sessions / self.session.storage_key / "source.srt"
        )
        self.subtitle_path.parent.mkdir(parents=True, exist_ok=True)
        self.subtitle_path.write_text("1\n00:00:10,000 --> 00:00:12,000\nhello\n")
        self.subtitle = self.services.artifacts.register(
            self.subtitle_path,
            kind="srt",
            role="transcription",
            session_id=self.session.id,
            metadata={},
        )
        self.media_path = (
            self.services.paths.sessions / self.session.storage_key / "media.mp4"
        )
        self.media_path.write_bytes(b"not-decoded-by-unit-test")
        self.media = self.services.artifacts.register(
            self.media_path,
            kind="mp4",
            role="upload",
            session_id=self.session.id,
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
                content_hash="revision-hash",
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
            stored = session.get(type(self.subtitle), self.subtitle.id)
            stored.metadata_json = {"revision_id": revision.id}

        self.revision_id = revision.id

    def tearDown(self):
        self.services.database.dispose()
        self.temporary.cleanup()

    def test_create_validates_revision_cue_and_enqueue_shape(self):
        response = self.client.post(
            f"/api/v1/sessions/{self.session.id}/subtitle-evidence",
            json={
                "source_artifact_id": self.subtitle.id,
                "cue_id": 1,
                "reason": "The cue is inaccurate.",
                "routes": ["whisper", "moss"],
            },
            headers=self.headers,
        )
        self.assertEqual(202, response.status_code, response.get_json())
        payload = response.get_json()
        record = payload["record"]
        self.assertEqual(8_000, record["clip_start_ms"])
        self.assertEqual(14_000, record["clip_end_ms"])
        self.assertEqual(self.media.id, record["source_media_artifact_id"])
        self.assertEqual(
            {"evidence_id", "session_id"},
            set(
                payload["job"]["id"]
                and self.services.jobs.get(record["job_id"]).payload_json
            ),
        )
        self.assertEqual("subtitle.evidence", payload["job"]["kind"])
        self.assertIn(
            f"subtitle-evidence:{record['id']}",
            self.services.jobs.get(record["job_id"]).resource_keys_json,
        )

        bad = self.client.post(
            f"/api/v1/sessions/{self.session.id}/subtitle-evidence",
            json={
                "source_artifact_id": self.subtitle.id,
                "cue_id": 2,
                "reason": "missing cue",
                "routes": ["whisper"],
            },
            headers=self.headers,
        )
        self.assertEqual(422, bad.status_code)

    def test_worker_uses_media_snapshot_even_if_primary_source_changes(self):
        created = self.services.subtitle_evidence.request(
            self.session.id,
            {
                "source_artifact_id": self.subtitle.id,
                "cue_id": 1,
                "reason": "Pin the exact media used by this request.",
                "routes": ["whisper"],
            },
        )
        replacement_path = (
            self.services.paths.sessions / self.session.storage_key / "replacement.mp4"
        )
        replacement_path.write_bytes(b"new-primary")
        replacement = self.services.artifacts.register(
            replacement_path,
            kind="mp4",
            role="upload",
            session_id=self.session.id,
        )

        with self.services.database.session() as session:
            self.assertEqual(
                replacement.id,
                resolve_primary_source(session, self.session.id).artifact.id,
            )
            evidence = session.get(SubtitleEvidence, created["record"]["id"])
            pinned = self.services.subtitle_evidence._pinned_media(session, evidence)

        self.assertEqual(self.media.id, pinned.id)

    def test_failure_persists_candidates_completed_before_cancellation(self):
        created = self.services.subtitle_evidence.request(
            self.session.id,
            {
                "source_artifact_id": self.subtitle.id,
                "cue_id": 1,
                "reason": "Retain completed witnesses.",
                "routes": ["whisper", "moss"],
            },
        )
        evidence_id = created["record"]["id"]
        candidate = {
            "id": "whisper-1",
            "route": "whisper",
            "status": "success",
            "text": "Co-chair.",
        }
        self.services.subtitle_evidence._set_failure(
            evidence_id,
            "Evidence transcription was canceled.",
            candidates=[candidate],
        )

        stored = self.services.subtitle_evidence.get(evidence_id)["record"]
        self.assertEqual("failed", stored["status"])
        self.assertEqual([candidate], stored["candidates"])

    def test_audio_witness_is_untimed_and_uses_the_pinned_media(self):
        provider = self.client.post(
            "/api/v1/providers",
            json={"provider_key": "gemini", "label": "Evidence Gemini"},
            headers=self.headers,
        ).get_json()
        audio_model = self.client.post(
            f"/api/v1/providers/{provider['id']}/models",
            json={
                "model_id": "audio-witness",
                "is_active": True,
                "input_modalities": ["text", "audio"],
            },
            headers=self.headers,
        ).get_json()
        created = self.services.subtitle_evidence.request(
            self.session.id,
            {
                "source_artifact_id": self.subtitle.id,
                "cue_id": 1,
                "reason": "Listen to the bounded source clip.",
                "routes": ["audio_llm"],
                "audio_model_ids": [audio_model["id"]],
            },
        )

        replacement_path = (
            self.services.paths.sessions / self.session.storage_key / "new-primary.mp4"
        )
        replacement_path.write_bytes(b"new-primary")
        self.services.artifacts.register(
            replacement_path,
            kind="mp4",
            role="upload",
            session_id=self.session.id,
        )
        extracted_from: list[Path] = []

        def fake_extract(source_path, output_dir, basename, *_args, **_kwargs):
            extracted_from.append(Path(source_path))
            output = Path(output_dir) / f"{basename}.wav"
            output.write_bytes(b"RIFF-audio")
            return str(output)

        completion = SimpleNamespace(
            usage={"prompt_tokens_details": {"audio_tokens": 1}},
            cost=None,
            cost_source="",
        )
        audio_result = SimpleNamespace(
            transcript="Co-chair. Co-chair, yes.",
            transport_metadata={"audio_consumption": "confirmed"},
            completion=completion,
        )
        runtime = {
            "model_id": "audio-witness",
            "resolved_model": "gemini/audio-witness",
            "llm_settings": {},
            "provider_key": "gemini",
            "provider_label": "Evidence Gemini",
            "openai_compatible_custom": False,
        }
        with (
            patch(
                "pandrator.web.subtitle_evidence.extract_audio_excerpt",
                side_effect=fake_extract,
            ),
            patch.object(
                self.services.subtitle_evidence,
                "_audio_model_runtime",
                return_value=runtime,
            ),
            patch(
                "pandrator.web.subtitle_evidence.transcribe_audio_evidence",
                return_value=audio_result,
            ),
        ):
            result = self.services.subtitle_evidence.run_request(
                created["record"]["id"], lambda *_args: None, threading.Event()
            )

        self.assertEqual([self.media_path], extracted_from)
        candidate = result["candidates"][0]
        self.assertEqual("bounded_clip", candidate["timing_kind"])
        self.assertEqual([], candidate["segments"])
        self.assertEqual([], candidate["words"])

    def test_create_and_resolve_replay_idempotently(self):
        create_payload = {
            "source_artifact_id": self.subtitle.id,
            "cue_id": 1,
            "reason": "Retry exactly once.",
            "routes": ["whisper"],
        }
        create_headers = {
            **self.headers,
            "Idempotency-Key": "subtitle-evidence:create:one",
        }
        first = self.client.post(
            f"/api/v1/sessions/{self.session.id}/subtitle-evidence",
            json=create_payload,
            headers=create_headers,
        )
        replay = self.client.post(
            f"/api/v1/sessions/{self.session.id}/subtitle-evidence",
            json=create_payload,
            headers=create_headers,
        )
        self.assertEqual(202, first.status_code, first.get_json())
        self.assertEqual(202, replay.status_code, replay.get_json())
        self.assertEqual("true", replay.headers.get("Idempotency-Replayed"))
        evidence_id = first.get_json()["record"]["id"]
        self.assertEqual(evidence_id, replay.get_json()["record"]["id"])
        with self.services.database.session() as session:
            records = session.scalars(
                select(SubtitleEvidence).where(
                    SubtitleEvidence.session_id == self.session.id
                )
            ).all()
            self.assertEqual([evidence_id], [record.id for record in records])
            evidence = session.get(SubtitleEvidence, evidence_id)
            evidence.status = "completed"

        resolve_headers = {
            **self.headers,
            "Idempotency-Key": "subtitle-evidence:resolve:one",
        }
        resolve_url = (
            f"/api/v1/sessions/{self.session.id}/subtitle-evidence/"
            f"{evidence_id}/resolve"
        )
        resolved = self.client.post(
            resolve_url,
            json={"action": "dismissed"},
            headers=resolve_headers,
        )
        resolve_replay = self.client.post(
            resolve_url,
            json={"action": "dismissed"},
            headers=resolve_headers,
        )
        self.assertEqual(200, resolved.status_code, resolved.get_json())
        self.assertEqual(200, resolve_replay.status_code, resolve_replay.get_json())
        self.assertEqual("true", resolve_replay.headers.get("Idempotency-Replayed"))
        self.assertEqual(resolved.get_json(), resolve_replay.get_json())

    def test_audio_witness_requires_an_active_model_declared_for_audio(self):
        provider = self.client.post(
            "/api/v1/providers",
            json={"provider_key": "gemini", "label": "Evidence Gemini"},
            headers=self.headers,
        ).get_json()
        text_only = self.client.post(
            f"/api/v1/providers/{provider['id']}/models",
            json={"model_id": "text-only", "is_active": True},
            headers=self.headers,
        ).get_json()
        audio_model = self.client.post(
            f"/api/v1/providers/{provider['id']}/models",
            json={
                "model_id": "audio-witness",
                "is_active": True,
                "input_modalities": ["text", "audio"],
            },
            headers=self.headers,
        ).get_json()
        base = {
            "source_artifact_id": self.subtitle.id,
            "cue_id": 1,
            "reason": "Check the words from the source audio.",
            "routes": ["audio_llm"],
        }
        with self.assertRaises(ValueError):
            self.services.subtitle_evidence.request(self.session.id, base)
        with self.assertRaises(ValueError):
            self.services.subtitle_evidence.request(
                self.session.id,
                {**base, "audio_model_ids": [text_only["id"]]},
            )

        created = self.services.subtitle_evidence.request(
            self.session.id,
            {**base, "audio_model_ids": [audio_model["id"]]},
        )
        self.assertEqual(["audio_llm"], created["record"]["routes"])
        self.assertEqual([audio_model["id"]], created["record"]["audio_model_ids"])

    def test_create_rejects_non_subtitle_ownership_and_overlong_cues(self):
        other = self.services.sessions.create("Other", workflow_kind="subtitles")
        foreign_path = self.services.paths.sessions / other.storage_key / "x.srt"
        foreign_path.parent.mkdir(parents=True, exist_ok=True)
        foreign_path.write_text("not a real subtitle")
        foreign = self.services.artifacts.register(
            foreign_path,
            kind="srt",
            role="transcription",
            session_id=other.id,
            metadata={"revision_id": self.revision_id},
        )
        with self.assertRaises(KeyError):
            self.services.subtitle_evidence.request(
                self.session.id,
                {
                    "source_artifact_id": foreign.id,
                    "cue_id": 1,
                    "reason": "wrong owner",
                    "routes": ["whisper"],
                },
            )

    def test_resolve_guards_state_and_accepts_successful_candidate(self):
        created = self.services.subtitle_evidence.request(
            self.session.id,
            {
                "source_artifact_id": self.subtitle.id,
                "cue_id": 1,
                "reason": "check",
                "routes": ["whisper"],
            },
        )
        evidence_id = created["record"]["id"]
        with self.services.database.session() as session:
            evidence = session.get(SubtitleEvidence, evidence_id)
            evidence.status = "completed"
            evidence.candidates_json = [
                {"id": "whisper-1", "route": "whisper", "status": "success"}
            ]
        resolved = self.services.subtitle_evidence.resolve(
            self.session.id,
            evidence_id,
            {"action": "accepted", "candidate_id": "whisper-1"},
        )
        self.assertEqual("resolved", resolved["record"]["status"])
        self.assertEqual("whisper-1", resolved["record"]["resolution"]["candidate_id"])

        reconsidered = self.services.subtitle_evidence.resolve(
            self.session.id,
            evidence_id,
            {"action": "uncertain", "note": "The wording still does not fit."},
        )
        self.assertEqual("uncertain", reconsidered["record"]["status"])
        dismissed = self.services.subtitle_evidence.resolve(
            self.session.id, evidence_id, {"action": "dismissed"}
        )
        self.assertEqual("dismissed", dismissed["record"]["status"])

        with self.services.database.session() as session:
            evidence = session.get(SubtitleEvidence, evidence_id)
            evidence.status = "running"
        with self.assertRaises(ValueError):
            self.services.subtitle_evidence.resolve(
                self.session.id, evidence_id, {"action": "dismissed"}
            )


class SubtitleEvidenceSchemaAndExcerptTests(unittest.TestCase):
    def test_candidate_text_is_cropped_to_the_cue_not_the_context_padding(self):
        service = self._service_class()
        text, method = service._cue_text(
            [
                {
                    "text": "Before Co-chair. Co-chair, yes. After",
                    "start_ms": 8_000,
                    "end_ms": 16_000,
                }
            ],
            [
                {"text": "Before", "start_ms": 8_500, "end_ms": 9_000},
                {"text": "Co-chair.", "start_ms": 10_050, "end_ms": 10_600},
                {"text": "Co-chair,", "start_ms": 10_700, "end_ms": 11_200},
                {"text": "yes.", "start_ms": 11_250, "end_ms": 11_700},
                {"text": "After", "start_ms": 12_100, "end_ms": 12_500},
            ],
            10_000,
            12_000,
        )
        self.assertEqual("Co-chair. Co-chair, yes.", text)
        self.assertEqual("word_overlap", method)

    def test_candidate_text_never_falls_back_to_neighboring_context(self):
        service = self._service_class()
        text, method = service._cue_text(
            [{"text": "Neighboring speech", "start_ms": 8_000, "end_ms": 9_000}],
            [],
            10_000,
            12_000,
        )

        self.assertEqual("", text)
        self.assertEqual("no_overlap", method)

    def test_evidence_costs_distinguish_local_from_commercial_estimates(self):
        service = self._service_class()
        self.assertEqual(
            {"kind": "not_applicable"},
            service._safe_cost({}, commercial=False),
        )
        self.assertEqual(
            {
                "kind": "estimate",
                "amount": 0.000055,
                "currency": "USD",
                "unit": "request",
                "usage_reported_by_provider": False,
                "billable_audio_seconds": 2,
                "billing_increment_seconds": 1,
                "cost_source": "published_list_price_estimate",
                "price_effective_until": "2026-12-31",
            },
            service._safe_cost(
                {
                    "usage": {
                        "estimated_cost_usd": 0.000055,
                        "currency": "USD",
                        "billable_audio_seconds": 2,
                        "billing_increment_seconds": 1,
                        "cost_source": "published_list_price_estimate",
                        "price_effective_until": "2026-12-31",
                        "usage_reported_by_provider": False,
                    }
                },
                commercial=True,
            ),
        )

    def test_mai_evidence_clone_forces_literal_verbatim_style(self):
        service = self._service_class()
        result = service._legacy_mai_v2_config(
            {
                "stt_transcribe_style": "clean",
                "provider_configs": [
                    {
                        "id": "azure_mai_transcribe_1_5",
                        "api_base": "https://eastus.api.cognitive.microsoft.com",
                        "secret_ref": "db:credential:stt:azure_mai_transcribe_1_5",
                        "path": "/legacy-path",
                        "pricing": {"amount_usd": None},
                    }
                ],
            }
        )
        self.assertEqual("verbatim", result["stt_transcribe_style"])
        clone = next(
            item
            for item in result["provider_configs"]
            if item["id"] == "azure_mai_transcribe_2"
        )
        self.assertEqual("MAI-Transcribe-2", clone["model"])
        self.assertNotIn("path", clone)
        self.assertNotIn("pricing", clone)
        profile = cloud_stt._profile_for(
            {**result, "stt_engine": "azure_mai_transcribe_2"}
        )
        self.assertEqual(0.10, profile["pricing"]["amount_usd"])
        self.assertEqual("2026-12-31", profile["pricing"]["price_effective_until"])

    @staticmethod
    def _service_class():
        from pandrator.web.subtitle_evidence import SubtitleEvidenceService

        return SubtitleEvidenceService

    def test_schema_resolution_rules(self):
        with self.assertRaises(ValueError):
            SubtitleEvidenceResolveRequest(action="accepted")
        with self.assertRaises(ValueError):
            SubtitleEvidenceResolveRequest(action="deleted", text="ambiguous")
        with self.assertRaises(ValueError):
            SubtitleEvidenceResolveRequest(action="deleted", text="")
        with self.assertRaises(ValueError):
            SubtitleEvidenceResolveRequest(action="uncertain")
        request = SubtitleEvidenceCreateRequest(
            source_artifact_id="artifact",
            cue_id=1,
            reason="check",
            routes=["whisper"],
        )
        self.assertEqual(2_000, request.padding_before_ms)

    def test_excerpt_command_is_bounded_and_normalized(self):
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, stderr=b"")

        with tempfile.TemporaryDirectory() as directory:
            output = extract_audio_excerpt(
                "/managed/source.mp4",
                directory,
                "excerpt",
                1_500,
                6_500,
                ffmpeg_executable="ffmpeg-test",
                run_func=run,
            )
        command = calls[0][0]
        self.assertEqual(output, str(Path(directory) / "excerpt.wav"))
        self.assertEqual(
            command[:7],
            ["ffmpeg-test", "-i", "/managed/source.mp4", "-ss", "1.5", "-t", "5"],
        )
        self.assertIn("-acodec", command)
        self.assertIn("pcm_s16le", command)
        self.assertIn("aresample,loudnorm", command)
        self.assertNotIn("shell", calls[0][1])

    def test_excerpt_reports_ffmpeg_failure_and_rejects_long_span(self):
        def run(command, **kwargs):
            raise subprocess.CalledProcessError(1, command, stderr=b"bad input")

        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaises(ExternalToolError),
        ):
            extract_audio_excerpt("source", directory, "excerpt", 0, 1000, run_func=run)
        with self.assertRaises(ValueError):
            extract_audio_excerpt("source", tempfile.mkdtemp(), "excerpt", 0, 60_001)


if __name__ == "__main__":
    unittest.main()
