"""Remote confirmation must precede native voice registration reconciliation."""

from __future__ import annotations

import hashlib
import io
import threading
import wave
from typing import Any, cast
from unittest import mock

import pytest
from sqlalchemy import Table, select

from pandrator.logic import tts_handler
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.models import Artifact, Voice, VoiceSample
from pandrator.web.workflow_handlers import WorkflowHandlers
from tests.test_tts_voice_delete_http import API_KEY, BASE_URL, VOICE_ID, HttpScript, response
from tests.web_test_support import prepare_web_test_data_root

SERVICE = {
    "id": "kobold_qwen",
    "name": "Qwen3 TTS",
    "adapter": "kobold_qwen",
    "api_base": BASE_URL,
    "api_key": API_KEY,
    "supports_voice_deletion": True,
}


def sample_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 160)
    return output.getvalue()


def row_snapshot(database: Database) -> dict[str, list[dict[str, Any]]]:
    """Independent SQL Connection observes all persisted columns, without ORM caching."""
    with database.engine.connect() as connection:
        return {
            table.name: [dict(row) for row in connection.execute(select(table)).mappings()]
            for table in (
                cast(Table, Voice.__table__),
                cast(Table, Artifact.__table__),
                cast(Table, VoiceSample.__table__),
            )
        }


@pytest.mark.parametrize(
    ("payload", "delete_status", "expected"),
    (
        ({}, 404, RuntimeError),
        ({"error": "busy"}, 404, RuntimeError),
        ({"data": [{}]}, 404, RuntimeError),
        ({"data": []}, 404, False),
        (None, 204, True),
    ),
    ids=("unknown-object", "error-object", "unknown-entry", "verified-absence", "deleted"),
)
def test_native_unpublish_requires_confirmation_and_retains_samples(
    tmp_path,
    payload: object,
    delete_status: int,
    expected: bool | type[Exception],
) -> None:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    handler: WorkflowHandlers | None = None
    try:
        fingerprint = hashlib.sha256(BASE_URL.rstrip("/").casefold().encode("utf-8")).hexdigest()
        with database.session() as session:
            voice = Voice(
                name="Disposable deletion narrator",
                language="en",
                description="Retain this voice and its sample.",
                metadata_json={
                    "fixture": "retained metadata",
                    "providers": {
                        "kobold_qwen": {
                            "voice_id": VOICE_ID,
                            "status": "ready",
                            "managed_by": "pandrator",
                            "endpoint_fingerprint": fingerprint,
                        }
                    },
                },
            )
            session.add(voice)
            session.flush()
            voice_id = voice.id
            revision = voice.revision
        sample_path = paths.voices / voice_id / "sample.wav"
        sample_path.parent.mkdir(parents=True)
        sample_bytes = sample_wav()
        sample_path.write_bytes(sample_bytes)
        artifact = ArtifactService(database, paths).register(
            sample_path, kind="audio", role="voice_sample", metadata={"fixture": "retain"}
        )
        with database.session() as session:
            session.add(
                VoiceSample(
                    voice_id=voice_id,
                    artifact_id=artifact.id,
                    transcript="Reviewed disposable sample.",
                    transcript_language="en",
                    transcript_reviewed=True,
                )
            )
        before = row_snapshot(database)
        handler = WorkflowHandlers(database, paths)
        deletes = (response(204),) if delete_status == 204 else (response(404), response(404))
        gets = (
            ()
            if delete_status == 204
            else (response(200, payload),)
            if isinstance(expected, bool)
            else (response(200, payload), response(200, payload))
        )
        script = HttpScript(deletes, gets)
        result: dict[str, Any] | None = None
        caught: Exception | None = None
        with (
            mock.patch.object(tts_handler, "get_service_config", return_value=SERVICE.copy()),
            script.installed(),
        ):
            try:
                result = handler.unpublish_voice(
                    {
                        "voice_id": voice_id,
                        "service_id": "kobold_qwen",
                        "service": "Qwen3 TTS",
                        "expected_voice_revision": revision,
                    },
                    lambda *_args: None,
                    threading.Event(),
                )
            except Exception as error:
                caught = error
        after = row_snapshot(database)
        assert after["artifacts"] == before["artifacts"]
        assert after["voice_samples"] == before["voice_samples"]
        assert sample_path.read_bytes() == sample_bytes
        if isinstance(expected, bool):
            assert caught is None, f"Unexpected fixture/workflow exception: {caught!r}"
            assert result is not None
            assert result["remote_deleted"] is expected
            assert result["voice_revision"] == revision + 1
            stored = after["voices"][0]
            assert stored["revision"] == revision + 1
            assert "kobold_qwen" not in stored["metadata_json"]["providers"]
            assert stored["metadata_json"]["fixture"] == "retained metadata"
        else:
            assert after["voices"] == before["voices"], (
                f"Unconfirmed catalogue mutated the full Voice row; result={result!r}, "
                f"exception={caught!r}, HTTP calls={script.calls!r}"
            )
            assert isinstance(caught, expected), f"Expected RuntimeError, got {result!r}"
            assert "could not be verified" in str(caught)
        script.assert_calls(VOICE_ID, deletes=len(deletes), gets=len(gets))
    finally:
        try:
            if handler is not None:
                handler.tts_providers.close()
        finally:
            database.dispose()
