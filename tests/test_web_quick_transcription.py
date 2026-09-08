import hashlib
import json
import io
import logging
import shutil
import tempfile
import threading
import time
import unittest
import wave
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import func, select

from pandrator.logic.cancellable_process import ProcessCancelled
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.jobs import Worker
from pandrator.web.models import (
    Artifact,
    DocumentRevision,
    Job,
    JobEvent,
    QuickTranscription,
    SessionRecord,
    SourceAsset,
    utcnow,
)
from pandrator.web.quick_transcription_schemas import CHUNK_SIZE
from pandrator.web.stt_resources import stt_resource_keys
from tests.web_test_support import prepare_web_test_data_root


class _FakeQuickTranscriber:
    def __init__(self, *, text="A private test transcript.", block=False):
        self.text = text
        self.block = block
        self.asr_calls = 0

    def normalize(self, command, *, cancel_event, **_kwargs):
        output_path = Path(command[-1])
        if self.block:
            while not cancel_event.wait(0.01):
                pass
            raise ProcessCancelled("fake normalizer observed cancellation")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output_path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\0\0" * 160)
        return SimpleNamespace(returncode=0)

    def transcribe(self, scratch, _normalized, _settings, **_kwargs):
        self.asr_calls += 1
        logging.getLogger("fake_asr").warning(self.text)
        scratch = Path(scratch)
        word_timestamps = scratch / "fake-word-timestamps.json"
        word_timestamps.write_text(
            json.dumps(
                {
                    "schema": "pandrator.transcript.v1",
                    "source_format": "fake-asr",
                    "language": "en",
                    "segments": [
                        {
                            "id": "fake-1",
                            "start_ms": 0,
                            "end_ms": 1000,
                            "text": self.text,
                            "words": [
                                {
                                    "text": self.text,
                                    "start_ms": 0,
                                    "end_ms": 1000,
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        srt = scratch / "fake.srt"
        srt.write_text(
            f"1\n00:00:00,000 --> 00:00:01,000\n{self.text}\n",
            encoding="utf-8",
        )
        return SimpleNamespace(
            word_timestamps_path=str(word_timestamps),
            srt_path=str(srt),
            engine="fake-asr",
            compute_backend="cpu",
        )


class QuickTranscriptionRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = prepare_web_test_data_root(self.temporary.name)
        self.bootstrap = BootstrapTokenStore()
        token = self.bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=self.bootstrap,
            background_maintenance=False,
        )
        self.client = self.app.test_client()
        bootstrap = self.client.post("/api/v1/auth/bootstrap", json={"token": token})
        self.assertEqual(200, bootstrap.status_code, bootstrap.get_json())
        self.csrf = bootstrap.get_json()["csrf_token"]
        self.headers = {"X-CSRF-Token": self.csrf}
        self.extension = self.app.extensions["pandrator"]

    def tearDown(self):
        self.extension["quick_transcriptions"].stop_maintenance()
        self.extension["database"].dispose()
        self.temporary.cleanup()

    @contextmanager
    def _fake_backend(self, fake):
        with (
            patch(
                "pandrator.web.quick_transcription.run_cancellable",
                side_effect=fake.normalize,
            ),
            patch(
                "pandrator.web.quick_transcription.transcribe_source_file_with_metadata",
                side_effect=fake.transcribe,
            ),
        ):
            yield

    def _payload(self, content=b"fake audio", **overrides):
        payload = {
            "filename": "clip.wav",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "format": "txt",
        }
        payload.update(overrides)
        return payload

    def _create(self, content=b"fake audio", *, key="quick-create-1", **overrides):
        response = self.client.post(
            "/api/v1/transcriptions",
            json=self._payload(content, **overrides),
            headers={**self.headers, "Idempotency-Key": key},
        )
        self.assertEqual(201, response.status_code, response.get_json())
        return response.get_json()

    def _upload(self, identifier, index, content, *, client=None, csrf=None):
        client = client or self.client
        headers = {"X-CSRF-Token": csrf or self.csrf}
        response = client.put(
            f"/api/v1/transcriptions/{identifier}/chunks/{index}",
            data=content,
            headers=headers,
        )
        return response

    def _start(self, identifier, *, client=None, csrf=None):
        client = client or self.client
        response = client.post(
            f"/api/v1/transcriptions/{identifier}/start",
            json={},
            headers={"X-CSRF-Token": csrf or self.csrf},
        )
        return response

    def _create_upload_start(self, content=b"fake audio", **overrides):
        record = self._create(content, **overrides)
        uploaded = self._upload(record["id"], 0, content)
        self.assertEqual(200, uploaded.status_code, uploaded.get_json())
        started = self._start(record["id"])
        self.assertEqual(202, started.status_code, started.get_json())
        return started.get_json()

    def _worker(self, worker_id="quick-test-worker"):
        handlers = self.extension["workflow_handlers"].handlers()
        return Worker(self.extension["jobs"], worker_id, handlers)

    def _record(self, identifier):
        with self.extension["database"].session() as session:
            record = session.get(QuickTranscription, identifier)
            session.expunge(record)
            return record

    def _wait_for_job_status(self, job_id, expected, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.extension["jobs"].get(job_id).status == expected:
                return True
            time.sleep(0.02)
        return False

    def test_stale_worker_cannot_publish_or_remove_replacement_inputs(self):
        started = self._create_upload_start()
        service = self.extension["quick_transcriptions"]
        directory = service.root / started["id"]
        fake = _FakeQuickTranscriber()
        transcribe = fake.transcribe

        def replace_lease(*args, **kwargs):
            result = transcribe(*args, **kwargs)
            with self.extension["database"].immediate_session() as session:
                job = session.get(Job, started["job_id"])
                job.lease_generation += 1
                replacement = directory / f"attempt-{job.lease_generation}"
                replacement.mkdir()
                (replacement / "in-use").write_text("replacement worker")
            return result

        fake.transcribe = replace_lease
        with self._fake_backend(fake):
            self.assertTrue(self._worker().run_once())
        self.assertTrue((directory / "source.wav").is_file())
        self.assertEqual(1, len(list(directory.glob("attempt-*/in-use"))))
        self.assertFalse((directory / "result.txt").exists())
        self.assertEqual("submitted", self._record(started["id"]).state)

    def test_metadata_chunks_start_worker_results_and_no_domain_records(self):
        content = b"audio bytes used by the fake normalizer"
        record = self._create(content, language="en_US", key="quick-metadata-1")
        self.assertEqual("uploading", record["status"])
        self.assertEqual(0, record["uploaded_bytes"])
        self.assertEqual(0, record["next_chunk_index"])
        self.assertEqual(CHUNK_SIZE, record["chunk_size"])
        self.assertIsNone(record["job_id"])

        uploaded = self._upload(record["id"], 0, content)
        self.assertEqual(200, uploaded.status_code, uploaded.get_json())
        self.assertEqual(len(content), uploaded.get_json()["uploaded_bytes"])

        started = self._start(record["id"])
        self.assertEqual(202, started.status_code, started.get_json())
        job_id = started.get_json()["job_id"]
        self.assertEqual("queued", started.get_json()["status"])
        retry = self._start(record["id"])
        self.assertEqual(202, retry.status_code, retry.get_json())
        self.assertEqual(job_id, retry.get_json()["job_id"])
        replay = self.client.post(
            "/api/v1/transcriptions",
            json=self._payload(content, language="en_US"),
            headers={**self.headers, "Idempotency-Key": "quick-metadata-1"},
        )
        self.assertEqual(201, replay.status_code, replay.get_json())
        self.assertEqual(record["id"], replay.get_json()["id"])
        self.assertEqual(job_id, replay.get_json()["job_id"])

        fake = _FakeQuickTranscriber(text="CONFIDENTIAL fake recognition")
        with self._fake_backend(fake):
            self.assertTrue(self._worker().run_once())
        self.assertEqual(1, fake.asr_calls)

        status = self.client.get(f"/api/v1/transcriptions/{record['id']}")
        self.assertEqual(200, status.status_code, status.get_json())
        self.assertEqual("succeeded", status.get_json()["status"])
        self.assertTrue(status.get_json()["inline_result"])
        self.assertEqual(
            "CONFIDENTIAL fake recognition",
            status.get_json()["result"]["content"],
        )

        raw_txt = self.client.get(
            f"/api/v1/transcriptions/{record['id']}/result?format=txt"
        )
        raw_srt = self.client.get(
            f"/api/v1/transcriptions/{record['id']}/result?format=srt"
        )
        raw_json = self.client.get(
            f"/api/v1/transcriptions/{record['id']}/result?format=json"
        )
        self.assertEqual(200, raw_txt.status_code)
        self.assertEqual(
            "CONFIDENTIAL fake recognition", raw_txt.get_data(as_text=True)
        )
        self.assertEqual(200, raw_srt.status_code)
        self.assertIn("00:00:00,000 --> 00:00:01,000", raw_srt.get_data(as_text=True))
        self.assertEqual(200, raw_json.status_code)
        self.assertEqual("pandrator.transcript.v1", raw_json.get_json()["schema"])
        self.assertEqual(
            "json",
            self.client.get(
                f"/api/v1/transcriptions/{record['id']}?format=json"
            ).get_json()["format"],
        )
        self.assertEqual(
            "srt",
            self.client.get(
                f"/api/v1/transcriptions/{record['id']}?format=srt"
            ).get_json()["format"],
        )
        self.assertEqual(1, fake.asr_calls, "format selection must not rerun ASR")

        with self.extension["database"].session() as session:
            job = session.get(Job, job_id)
            events = list(
                session.scalars(select(JobEvent).where(JobEvent.job_id == job_id))
            )
            self.assertEqual({"transcription_id": record["id"]}, job.payload_json)
            self.assertEqual({}, job.result_json)
            serialized_events = json.dumps([event.payload_json for event in events])
            self.assertNotIn("CONFIDENTIAL fake recognition", serialized_events)
            for model in (SessionRecord, SourceAsset, Artifact, DocumentRevision):
                self.assertEqual(
                    0,
                    session.scalar(select(func.count()).select_from(model)),
                    model.__name__,
                )

        self.assertEqual(["service:stt"], job.resource_keys_json)

    def test_chunk_size_boundaries_and_out_of_order_or_hash_conflicts(self):
        first = b"a" * CHUNK_SIZE
        final = b"b"
        content = first + final
        record = self._create(content, key="quick-chunk-boundary")
        short_first = self._create(content, key="quick-chunk-short-first")
        short_response = self._upload(short_first["id"], 0, b"a" * (CHUNK_SIZE - 1))
        self.assertEqual(400, short_response.status_code, short_response.get_json())
        self.assertEqual(
            "invalid_chunk_size", short_response.get_json()["error"]["code"]
        )
        accepted = self._upload(record["id"], 0, first)
        self.assertEqual(200, accepted.status_code, accepted.get_json())
        self.assertEqual(CHUNK_SIZE, accepted.get_json()["uploaded_bytes"])
        accepted = self._upload(record["id"], 1, final)
        self.assertEqual(200, accepted.status_code, accepted.get_json())
        self.assertEqual(len(content), accepted.get_json()["uploaded_bytes"])
        self.assertEqual(2, accepted.get_json()["next_chunk_index"])

        started = self._start(record["id"])
        self.assertEqual(202, started.status_code, started.get_json())
        self.assertEqual(
            ["service:stt"],
            self.extension["jobs"].get(started.get_json()["job_id"]).resource_keys_json,
        )

        out_of_order = self._create(b"abcde", key="quick-chunk-order")
        response = self._upload(out_of_order["id"], 1, b"e")
        self.assertEqual(409, response.status_code, response.get_json())
        self.assertEqual("chunk_out_of_order", response.get_json()["error"]["code"])
        self.assertEqual(200, self._upload(out_of_order["id"], 0, b"abcde").status_code)
        replay_conflict = self._upload(out_of_order["id"], 0, b"xxxxx")
        self.assertEqual(409, replay_conflict.status_code, replay_conflict.get_json())
        self.assertEqual("chunk_conflict", replay_conflict.get_json()["error"]["code"])

        wrong_hash = self._create(
            b"hash mismatch",
            key="quick-source-hash",
            sha256="0" * 64,
        )
        self.assertEqual(
            200, self._upload(wrong_hash["id"], 0, b"hash mismatch").status_code
        )
        response = self._start(wrong_hash["id"])
        self.assertEqual(409, response.status_code, response.get_json())
        self.assertEqual("source_hash_mismatch", response.get_json()["error"]["code"])

    def test_create_idempotency_replays_resource_and_rejects_argument_mismatch(self):
        content = b"idempotent source"
        payload = self._payload(content, language="en")
        headers = {**self.headers, "Idempotency-Key": "quick-idempotency-1"}
        first = self.client.post(
            "/api/v1/transcriptions", json=payload, headers=headers
        )
        replay = self.client.post(
            "/api/v1/transcriptions", json=payload, headers=headers
        )
        self.assertEqual(201, first.status_code, first.get_json())
        self.assertEqual(201, replay.status_code, replay.get_json())
        self.assertEqual(first.get_json()["id"], replay.get_json()["id"])
        self.assertIsNone(replay.get_json()["job_id"])

        mismatch = dict(payload, language="pl")
        conflict = self.client.post(
            "/api/v1/transcriptions", json=mismatch, headers=headers
        )
        self.assertEqual(409, conflict.status_code, conflict.get_json())
        self.assertEqual("idempotency_conflict", conflict.get_json()["error"]["code"])

    def test_multipart_shortcut_real_ffmpeg_and_raw_replay(self):
        if not shutil.which("ffmpeg"):
            self.skipTest("FFmpeg is not installed")
        audio_bytes = io.BytesIO()
        with wave.open(audio_bytes, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\0\0" * 1600)
        content = audio_bytes.getvalue()
        headers = {**self.headers, "Idempotency-Key": "multipart-real-ffmpeg"}

        def submit(query=""):
            return self.client.post(
                f"/api/v1/transcriptions{query}",
                data={
                    "file": (io.BytesIO(content), "clip.wav"),
                    "options": '{"format":"srt"}',
                },
                headers=headers,
            )

        invalid = submit("?wait_seconds=300")
        self.assertEqual(400, invalid.status_code)
        with self.extension["database"].session() as session:
            self.assertEqual(0, session.scalar(select(func.count()).select_from(Job)))
        pending = submit()
        self.assertEqual(202, pending.status_code, pending.get_json())
        fake = _FakeQuickTranscriber(text="Multipart transcription")
        with patch(
            "pandrator.web.quick_transcription.transcribe_source_file_with_metadata",
            side_effect=fake.transcribe,
        ):
            self.assertTrue(self._worker().run_once())
        self.assertEqual(1, fake.asr_calls)
        complete = submit("?response=raw&wait_seconds=1")
        self.assertEqual(200, complete.status_code, complete.get_data(as_text=True))
        self.assertEqual("application/x-subrip", complete.mimetype)
        self.assertIn("Multipart transcription", complete.get_data(as_text=True))
        self.assertEqual(1, fake.asr_calls)
        with self.extension["database"].session() as session:
            self.assertEqual(1, session.scalar(select(func.count()).select_from(Job)))

    def test_owner_isolation_covers_quick_and_generic_job_routes(self):
        started = self._create_upload_start(key="quick-owner-isolation")
        identifier = started["id"]
        job_id = started["job_id"]
        second = self.bootstrap.issue(subject="second", kind="owner_session")
        second_client = self.app.test_client()
        second_bootstrap = second_client.post(
            "/api/v1/auth/bootstrap", json={"token": second}
        )
        self.assertEqual(200, second_bootstrap.status_code, second_bootstrap.get_json())
        second_csrf = second_bootstrap.get_json()["csrf_token"]
        second_headers = {"X-CSRF-Token": second_csrf}

        self.assertEqual(
            404,
            second_client.get(f"/api/v1/transcriptions/{identifier}").status_code,
        )
        self.assertEqual(
            404,
            second_client.get(
                f"/api/v1/transcriptions/{identifier}/result?format=txt"
            ).status_code,
        )
        self.assertEqual(
            404,
            second_client.post(
                f"/api/v1/transcriptions/{identifier}/cancel",
                headers=second_headers,
            ).status_code,
        )
        self.assertEqual(
            404,
            second_client.delete(
                f"/api/v1/transcriptions/{identifier}",
                headers=second_headers,
            ).status_code,
        )
        for path in (f"/api/v1/jobs/{job_id}", f"/api/v1/work/{job_id}"):
            self.assertEqual(404, second_client.get(path).status_code)
        for path, headers in (
            (
                f"/api/v1/jobs/{job_id}/cancel",
                second_headers,
            ),
            (
                f"/api/v1/work/{job_id}/cancel",
                {**second_headers, "Idempotency-Key": "second-cancel-1"},
            ),
        ):
            self.assertEqual(404, second_client.post(path, headers=headers).status_code)
        self.assertEqual(200, self.client.get(f"/api/v1/jobs/{job_id}").status_code)
        for path in ("/api/v1/jobs", "/api/v1/work", "/api/v1/events/snapshot"):
            response = second_client.get(path)
            self.assertEqual(200, response.status_code)
            self.assertNotIn(job_id, response.get_data(as_text=True))
            self.assertNotIn(identifier, response.get_data(as_text=True))
        # Streaming skips private events but advances the reconnect cursor.
        stream = second_client.get("/api/v1/events?after=0", buffered=False)
        first = next(iter(stream.response)).decode()
        stream.close()
        self.assertNotIn(job_id, first)
        self.assertNotIn("transcription.quick", first)
        self.assertIn("stream.cursor", first)

    def test_expiry_queued_cancel_and_delete_remove_temporary_inputs(self):
        expiring = self._create(b"expire me", key="quick-expiring-upload")
        self.assertEqual(200, self._upload(expiring["id"], 0, b"expire me").status_code)
        expiring_path = self.paths.root / "quick-transcriptions" / expiring["id"]
        self.assertTrue((expiring_path / "chunk-0").exists())
        with self.extension["database"].session() as session:
            session.get(QuickTranscription, expiring["id"]).expires_at = (
                utcnow() - timedelta(seconds=1)
            )
        self.extension["quick_transcriptions"].cleanup()
        expired = self._record(expiring["id"])
        self.assertEqual("expired", expired.state)
        self.assertFalse(expiring_path.exists())
        self.assertEqual({}, expired.settings_json)
        self.assertEqual("", expired.sha256)
        self.assertEqual("", expired.source_suffix)

        queued = self._create_upload_start(b"queued cancel", key="quick-queued-cancel")
        queued_path = self.paths.root / "quick-transcriptions" / queued["id"]
        self.assertTrue((queued_path / "source.wav").exists())
        canceled = self.client.post(
            f"/api/v1/transcriptions/{queued['id']}/cancel",
            headers=self.headers,
        )
        self.assertEqual(200, canceled.status_code, canceled.get_json())
        self.assertEqual("canceled", canceled.get_json()["status"])
        self.assertFalse((queued_path / "source.wav").exists())
        if queued_path.exists():
            self.assertFalse(
                any(path.name.startswith("chunk-") for path in queued_path.iterdir())
            )

        deleted = self._create_upload_start(
            b"explicit delete", key="quick-explicit-delete"
        )
        deleted_path = self.paths.root / "quick-transcriptions" / deleted["id"]
        self.assertTrue(deleted_path.exists())
        removed = self.client.delete(
            f"/api/v1/transcriptions/{deleted['id']}",
            headers=self.headers,
        )
        self.assertEqual(200, removed.status_code, removed.get_json())
        self.assertEqual("deleted", removed.get_json()["status"])
        self.assertFalse(deleted_path.exists())
        erased = self._record(deleted["id"])
        self.assertEqual({}, erased.settings_json)
        self.assertEqual("", erased.sha256)

    def test_running_delete_requests_worker_cancellation_and_preserves_original(self):
        original = (
            self.paths.root.parent / f"{self.paths.root.name}-original-user-file.wav"
        )
        original.write_bytes(b"original user bytes")
        self.addCleanup(original.unlink, missing_ok=True)
        started = self._create_upload_start(
            original.read_bytes(), key="quick-running-delete"
        )
        identifier = started["id"]
        job_id = started["job_id"]
        source_path = (
            self.paths.root / "quick-transcriptions" / identifier / "source.wav"
        )
        fake = _FakeQuickTranscriber(block=True)
        worker = self._worker("quick-cancel-worker")
        worker_thread = threading.Thread(target=worker.run_once)
        with self._fake_backend(fake):
            worker_thread.start()
            self.assertTrue(self._wait_for_job_status(job_id, "running"))
            response = self.client.delete(
                f"/api/v1/transcriptions/{identifier}", headers=self.headers
            )
            self.assertEqual(200, response.status_code, response.get_json())
            self.assertEqual("deleting", response.get_json()["status"])
            worker_thread.join(timeout=5)
        self.assertFalse(worker_thread.is_alive())
        self.assertEqual("canceled", self.extension["jobs"].get(job_id).status)
        self.extension["quick_transcriptions"].cleanup()
        self.assertFalse(source_path.exists())
        self.assertTrue(
            original.exists(), "the worker must not remove the caller's original"
        )
        self.assertEqual("deleted", self._record(identifier).state)

    def test_large_results_are_paged_without_inline_transcript_storage(self):
        text = "x" * 35_000
        started = self._create_upload_start(b"large result", key="quick-large-result")
        fake = _FakeQuickTranscriber(text=text)
        with self._fake_backend(fake):
            self.assertTrue(self._worker("quick-large-worker").run_once())
        status = self.client.get(f"/api/v1/transcriptions/{started['id']}")
        self.assertEqual(200, status.status_code, status.get_json())
        self.assertFalse(status.get_json()["inline_result"])
        raw = self.client.get(
            f"/api/v1/transcriptions/{started['id']}/result?format=txt"
        )
        self.assertEqual(text, raw.get_data(as_text=True))

        pages = []
        offset = 0
        while True:
            page = self.client.get(
                f"/api/v1/transcriptions/{started['id']}/result",
                query_string={"format": "txt", "offset": offset, "limit": 8192},
            )
            self.assertEqual(200, page.status_code, page.get_json())
            payload = page.get_json()
            pages.append(payload["content"])
            if payload["next_offset"] is None:
                break
            offset = payload["next_offset"]
        self.assertEqual(text, "".join(pages))

    def test_validation_auth_and_stt_resource_key_contract(self):
        base_headers = {**self.headers, "Idempotency-Key": "quick-invalid-1"}
        invalid_format = self.client.post(
            "/api/v1/transcriptions",
            json=self._payload(format="vtt"),
            headers=base_headers,
        )
        self.assertEqual(400, invalid_format.status_code, invalid_format.get_json())
        invalid_size = self.client.post(
            "/api/v1/transcriptions",
            json=self._payload(size_bytes=0),
            headers={**self.headers, "Idempotency-Key": "quick-invalid-2"},
        )
        self.assertEqual(400, invalid_size.status_code, invalid_size.get_json())
        invalid_extension = self.client.post(
            "/api/v1/transcriptions",
            json=self._payload(filename="clip.txt"),
            headers={**self.headers, "Idempotency-Key": "quick-invalid-3"},
        )
        self.assertEqual(
            400, invalid_extension.status_code, invalid_extension.get_json()
        )
        missing_key = self.client.post(
            "/api/v1/transcriptions", json=self._payload(), headers=self.headers
        )
        self.assertEqual(400, missing_key.status_code, missing_key.get_json())
        unauthenticated = self.app.test_client().post(
            "/api/v1/transcriptions",
            json=self._payload(),
            headers={"Idempotency-Key": "quick-unauth-1"},
        )
        self.assertEqual(401, unauthenticated.status_code, unauthenticated.get_json())

        limited_token = self.bootstrap.issue(subject="limited", scopes=["app.read"])
        limited_client = self.app.test_client()
        limited_bootstrap = limited_client.post(
            "/api/v1/auth/bootstrap", json={"token": limited_token}
        )
        self.assertEqual(200, limited_bootstrap.status_code)
        denied = limited_client.post(
            "/api/v1/transcriptions",
            json=self._payload(),
            headers={"Idempotency-Key": "quick-scope-1"},
        )
        self.assertEqual(403, denied.status_code, denied.get_json())

        self.assertEqual(["service:stt"], stt_resource_keys({}))
        self.assertEqual(
            ["service:stt", "gpu:cuda"],
            stt_resource_keys({"compute_backend": "cuda"}),
        )
        cuda = self._create(
            b"cuda resource",
            key="quick-cuda-resource",
            compute_backend="cuda",
        )
        self.assertEqual(200, self._upload(cuda["id"], 0, b"cuda resource").status_code)
        started = self._start(cuda["id"])
        self.assertEqual(202, started.status_code, started.get_json())
        self.assertEqual(
            ["gpu:cuda", "service:stt"],
            self.extension["jobs"].get(started.get_json()["job_id"]).resource_keys_json,
        )


if __name__ == "__main__":
    unittest.main()
