"""Project files cannot acquire a visiting editor's application-origin authority."""

import io
import wave

from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401


def _upload(case, filename, data):
    created = case.client.post(
        "/api/v1/sessions",
        json={"name": "Untrusted document", "workflow_kind": "audiobook"},
        headers={"X-CSRF-Token": case.csrf},
    )
    assert created.status_code == 201
    result = case.client.post(
        "/api/v1/uploads",
        data={
            "session_id": created.get_json()["id"],
            "attach_to_session": "false",
            "file": (io.BytesIO(data), filename),
        },
        headers={"X-CSRF-Token": case.csrf},
    )
    assert result.status_code == 201
    return f"/api/v1/artifacts/{result.get_json()['artifact_id']}/content"


def test_artifact_document_navigation_has_an_opaque_origin_and_no_script_permission(case):
    content = b"<!doctype html><title>Untrusted project document</title>"
    path = _upload(case, "document.html", content)
    for method in ("get", "head"):
        response = getattr(case.client, method)(path)
        assert response.status_code == 200
        policy = response.headers["Content-Security-Policy"]
        sandbox = next(
            part.strip() for part in policy.split(";") if part.strip().startswith("sandbox")
        )
        assert sandbox.split() == ["sandbox", "allow-downloads"]
        assert "no-store" in response.headers["Cache-Control"]
        assert response.mimetype == "text/html"
        assert response.data == (content if method == "get" else b"")
    # Application documents retain their normal script/bootstrap policy.
    assert "sandbox" not in case.client.get("/").headers["Content-Security-Policy"]


def test_artifact_sandbox_preserves_audio_ranges_and_bytes(case):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\0\0" * 8000)
    content = buffer.getvalue()
    path = _upload(case, "sound.wav", content)
    response = case.client.get(path, headers={"Range": "bytes=0-15"})
    assert response.status_code == 206
    assert response.data == content[:16]
    assert response.headers["Content-Range"] == f"bytes 0-15/{len(content)}"
    assert "sandbox allow-downloads" in response.headers["Content-Security-Policy"]
