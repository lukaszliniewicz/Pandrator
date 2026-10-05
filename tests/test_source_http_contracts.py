"""Native source HTTP reads and admission, without executing ingestion jobs."""

import io
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import func, select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import Document, DocumentRevision, Job, TimedWord
from tests.web_test_support import prepare_web_test_data_root


class SourceHttpContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        for target in ("socket.socket.connect", "requests.sessions.Session.request"):
            blocker = patch(target, side_effect=AssertionError("Unexpected network call"))
            blocker.start()
            self.addCleanup(blocker.stop)
        prepare_web_test_data_root(self.temporary.name)
        bootstrap = BootstrapTokenStore()
        self.app = create_app(
            data_root=self.temporary.name, testing=True, bootstrap_tokens=bootstrap
        )
        self.database = self.app.extensions["pandrator"]["database"]
        self.addCleanup(self.database.dispose)
        self.client = self.app.test_client()
        token = bootstrap.issue()
        csrf = self.client.post("/api/v1/auth/bootstrap", json={"token": token})
        self.headers = {"X-CSRF-Token": csrf.get_json()["csrf_token"]}
        self.session_id = self.create_session("Source HTTP")

    def create_session(self, name: str) -> str:
        response = self.client.post(
            "/api/v1/sessions",
            json={"name": name, "workflow_kind": "audiobook"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["id"]

    def seed_words(self, session_id: str, ordinals: list[int]) -> str:
        with self.database.session() as session:
            document = Document(session_id=session_id, stage="transcription")
            session.add(document)
            session.flush()
            revision = DocumentRevision(
                document_id=document.id, revision_number=1, content_hash="word-fixture"
            )
            session.add(revision)
            session.flush()
            session.add_all(
                [
                    TimedWord(
                        revision_id=revision.id,
                        ordinal=ordinal,
                        text=f"word-{ordinal}",
                        start_ms=ordinal * 100,
                        end_ms=ordinal * 100 + 90,
                        speaker="speaker-a" if ordinal == 10 else None,
                        confidence=0.75 if ordinal == 10 else None,
                        metadata_json={"nested": {"ordinal": ordinal}, "tags": ["raw"]},
                    )
                    for ordinal in ordinals
                ]
            )
            return revision.id

    def job_count(self) -> int:
        with self.database.session() as session:
            return int(session.scalar(select(func.count()).select_from(Job)) or 0)

    def test_word_pages_use_sparse_ordinals_and_preserve_metadata_and_nulls(self):
        revision_id = self.seed_words(self.session_id, [20, 3, 10])
        other = self.create_session("Other source")
        self.seed_words(other, [3])
        url = f"/api/v1/document-revisions/{revision_id}/words"
        first = self.client.get(url, query_string={"cursor": -10, "limit": 2})
        self.assertEqual(first.status_code, 200)
        body = first.get_json()
        self.assertEqual([item["ordinal"] for item in body["items"]], [3, 10])
        self.assertEqual(body["next_cursor"], 11)
        self.assertEqual({item["revision_id"] for item in body["items"]}, {revision_id})
        self.assertEqual(
            body["items"][0]["metadata_json"], {"nested": {"ordinal": 3}, "tags": ["raw"]}
        )
        self.assertIsNone(body["items"][0]["segment_id"])
        self.assertIsNone(body["items"][0]["speaker"])
        self.assertIsNone(body["items"][0]["confidence"])
        self.assertEqual(body["items"][1]["speaker"], "speaker-a")
        self.assertEqual(body["items"][1]["confidence"], 0.75)
        self.assertEqual((body["items"][1]["start_ms"], body["items"][1]["end_ms"]), (1000, 1090))
        last = self.client.get(
            url, query_string={"cursor": body["next_cursor"], "limit": 2}
        ).get_json()
        self.assertEqual([item["ordinal"] for item in last["items"]], [20])
        self.assertIsNone(last["next_cursor"])
        self.assertEqual(
            self.client.get(url, query_string={"cursor": 21}).get_json(),
            {"items": [], "next_cursor": None},
        )

    def test_word_pagination_defaults_and_bounds(self):
        revision_id = self.seed_words(self.session_id, list(range(1002)))
        url = f"/api/v1/document-revisions/{revision_id}/words"
        for query, count in (
            ({}, 500),
            ({"limit": ""}, 500),
            ({"limit": 5000}, 1000),
            ({"limit": 0}, 1),
        ):
            with self.subTest(query=query):
                response = self.client.get(url, query_string=query)
                self.assertEqual(response.status_code, 200)
                body = response.get_json()
                self.assertEqual(len(body["items"]), count)
                self.assertEqual(body["next_cursor"], count)

    def test_missing_reads_and_invalid_pagination_return_http_errors(self):
        for url, code in (
            ("/api/v1/sessions/missing/documents", "not_found"),
            ("/api/v1/document-revisions/missing/words", "not_found"),
            ("/api/v1/document-revisions/missing/words?cursor=bad", "validation_error"),
            ("/api/v1/document-revisions/missing/words?limit=1.5", "validation_error"),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 404 if code == "not_found" else 422)
                self.assertEqual(response.get_json()["error"]["code"], code)

    def test_documents_only_expose_the_requested_session(self):
        other = self.create_session("Other documents")
        revision_id = self.seed_words(other, [0])
        self.assertEqual(
            self.client.get(f"/api/v1/sessions/{self.session_id}/documents").get_json(),
            {"items": []},
        )
        items = self.client.get(f"/api/v1/sessions/{other}/documents").get_json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["revisions"][0]["id"], revision_id)

    def test_url_admission_persists_a_queued_job_without_downloading(self):
        url = "https://example.org/source.mp4"
        response = self.client.post(
            f"/api/v1/sessions/{self.session_id}/sources/url",
            json={"url": url},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 202, response.get_json())
        with self.database.session() as session:
            job = session.get(Job, response.get_json()["id"])
            self.assertIsNotNone(job)
            assert job is not None
            self.assertEqual(
                (job.kind, job.status, job.session_id),
                ("source.download_url", "queued", self.session_id),
            )
            self.assertEqual(job.payload_json, {"session_id": self.session_id, "url": url})
        self.assertEqual(self.job_count(), 1)

    def test_reuse_admission_accepts_managed_sources_from_another_session(self):
        owner = self.create_session("Reusable owner")
        uploaded = self.client.post(
            "/api/v1/uploads",
            data={"session_id": owner, "file": (io.BytesIO(b"reusable source"), "source.txt")},
            headers=self.headers,
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.get_json())
        artifact_id = uploaded.get_json()["artifact_id"]
        response = self.client.post(
            f"/api/v1/sessions/{self.session_id}/sources/reuse",
            json={"artifact_id": artifact_id},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 202, response.get_json())
        with self.database.session() as session:
            job = session.get(Job, response.get_json()["id"])
            assert job is not None
            self.assertEqual(
                (job.kind, job.status, job.session_id), ("source.reuse", "queued", self.session_id)
            )
            self.assertEqual(
                job.payload_json, {"session_id": self.session_id, "artifact_id": artifact_id}
            )
        self.assertEqual(self.job_count(), 1)

    def test_rejected_ingestion_requests_create_no_jobs(self):
        for session_id, route, payload, status in (
            (self.session_id, "url", {"url": "short"}, 422),
            (self.session_id, "url", {}, 422),
            (self.session_id, "reuse", {}, 422),
            (self.session_id, "reuse", {"artifact_id": "missing"}, 404),
            ("missing", "url", {"url": "https://example.org/source.mp4"}, 404),
            ("missing", "reuse", {"artifact_id": "missing"}, 404),
        ):
            with self.subTest(session_id=session_id, route=route, payload=payload):
                response = self.client.post(
                    f"/api/v1/sessions/{session_id}/sources/{route}",
                    json=payload,
                    headers=self.headers,
                )
                self.assertEqual(response.status_code, status, response.get_json())
                self.assertEqual(self.job_count(), 0)
