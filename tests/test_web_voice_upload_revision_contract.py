"""Native multipart admission and its separately documented revision contract."""

import io
import json
import wave
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from flask.testing import FlaskClient
from sqlalchemy import select

from pandrator.runtime import DataPaths
from pandrator.web.api import create_app
from pandrator.web.artifacts import ArtifactService
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.database import Database
from pandrator.web.models import Artifact, Job, Voice, VoiceSample
from pandrator.web.openapi import build_openapi_document
from tests.web_test_support import prepare_web_test_data_root

UPLOAD_PATH = "/api/v1/voices/{voiceId}/samples"
REPLACE_PATH = "/api/v1/voices/{voiceId}/samples/{sampleId}/replace"


def _recording() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\0\0" * 160)
    return output.getvalue()


@dataclass
class UploadCase:
    client: FlaskClient
    database: Database
    paths: DataPaths
    artifacts: ArtifactService
    csrf: str
    voice_id: str
    revision: int
    sample_id: str


@pytest.fixture
def upload_case(tmp_path: Path) -> Iterator[UploadCase]:
    paths = prepare_web_test_data_root(tmp_path)
    bootstrap = BootstrapTokenStore()
    token = bootstrap.issue()
    app = create_app(data_root=tmp_path, testing=True, bootstrap_tokens=bootstrap)
    client = app.test_client()
    csrf = client.post("/api/v1/auth/bootstrap", json={"token": token}).get_json()["csrf_token"]
    database = app.extensions["pandrator"]["database"]
    artifacts = app.extensions["pandrator"]["artifacts"]
    try:
        with database.session() as session:
            voice = Voice(name="Revision contract narrator", language="en", revision=7)
            session.add(voice)
            session.flush()
            voice_id, revision = voice.id, voice.revision
        original_path = paths.voices / voice_id / "original.wav"
        original_path.parent.mkdir(parents=True, exist_ok=True)
        original_path.write_bytes(_recording())
        artifact = artifacts.register(original_path, kind="audio", role="voice_sample")
        with database.session() as session:
            sample = VoiceSample(
                voice_id=voice_id,
                artifact_id=artifact.id,
                transcript="Existing reference",
                transcript_reviewed=True,
            )
            session.add(sample)
            session.flush()
            sample_id = sample.id
        yield UploadCase(client, database, paths, artifacts, csrf, voice_id, revision, sample_id)
    finally:
        database.dispose()


def _rows(database: Database) -> dict[str, list[tuple[object, ...]]]:
    """Snapshot every stored column of the admission-owned rows."""
    result = {}
    with database.session() as session:
        for model in (Job, Artifact, VoiceSample, Voice):
            table = model.__table__
            result[model.__tablename__] = [
                tuple(row) for row in session.execute(select(table).order_by(table.c.id))
            ]
    return result


def _managed_paths(paths: DataPaths) -> dict[str, bytes | None]:
    result = {}
    for label, root in (("temporary", paths.temporary), ("voices", paths.voices)):
        for path in root.rglob("*"):
            result[f"{label}/{path.relative_to(root)}"] = (
                path.read_bytes() if path.is_file() else None
            )
    return result


def _revision_input(value: str | None, revision: int, *, header: bool = False) -> str | None:
    if value == "current":
        value = str(revision)
    elif value == "stale":
        value = str(revision - 1)
    if header and value is not None and value != "malformed":
        return f'"{value}"'
    return value


@pytest.mark.parametrize(
    ("replace", "form", "header", "status"),
    [
        pytest.param(False, "current", None, 202, id="upload-form-only"),
        pytest.param(False, None, "current", 202, id="upload-quoted-header-only"),
        pytest.param(False, "current", "stale", 202, id="upload-form-current-header-stale"),
        pytest.param(False, "stale", "current", 409, id="upload-form-stale-header-current"),
        pytest.param(False, "malformed", "current", 202, id="upload-invalid-form-header-current"),
        pytest.param(False, "current", "malformed", 202, id="upload-form-current-header-malformed"),
        pytest.param(False, None, None, 428, id="upload-both-missing"),
        pytest.param(False, "malformed", "malformed", 428, id="upload-both-malformed"),
        pytest.param(False, None, "stale", 409, id="upload-stale-header"),
        pytest.param(True, "current", None, 428, id="replace-form-only"),
        pytest.param(True, None, "current", 202, id="replace-header-current"),
        pytest.param(True, "stale", "current", 202, id="replace-form-stale-header-current"),
        pytest.param(True, "current", "stale", 409, id="replace-header-stale-form-current"),
        pytest.param(True, "current", "malformed", 428, id="replace-header-malformed-form-current"),
    ],
)
def test_native_multipart_revision_admission(
    upload_case: UploadCase,
    replace: bool,
    form: str | None,
    header: str | None,
    status: int,
) -> None:
    case = upload_case
    before_rows = _rows(case.database)
    before_paths = _managed_paths(case.paths)
    data: dict[str, str | tuple[io.BytesIO, str]] = {
        "file": (io.BytesIO(_recording()), "incoming.wav"),
    }
    form_value = _revision_input(form, case.revision)
    if form_value is not None:
        data["expected_revision"] = form_value
    headers = {"X-CSRF-Token": case.csrf}
    header_value = _revision_input(header, case.revision, header=True)
    if header_value is not None:
        headers["If-Match"] = header_value
    url = f"/api/v1/voices/{case.voice_id}/samples"
    if replace:
        url += f"/{case.sample_id}/replace"
    response = case.client.post(url, data=data, content_type="multipart/form-data", headers=headers)
    body = response.get_json()
    assert response.status_code == status, body
    with case.database.session() as session:
        voice = session.get(Voice, case.voice_id)
        assert voice is not None
        assert voice.revision == case.revision
    if status != 202:
        assert body["error"]["code"] == (
            "precondition_required" if status == 428 else "revision_conflict"
        )
        assert _rows(case.database) == before_rows
        assert _managed_paths(case.paths) == before_paths
        return

    with case.database.session() as session:
        job = session.get(Job, body["id"])
        assert job is not None
        assert job.kind == "voice.normalize_recording"
        assert job.payload_json["voice_id"] == case.voice_id
        assert job.payload_json["expected_voice_revision"] == case.revision
        if replace:
            assert job.payload_json["replace_sample_id"] == case.sample_id
        else:
            assert "replace_sample_id" not in job.payload_json
        source_id = job.payload_json["source_artifact_id"]
    source, source_path = case.artifacts.resolve(source_id)
    assert source.role == "recording_upload"
    assert source_path.is_file()
    assert source_path.read_bytes() == _recording()
    after_rows = _rows(case.database)
    assert after_rows["voices"] == before_rows["voices"]
    assert after_rows["voice_samples"] == before_rows["voice_samples"]
    assert len(after_rows["jobs"]) == len(before_rows["jobs"]) + 1
    assert len(after_rows["artifacts"]) == len(before_rows["artifacts"]) + 1
    after_paths = _managed_paths(case.paths)
    assert all(after_paths[name] == content for name, content in before_paths.items())


def test_source_openapi_upload_header_is_optional() -> None:
    operation = build_openapi_document()["paths"][UPLOAD_PATH]["post"]
    header = next(
        parameter
        for parameter in operation["parameters"]
        if parameter["name"] == "If-Match" and parameter["in"] == "header"
    )
    assert header["required"] is False
    assert header["schema"]["type"] == "string"


def test_source_openapi_upload_documents_optional_form_revision() -> None:
    operation = build_openapi_document()["paths"][UPLOAD_PATH]["post"]
    body = operation["requestBody"]
    assert body["required"] is True
    schema = body["content"]["multipart/form-data"]["schema"]
    assert schema["properties"]["expected_revision"]["type"] == "integer"
    assert "expected_revision" not in schema["required"]
    assert "file" in schema["required"]
    assert schema["properties"]["file"] == {"type": "string", "format": "binary"}


@pytest.mark.parametrize("path", [UPLOAD_PATH, REPLACE_PATH], ids=["upload", "replacement"])
def test_source_openapi_documents_precondition_required(path: str) -> None:
    operation = build_openapi_document()["paths"][path]["post"]
    assert "428" in operation["responses"]


def test_source_openapi_replacement_requires_header_without_form_revision() -> None:
    operation = build_openapi_document()["paths"][REPLACE_PATH]["post"]
    header = next(
        parameter
        for parameter in operation["parameters"]
        if parameter["name"] == "If-Match" and parameter["in"] == "header"
    )
    assert header["required"] is True
    schema = operation["requestBody"]["content"]["multipart/form-data"]["schema"]
    assert "expected_revision" not in schema["properties"]
    assert "file" in schema["required"]


@pytest.mark.parametrize("path", [UPLOAD_PATH, REPLACE_PATH], ids=["upload", "replacement"])
def test_generated_openapi_multipart_operation_matches_source(path: str) -> None:
    document = json.loads((Path(__file__).resolve().parents[1] / "openapi.json").read_text())
    assert document["paths"][path]["post"] == build_openapi_document()["paths"][path]["post"]
