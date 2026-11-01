"""Native file references cannot cross private workspace boundaries."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import sys
import wave
import zipfile
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from pandrator.web import models as m
from pandrator.web.accounts.execution_actor import HostActor, execution_actor_scope
from pandrator.web.jobs import Worker
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, team
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401
from tests.test_purge_workspace_http import project
from tests.test_session_bundle_import_http import _admit, _execute, _upload

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is unavailable")


def _fixture(case):
    return SimpleNamespace(case=case, services=case.app.extensions["pandrator"]["services"])


def _import_media(f, client, auth, filename, payload):
    member = "files/" + filename
    manifest = {
        "format": "pandrator-session",
        "version": 1,
        "session": {"name": "Synthetic native input", "workflow_kind": "voiceover"},
        "artifacts": [
            {
                "source_id": "owned-source",
                "kind": "audio",
                "role": "upload",
                "archive_path": member,
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "metadata": {},
            }
        ],
        "artifact_edges": [],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr(member, payload)
    source_id, _ = _upload(f, client, auth, buffer.getvalue())
    imported = _execute(f, _admit(client, auth, source_id))
    assert imported.status == "succeeded", imported.error_message
    with f.case.database.snapshot_session() as unit:
        artifact = unit.scalars(
            select(m.Artifact).where(m.Artifact.session_id == imported.result_json["session_id"])
        ).one()
        return artifact.id, artifact.session_id, artifact.workspace_id


def _preview(f, client, auth, artifact_id):
    queued = body(client.post(f"/api/v1/artifacts/{artifact_id}/audio-preview", headers=auth), 202)
    assert Worker(
        f.services.jobs, "private-native-input-regression", f.services.workflow_handlers.handlers()
    ).run_once()
    return f.services.jobs.get(queued["job_id"])


def _trace_native_decoder(monkeypatch, directory):
    executable = shutil.which("ffmpeg")
    assert executable is not None
    monkeypatch.setenv("PANDRATOR_FFMPEG_EXE", executable)
    tracer = shutil.which("strace") if sys.platform == "linux" else None
    if tracer is None:
        return None
    trace = directory / "native-open.trace"
    wrapper = directory / "recorded-ffmpeg"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        f"os.execv({tracer!r}, [{tracer!r}, '-f', '-e', 'trace=openat', "
        f"'-s', '4096', '-o', {str(trace)!r}, {executable!r}, *sys.argv[1:]])\n"
    )
    wrapper.chmod(0o700)
    monkeypatch.setenv("PANDRATOR_FFMPEG_EXE", str(wrapper))
    return trace


@pytest.mark.parametrize("reference_kind", ["hls", "concat"])
def test_imported_reference_cannot_preview_other_private_workspace_bytes(
    case, monkeypatch, tmp_path, reference_kind
):
    f, people = _fixture(case), team(case, monkeypatch)
    private = project(people.bob, people.bh)
    directory = f.services.paths.sessions / private["storage_key"]
    directory.mkdir(parents=True, exist_ok=True)
    private_path = directory / f"private-{uuid4()}.ts"
    subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=48000:duration=1",
            "-c:a",
            "aac",
            "-f",
            "mpegts",
            str(private_path),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    original_hash = hashlib.sha256(private_path.read_bytes()).hexdigest()
    with execution_actor_scope(HostActor("pandrator-cli")):
        private_artifact = f.services.artifacts.register(
            private_path, kind="audio", role="upload", session_id=private["id"]
        )
    with case.database.snapshot_session() as unit:
        assert not unit.get_one(m.User, people.alice_id).is_instance_admin
        private_workspace = unit.get_one(m.Artifact, private_artifact.id).workspace_id
    assert people.alice.get(f"/api/v1/artifacts/{private_artifact.id}/content").status_code == 404
    if reference_kind == "hls":
        name = "owned.m3u8"
        payload = (
            "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n"
            "#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:1.0,\n" + private_path.as_uri() + "\n#EXT-X-ENDLIST\n"
        ).encode()
    else:
        name = "owned.ffconcat"
        escaped = str(private_path).replace("'", "'\\''")
        payload = f"ffconcat version 1.0\nfile '{escaped}'\n".encode()
    owned_id, session_id, owned_workspace = _import_media(f, people.alice, people.ah, name, payload)
    assert owned_workspace != private_workspace
    trace = _trace_native_decoder(monkeypatch, tmp_path)
    denied = _preview(f, people.alice, people.ah, owned_id)
    assert denied.status == "failed" and not denied.result_json
    if trace is not None:
        opened = trace.read_text()
        assert name in opened and private_path.name not in opened
        (tmp_path / "denied-native-open.trace").write_text(opened)
    with case.database.snapshot_session() as unit:
        assert unit.get_one(m.JobActorRecord, denied.id).user_id == people.alice_id
        assert (
            unit.scalar(
                select(func.count())
                .select_from(m.Artifact)
                .where(m.Artifact.session_id == session_id)
            )
            == 1
        )
    allowed = _preview(f, people.bob, people.bh, private_artifact.id)
    assert allowed.status == "succeeded", allowed.error_message
    output_id = allowed.result_json["artifact_id"]
    assert people.bob.get(f"/api/v1/artifacts/{output_id}/content").status_code == 200
    assert people.alice.get(f"/api/v1/artifacts/{output_id}/content").status_code == 404
    assert hashlib.sha256(private_path.read_bytes()).hexdigest() == original_hash


def test_actual_self_contained_format_works_with_unusual_imported_filename(case, monkeypatch):
    f, people = _fixture(case), team(case, monkeypatch)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x10\x27\xf0\xd8" * 8000)
    owned_id, _, _ = _import_media(
        f, people.alice, people.ah, "actual-wave.m3u8", buffer.getvalue()
    )
    completed = _preview(f, people.alice, people.ah, owned_id)
    assert completed.status == "succeeded", completed.error_message
    response = people.alice.get(f"/api/v1/artifacts/{completed.result_json['artifact_id']}/content")
    assert response.status_code == 200 and len(response.data) > 1000
