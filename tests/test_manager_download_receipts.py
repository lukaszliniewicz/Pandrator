import hashlib
import io
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import requests

from pandrator_manager.application import create_application
from pandrator_manager.artifacts import ArtifactDownloader, ArtifactSpec
from pandrator_manager.context import CancellationToken
from pandrator_manager.models import (
    OperationKind,
    OperationPlan,
    OperationRecord,
    OperationState,
    TaskSpec,
)
from pandrator_manager.operations import FilesystemTaskHandler, OperationTaskContext
from pandrator_manager.releases import release_cache_path
from pandrator_manager.releases.models import ReleaseArtifact

PAYLOAD = b"verified-release-fixture"
URL = "https://releases.example.invalid/fixture.zip"


class ReleaseDownloadReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="manager-download-receipt-")
        self.addCleanup(temporary.cleanup)
        self.application = create_application(Path(temporary.name) / "workspace")
        self.artifact = ReleaseArtifact(
            filename="fixture.zip",
            url=URL,
            sha256=hashlib.sha256(PAYLOAD).hexdigest(),
            size_bytes=len(PAYLOAD),
            kind="zip",
            systems=(self.application.context.system,),
            architectures=(self.application.context.architecture,),
        )
        self.destination = release_cache_path(
            self.application.context.layout,
            self.artifact,
        )

    def _populate(self, content: bytes) -> None:
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        self.destination.write_bytes(content)

    def _execute(self, *, offline: bool = False) -> dict:
        task = TaskSpec(
            id="release:download",
            kind="download_release",
            label="Fixture release download",
            inputs={
                "artifact": self.artifact.model_dump(mode="json"),
                "offline": offline,
            },
        )
        now = datetime.now(UTC)
        plan = OperationPlan(
            id="receipt-plan",
            kind=OperationKind.UPDATE,
            workspace=str(self.application.context.layout.root),
            expected_revision=0,
            desired={},
            inspections={},
            tasks=(task,),
            created_at=now,
            expires_at=now + timedelta(hours=1),
            digest="receipt-fixture",
        )
        operation = OperationRecord(
            id="receipt-operation",
            plan_id=plan.id,
            kind=plan.kind,
            state=OperationState.RUNNING,
        )
        execution = OperationTaskContext(
            context=self.application.context,
            store=self.application.store,
            registry=self.application.registry,
            supervisor=None,
            operation=operation,
            plan=plan,
            prior_results={},
            cancellation=CancellationToken(),
        )
        return FilesystemTaskHandler().execute(execution, task)

    @contextmanager
    def _http_response(self, payload: bytes = PAYLOAD) -> Iterator[tuple[mock.Mock, mock.Mock]]:
        raw = io.BytesIO(payload)
        response = requests.Response()
        response.status_code = 200
        response.url = URL
        response.headers["Content-Length"] = str(len(payload))
        response.raw = raw
        self.addCleanup(raw.close)
        self.addCleanup(response.close)

        def open_response(downloader: ArtifactDownloader, url: str) -> requests.Response:
            self.assertEqual(url, URL)
            self.addCleanup(downloader.session.close)
            return response

        with (
            mock.patch.object(response, "close", wraps=response.close) as close_response,
            mock.patch.object(
                ArtifactDownloader,
                "_open_https",
                autospec=True,
                side_effect=open_response,
            ) as open_http,
        ):
            yield open_http, close_response

    def _assert_published_receipt(self, receipt: dict, *, reused: bool) -> None:
        self.assertEqual(set(receipt), {"artifact_path", "artifact", "cache_reused"})
        self.assertEqual(receipt["artifact_path"], str(self.destination.resolve()))
        self.assertEqual(receipt["artifact"], self.artifact.model_dump(mode="json"))
        self.assertEqual(self.destination.read_bytes(), PAYLOAD)
        self.assertEqual(list(self.destination.parent.glob("*.part")), [])
        self.assertIs(receipt["cache_reused"], reused)

    def test_absent_cache_fresh_receipt(self) -> None:
        self.assertFalse(self.destination.exists())
        with self._http_response() as (open_http, close_response):
            receipt = self._execute()
            self.assertEqual(open_http.call_count, 1)
            close_response.assert_called_once_with()
            self._assert_published_receipt(receipt, reused=False)

    def test_verified_cache_offline_receipt(self) -> None:
        self._populate(PAYLOAD)
        with mock.patch.object(
            ArtifactDownloader, "_open_https", side_effect=AssertionError("Unexpected HTTP")
        ) as open_http:
            receipt = self._execute(offline=True)
            open_http.assert_not_called()
            self._assert_published_receipt(receipt, reused=True)

    def test_invalid_size_cache_replaced_receipt(self) -> None:
        self._populate(b"old")
        with self._http_response() as (open_http, close_response):
            receipt = self._execute()
            self.assertEqual(open_http.call_count, 1)
            close_response.assert_called_once_with()
            self._assert_published_receipt(receipt, reused=False)

    def test_same_size_invalid_digest_cache_replaced_receipt(self) -> None:
        self._populate(b"x" * len(PAYLOAD))
        with self._http_response() as (open_http, close_response):
            receipt = self._execute()
            self.assertEqual(open_http.call_count, 1)
            close_response.assert_called_once_with()
            self._assert_published_receipt(receipt, reused=False)

    def test_invalid_cache_offline_preserves_bytes(self) -> None:
        old = b"invalid cached bytes"
        self._populate(old)
        with mock.patch.object(
            ArtifactDownloader, "_open_https", side_effect=AssertionError("Unexpected HTTP")
        ) as open_http:
            with self.assertRaisesRegex(FileNotFoundError, "verified artifact"):
                self._execute(offline=True)
            open_http.assert_not_called()
        self.assertEqual(self.destination.read_bytes(), old)
        self.assertEqual(list(self.destination.parent.glob("*.part")), [])

    def test_wrong_download_digest_preserves_cache_and_cleans_partial(self) -> None:
        old = b"old invalid cache"
        self._populate(old)
        with self._http_response(b"!" * len(PAYLOAD)) as (open_http, close_response):
            with self.assertRaisesRegex(ValueError, "SHA-256 verification failed"):
                self._execute()
            self.assertEqual(open_http.call_count, 1)
            close_response.assert_called_once_with()
        self.assertEqual(self.destination.read_bytes(), old)
        self.assertEqual(list(self.destination.parent.glob("*.part")), [])

    def test_public_download_fresh_keeps_path_return(self) -> None:
        downloader = ArtifactDownloader(cancellation=CancellationToken())
        self.addCleanup(downloader.session.close)
        spec = ArtifactSpec(url=URL, sha256=self.artifact.sha256, size_bytes=len(PAYLOAD))
        with self._http_response() as (open_http, close_response):
            selected = downloader.download(spec, self.destination)
            self.assertEqual(open_http.call_count, 1)
            close_response.assert_called_once_with()
        self.assertIsInstance(selected, Path)
        self.assertEqual(selected, self.destination.resolve())
        self.assertEqual(selected.read_bytes(), PAYLOAD)
        self.assertEqual(list(self.destination.parent.glob("*.part")), [])

    def test_public_download_verified_offline_keeps_path_return(self) -> None:
        self._populate(PAYLOAD)
        downloader = ArtifactDownloader(cancellation=CancellationToken())
        self.addCleanup(downloader.session.close)
        spec = ArtifactSpec(url=URL, sha256=self.artifact.sha256, size_bytes=len(PAYLOAD))
        with mock.patch.object(
            ArtifactDownloader, "_open_https", side_effect=AssertionError("Unexpected HTTP")
        ) as open_http:
            selected = downloader.download(spec, self.destination, offline=True)
            open_http.assert_not_called()
        self.assertIsInstance(selected, Path)
        self.assertEqual(selected, self.destination.resolve())
        self.assertEqual(selected.read_bytes(), PAYLOAD)
        self.assertEqual(list(self.destination.parent.glob("*.part")), [])
