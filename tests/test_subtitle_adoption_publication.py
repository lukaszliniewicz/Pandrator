"""Subtitle derivatives and database publication share failure ownership."""

import hashlib
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from sqlalchemy import select
from sqlalchemy.engine import Connection

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import (
    Artifact,
    ArtifactEdge,
    Document,
    DocumentRevision,
    Segment,
    SessionRecord,
    SessionSetting,
    SessionSource,
    SourceAsset,
    SourceRecord,
)
from pandrator.web.openapi import build_openapi_document
from tests.web_test_support import prepare_web_test_data_root

SOURCE = b"1\n00:00:01,000 --> 00:00:03,000\nHello, world.\n"


class SubtitleAdoptionPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        prepare_web_test_data_root(self.temporary.name)
        bootstrap = BootstrapTokenStore()
        self.app = create_app(
            data_root=self.temporary.name, testing=True, bootstrap_tokens=bootstrap
        )
        self.services = self.app.extensions["pandrator"]
        self.database = self.services["database"]
        self.addCleanup(self.database.dispose)
        self.client = self.app.test_client()
        csrf = self.client.post(
            "/api/v1/auth/bootstrap", json={"token": bootstrap.issue()}
        ).get_json()["csrf_token"]
        self.headers = {"X-CSRF-Token": csrf}
        response = self.client.post(
            "/api/v1/sessions",
            json={"name": "Publication rollback", "workflow_kind": "voiceover"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201)
        self.session_id = response.get_json()["id"]
        paths = self.services["paths"]
        self.source = paths.managed_path("uploads/legacy-source.srt")
        self.source.parent.mkdir(parents=True, exist_ok=True)
        self.source.write_bytes(SOURCE)
        artifact = self.services["artifacts"].register(self.source, kind="srt", role="upload")
        self.source_artifact_id = artifact.id
        library = self.services["source_library"]
        self.asset_id = library.ensure_for_artifact(artifact.id, kind="srt").id
        # Model a legacy attachment without a materialized transcription.
        with patch.object(library, "artifacts", None):
            library.attach(self.session_id, self.asset_id)
        self.destination = paths.managed_path(
            f"sessions/{self.session_id}/imported-subtitles/{hashlib.sha256(SOURCE).hexdigest()}.srt"
        )

    def snapshot(self) -> str:
        models = (
            SessionRecord,
            SessionSource,
            SourceAsset,
            SourceRecord,
            SessionSetting,
            Document,
            DocumentRevision,
            Segment,
            Artifact,
            ArtifactEdge,
        )
        with self.database.session() as session:
            rows = {
                model.__tablename__: sorted(
                    json.dumps(dict(row), sort_keys=True, default=str)
                    for row in session.execute(select(model.__table__)).mappings()
                )
                for model in models
            }
        return json.dumps(rows, sort_keys=True)

    def adopt(self):
        return self.services["source_library"].adopt_subtitles(self.session_id, self.asset_id)

    def assert_failed_publication_is_retryable(self, before: str):
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.source.read_bytes(), SOURCE)
        content = self.destination.read_bytes()
        inode = self.destination.stat().st_ino
        retry = self.adopt()
        self.assertFalse(retry["reused"])
        self.assertEqual(self.destination.read_bytes(), content)
        self.assertEqual(self.destination.stat().st_ino, inode)
        with self.database.session() as session:
            self.assertEqual(len(session.scalars(select(DocumentRevision)).all()), 1)
            artifact = session.get(Artifact, retry["artifact_id"])
            assert artifact is not None
            self.assertEqual(
                artifact.relative_path,
                self.services["paths"].relative_managed_path(self.destination),
            )

    def test_database_failure_after_artifact_flush_retains_retryable_staging(self):
        before = self.snapshot()
        artifacts = self.services["artifacts"]
        original = artifacts.register_in_session
        witnessed = []

        def fail_after_registration(*args, **kwargs):
            artifact = original(*args, **kwargs)
            self.assertTrue(self.destination.is_file())
            self.assertEqual(artifact.role, "transcription")
            witnessed.append(artifact.id)
            raise RuntimeError("publication failed after flush")

        with patch.object(artifacts, "register_in_session", side_effect=fail_after_registration):
            with self.assertRaisesRegex(RuntimeError, "publication failed after flush"):
                self.adopt()
        self.assertEqual(len(witnessed), 1)
        self.assert_failed_publication_is_retryable(before)

    def test_native_connection_commit_failure_retains_retryable_staging(self):
        before = self.snapshot()
        witnessed = []
        original = Connection.commit

        def fail_commit(connection):
            if connection.engine is not self.database.engine:
                return original(connection)
            revisions = connection.execute(select(DocumentRevision.id)).scalars().all()
            self.assertEqual(len(revisions), 1)
            self.assertTrue(self.destination.is_file())
            witnessed.extend(revisions)
            raise RuntimeError("native commit failed")

        with patch.object(Connection, "commit", fail_commit):
            with self.assertRaisesRegex(RuntimeError, "native commit failed"):
                self.adopt()
        self.assertEqual(len(witnessed), 1)
        self.assert_failed_publication_is_retryable(before)

    def test_interrupted_write_leaves_no_partial_destination_and_retry_succeeds(self):
        before = self.snapshot()
        opened = []
        original_open = Path.open
        original_fdopen = os.fdopen

        @contextmanager
        def interrupted_writer(handle):
            with handle:
                writer = Mock(wraps=handle)

                def write_partial(content):
                    handle.write(content[:5])
                    handle.flush()
                    opened.append(content[:5])
                    raise OSError("interrupted subtitle write")

                writer.write.side_effect = write_partial
                yield writer

        def open_path(path, *args, **kwargs):
            handle = original_open(path, *args, **kwargs)
            if path == self.destination and args and args[0] == "x":
                return interrupted_writer(handle)
            return handle

        def open_descriptor(*args, **kwargs):
            return interrupted_writer(original_fdopen(*args, **kwargs))

        with patch.object(Path, "open", open_path), patch.object(os, "fdopen", open_descriptor):
            with self.assertRaisesRegex(OSError, "interrupted subtitle write"):
                self.adopt()
        self.assertEqual(len(opened), 1)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.source.read_bytes(), SOURCE)
        self.assertFalse(
            self.destination.exists(), "Partial bytes occupied the immutable destination"
        )
        self.assertEqual(list(self.destination.parent.iterdir()), [])
        retry = self.adopt()
        self.assertFalse(retry["reused"])
        self.assertIn(b"Hello, world.", self.destination.read_bytes())

    def test_successful_adoption_preserves_derivative_on_reuse(self):
        first = self.adopt()
        content = self.destination.read_bytes()
        before = self.snapshot()
        second = self.adopt()
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(second["revision_id"], first["revision_id"])
        self.assertEqual(self.destination.read_bytes(), content)
        self.assertEqual(self.source.read_bytes(), SOURCE)
        self.assertEqual(self.snapshot(), before)

    def test_changed_preexisting_derivative_is_never_overwritten_or_removed(self):
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        self.destination.write_text("A user changed this file.", encoding="utf-8")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "derivative has changed"):
            self.adopt()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.destination.read_text(), "A user changed this file.")
        self.assertEqual(self.source.read_bytes(), SOURCE)

    def test_matching_preexisting_derivative_survives_failed_registration(self):
        # Retain a complete candidate from an earlier failed database attempt.
        artifacts = self.services["artifacts"]
        original = artifacts.register_in_session

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("registration failed")

        before = self.snapshot()
        with patch.object(artifacts, "register_in_session", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "registration failed"):
                self.adopt()
        content = self.destination.read_bytes()
        inode = self.destination.stat().st_ino
        with patch.object(artifacts, "register_in_session", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "registration failed"):
                self.adopt()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.destination.read_bytes(), content)
        self.assertEqual(self.destination.stat().st_ino, inode)

    def test_destination_created_during_publication_is_not_overwritten(self):
        before = self.snapshot()
        publication = "rename" if os.name == "nt" else "link"
        original = getattr(os, publication)
        witnessed = []

        def concurrent_publication(source, destination, *args, **kwargs):
            self.assertEqual(Path(destination), self.destination)
            self.destination.write_text("Another writer owns this file.", encoding="utf-8")
            witnessed.append(Path(source).read_bytes())
            return original(source, destination, *args, **kwargs)

        with patch.object(os, publication, concurrent_publication):
            with self.assertRaisesRegex(ValueError, "derivative has changed"):
                self.adopt()
        self.assertEqual(len(witnessed), 1)
        self.assertIn(b"Hello, world.", witnessed[0])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.destination.read_text(), "Another writer owns this file.")
        self.assertEqual(list(self.destination.parent.iterdir()), [self.destination])
        self.assertEqual(self.source.read_bytes(), SOURCE)

    def test_matching_destination_created_during_publication_is_reused(self):
        publication = "rename" if os.name == "nt" else "link"
        original = getattr(os, publication)
        witnessed = []

        def concurrent_publication(source, destination, *args, **kwargs):
            self.destination.write_bytes(Path(source).read_bytes())
            witnessed.append(self.destination.stat().st_ino)
            return original(source, destination, *args, **kwargs)

        with patch.object(os, publication, concurrent_publication):
            result = self.adopt()
        self.assertFalse(result["reused"])
        self.assertEqual(witnessed, [self.destination.stat().st_ino])
        self.assertEqual(list(self.destination.parent.iterdir()), [self.destination])
        self.assertEqual(self.source.read_bytes(), SOURCE)

    def test_sync_failure_does_not_publish_and_retry_succeeds(self):
        before = self.snapshot()
        with patch.object(os, "fsync", side_effect=OSError("subtitle sync failed")):
            with self.assertRaisesRegex(OSError, "subtitle sync failed"):
                self.adopt()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.destination.parent.iterdir()), [])
        self.assertEqual(self.source.read_bytes(), SOURCE)
        self.assertFalse(self.adopt()["reused"])

    def test_http_adoption_preconditions_and_rejection_preserve_state(self):
        endpoint = f"/api/v1/sessions/{self.session_id}/sources/adopt-subtitles"
        before = self.snapshot()
        for payload, extra, status, code in (
            ({"source_asset_id": self.asset_id}, {"If-Match": "bad"}, 422, "validation_error"),
            ({"source_asset_id": self.asset_id}, {"If-Match": '"999"'}, 409, "revision_conflict"),
            ({"source_asset_id": self.asset_id, "role": "media"}, {}, 422, "validation_error"),
            ({"source_asset_id": "missing"}, {}, 404, "not_found"),
        ):
            with self.subTest(payload=payload, extra=extra):
                response = self.client.post(
                    endpoint, json=payload, headers={**self.headers, **extra}
                )
                self.assertEqual(response.status_code, status, response.get_json())
                self.assertEqual(response.get_json()["error"]["code"], code)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.source.read_bytes(), SOURCE)
                self.assertFalse(self.destination.exists())

    def test_http_fresh_and_reused_adoption_return_current_etag(self):
        endpoint = f"/api/v1/sessions/{self.session_id}/sources/adopt-subtitles"
        first = self.client.post(
            endpoint, json={"source_asset_id": self.asset_id}, headers=self.headers
        )
        self.assertEqual(first.status_code, 201, first.get_json())
        self.assertFalse(first.get_json()["reused"])
        self.assertEqual(first.headers["ETag"], f'"{first.get_json()["session_revision"]}"')
        for value in (first.headers["ETag"], ""):
            second = self.client.post(
                endpoint,
                json={"source_asset_id": self.asset_id},
                headers={**self.headers, "If-Match": value},
            )
            self.assertEqual(second.status_code, 200, second.get_json())
            self.assertTrue(second.get_json()["reused"])
            self.assertEqual(second.headers["ETag"], first.headers["ETag"])

    def test_openapi_describes_optional_precondition_and_both_success_statuses(self):
        path = "/api/v1/sessions/{sessionId}/sources/adopt-subtitles"
        operation = build_openapi_document()["paths"][path]["post"]
        header = next(
            item for item in operation.get("parameters", []) if item["name"] == "If-Match"
        )
        self.assertEqual(header["in"], "header")
        self.assertFalse(header["required"])
        self.assertTrue({"200", "201", "404", "409", "422"}.issubset(operation["responses"]))
        for status in ("200", "201"):
            self.assertEqual(
                operation["responses"][status]["headers"]["ETag"]["schema"], {"type": "string"}
            )
        generated = json.loads((Path(__file__).resolve().parents[1] / "openapi.json").read_text())
        self.assertEqual(generated["paths"][path]["post"], operation)
