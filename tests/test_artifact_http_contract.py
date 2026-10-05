"""Native artifact review admission and cached file HTTP contracts."""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from flask.testing import FlaskClient
from sqlalchemy import select
from werkzeug.test import TestResponse

from pandrator.web.api import create_app
from pandrator.web.artifacts import ArtifactService
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.database import Database
from pandrator.web.models import Artifact, ArtifactEdge, Job
from tests.web_test_support import prepare_web_test_data_root

CONTENT = b"0123456789 fixture content\n"
PEAKS = b'{"points":[0.5],"channels":1}'
SOURCE_ROWS = [
    {"source_text": "Original zero", "text": "Old zero", "stable": "row-zero"},
    {"source_text": "Original one", "text": "Old one", "stable": "row-one"},
]
SOURCE_METADATA = {"packet": "review", "stable": ["keep"]}


@dataclass
class Harness:
    client: FlaskClient
    database: Database
    artifacts: ArtifactService
    directory: Path
    session_id: str
    csrf: str


@pytest.fixture
def harness() -> Iterator[Harness]:
    with tempfile.TemporaryDirectory(prefix="pandrator-artifact-http-") as temporary:
        paths = prepare_web_test_data_root(temporary)
        bootstrap = BootstrapTokenStore()
        app = create_app(data_root=temporary, testing=True, bootstrap_tokens=bootstrap)
        services = app.extensions["pandrator"]
        database: Database = services["database"]
        try:
            client = app.test_client()
            with client.post(
                "/api/v1/auth/bootstrap", json={"token": bootstrap.issue()}
            ) as response:
                assert response.status_code == 200
                csrf = response.get_json()["csrf_token"]
            session = services["sessions"].create("Artifact HTTP fixture")
            directory = paths.sessions / session.storage_key
            directory.mkdir(parents=True, exist_ok=True)
            yield Harness(client, database, services["artifacts"], directory, session.id, csrf)
        finally:
            database.dispose()


def row_inventory(harness: Harness) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    with harness.database.session() as session:
        artifacts = [
            dict(row)
            for row in session.execute(select(Artifact.__table__).order_by(Artifact.id)).mappings()
        ]
        edges = [
            dict(row)
            for row in session.execute(
                select(ArtifactEdge.__table__).order_by(
                    ArtifactEdge.parent_artifact_id, ArtifactEdge.child_artifact_id
                )
            ).mappings()
        ]
    return artifacts, edges


def file_inventory(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


def assert_no_jobs(harness: Harness) -> None:
    with harness.database.session() as session:
        assert list(session.scalars(select(Job)).all()) == []


@pytest.mark.parametrize("case", ("duplicate", "whitespace", "valid", "missing", "empty"))
def test_review_admission_preserves_source_and_native_registration(
    harness: Harness, case: str
) -> None:
    source_path = harness.directory / "optimized.json"
    source_path.write_text(json.dumps(SOURCE_ROWS), encoding="utf-8")
    source = harness.artifacts.register(
        source_path,
        kind="json",
        role="tts_optimized",
        session_id=harness.session_id,
        metadata=SOURCE_METADATA,
    )
    items = {
        "duplicate": [
            {"index": 0, "text": "First zero"},
            {"index": 1, "text": "Reviewed one"},
            {"index": 0, "text": "Second zero"},
        ],
        "whitespace": [{"index": 0, "text": " \t "}, {"index": 1, "text": "Reviewed one"}],
        "valid": [
            {"index": 1, "text": "  Reviewed one  "},
            {"index": 0, "text": "  Reviewed zero  "},
        ],
        "missing": [{"index": 0, "text": "Reviewed zero"}],
        "empty": [{"index": 0, "text": ""}, {"index": 1, "text": "Reviewed one"}],
    }[case]
    source_bytes = source_path.read_bytes()
    before_rows = row_inventory(harness)
    before_files = file_inventory(harness.directory)
    with harness.client.post(
        f"/api/v1/artifacts/{source.id}/optimization-review",
        json={"items": items},
        headers={"X-CSRF-Token": harness.csrf},
    ) as response:
        assert isinstance(response, TestResponse)
        after_rows = row_inventory(harness)
        after_files = file_inventory(harness.directory)
        print(
            "REVIEW_NATIVE_STATE:"
            + json.dumps(
                {
                    "case": case,
                    "status": response.status_code,
                    "source_unchanged": source_path.read_bytes() == source_bytes,
                    "rows_unchanged": after_rows == before_rows,
                    "files_unchanged": after_files == before_files,
                    "artifact_count": len(after_rows[0]),
                    "edge_count": len(after_rows[1]),
                    "files": sorted(after_files),
                }
            )
        )
        assert source_path.read_bytes() == source_bytes
        if case != "valid":
            assert response.status_code == 422
            assert response.get_json()["error"]["code"] == "validation_error"
            assert after_rows == before_rows
            assert after_files == before_files
            return
        assert response.status_code == 201
        result = response.get_json()
        reviewed, reviewed_path = harness.artifacts.resolve(result["id"])
        assert reviewed.id != source.id
        assert reviewed.session_id == harness.session_id
        assert reviewed.kind == "json" and reviewed.role == "tts_optimized"
        assert reviewed.metadata_json == {
            **SOURCE_METADATA,
            "reviewed": True,
            "reviewed_from": source.id,
        }
        assert json.loads(reviewed_path.read_bytes()) == [
            {
                "source_text": "Original zero",
                "text": "Reviewed zero",
                "stable": "row-zero",
                "processed_sentence": "Reviewed zero",
                "tts_optimized_sentence": "Reviewed zero",
                "optimization_reviewed": True,
            },
            {
                "source_text": "Original one",
                "text": "Reviewed one",
                "stable": "row-one",
                "processed_sentence": "Reviewed one",
                "tts_optimized_sentence": "Reviewed one",
                "optimization_reviewed": True,
            },
        ]
        assert len(after_rows[0]) == len(before_rows[0]) + 1
        assert after_rows[1] == [
            {
                "parent_artifact_id": source.id,
                "child_artifact_id": reviewed.id,
                "relation": "derived_from",
            }
        ]
        assert len(after_files) == len(before_files) + 1


def content_fixture(harness: Harness) -> tuple[Artifact, Path]:
    path = harness.directory / "content.txt"
    path.write_bytes(CONTENT)
    artifact = harness.artifacts.register(
        path, kind="text", role="extracted_text", session_id=harness.session_id
    )
    return artifact, path


@pytest.mark.parametrize("route", ("content", "waveform"))
@pytest.mark.parametrize("hash_kind", ("normal", "none", "empty"))
def test_cached_file_body_etag_and_conditional_native_http(
    harness: Harness, route: str, hash_kind: str
) -> None:
    source, _path = content_fixture(harness)
    served = source
    body = CONTENT
    mime_type = "text/plain"
    url = f"/api/v1/artifacts/{source.id}/content"
    if route == "waveform":
        path = harness.directory / "peaks.json"
        path.write_bytes(PEAKS)
        served = harness.artifacts.register(
            path,
            kind="json",
            role="waveform_peaks",
            session_id=harness.session_id,
            parent_ids=[source.id],
            metadata={"max_points": 128, "start_ms": 0},
        )
        body = PEAKS
        mime_type = "application/json"
        url = f"/api/v1/artifacts/{source.id}/waveform?points=128"
    normal_hash = hashlib.sha256(body).hexdigest()
    assert served.content_hash == normal_hash
    expected_etag: str | None = f'"{normal_hash}"'
    if hash_kind != "normal":
        # Characterize legacy nullable/empty ORM values, not producer admission.
        with harness.database.session() as session:
            record = session.get(Artifact, served.id)
            assert record is not None
            record.content_hash = None if hash_kind == "none" else ""
        expected_etag = None if hash_kind == "none" else '""'
    assert_no_jobs(harness)
    with harness.client.get(url) as response:
        assert isinstance(response, TestResponse)
        assert response.status_code == 200
        assert response.data == body
        assert response.mimetype == mime_type
        assert response.headers.get("ETag") == expected_etag
    with harness.client.get(
        url, headers={"If-None-Match": expected_etag or '"fixture-nohash"'}
    ) as response:
        assert response.status_code == (304 if hash_kind == "normal" else 200)
        assert response.data == (b"" if hash_kind == "normal" else body)
        assert response.headers.get("ETag") == expected_etag
    assert_no_jobs(harness)


def test_normal_content_range_uses_native_file_response(harness: Harness) -> None:
    source, _path = content_fixture(harness)
    with harness.client.get(
        f"/api/v1/artifacts/{source.id}/content", headers={"Range": "bytes=0-3"}
    ) as response:
        assert response.status_code == 206
        assert response.data == b"0123"
        assert response.headers["Content-Range"] == f"bytes 0-3/{len(CONTENT)}"
        assert response.headers["ETag"] == f'"{hashlib.sha256(CONTENT).hexdigest()}"'


def test_normal_content_head_has_length_without_file_body(harness: Harness) -> None:
    source, _path = content_fixture(harness)
    with harness.client.head(f"/api/v1/artifacts/{source.id}/content") as response:
        assert response.status_code == 200
        assert response.data == b""
        assert response.content_length == len(CONTENT)
        assert response.headers["ETag"] == f'"{hashlib.sha256(CONTENT).hexdigest()}"'


@pytest.mark.parametrize("missing", ("file", "artifact"))
def test_content_missing_file_and_unknown_artifact_controls(harness: Harness, missing: str) -> None:
    artifact_id = "unknown-fixture-artifact"
    if missing == "file":
        source, path = content_fixture(harness)
        artifact_id = source.id
        path.unlink()
    with harness.client.get(f"/api/v1/artifacts/{artifact_id}/content") as response:
        assert response.status_code == (410 if missing == "file" else 404)
        assert response.get_json()["error"]["code"] == (
            "artifact_missing" if missing == "file" else "not_found"
        )
