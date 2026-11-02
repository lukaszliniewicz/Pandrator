from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select

from pandrator.logic.dubbing import crispasr, crispasr_qwen_assets, qwen_alignment
from pandrator.logic.dubbing.transcription_readiness import (
    diagnose_transcription_readiness,
)
from pandrator.web.accounts.credential_resources import CredentialResources
from pandrator.web.accounts.models import Workspace, WorkspaceMembership
from pandrator.web.api import create_app
from pandrator.web.artifacts import ArtifactService
from pandrator.web.models import new_id
from pandrator.web.openapi import build_openapi_document
from pandrator.web.transcription_readiness_routes import (
    ReadinessRequestError,
    _validate_overrides,
)
from tests.web_test_support import prepare_web_test_data_root


def _settings(**values):
    return {
        "stt_engine": "whisper",
        "stt_model_quantization": "f16",
        "stt_language": "en",
        "stt_lid_backend": "off",
        "crispasr_vad_enabled": False,
        **values,
    }


def _write(path: Path, content: bytes = b"model") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _asset(result, kind: str, asset_id: str):
    return next(
        item
        for item in result["assets"]
        if item["kind"] == kind and item["id"] == asset_id
    )


def test_cached_asr_with_missing_default_silero_is_not_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("CRISPASR_CACHE_DIR", str(tmp_path))
    _write(tmp_path / "ggml-large-v3.bin")

    result = diagnose_transcription_readiness(
        _settings(crispasr_vad_enabled=True, crispasr_vad_model="silero")
    )

    assert result["schema_version"] == "1"
    assert result["scope"] == "local_model_files"
    assert result["status"] == "missing"
    assert result["ready"] is False
    assert result["runtime_check_required"] is True
    assert _asset(result, "asr", "whisper_f16") == {
        "kind": "asr",
        "id": "whisper_f16",
        "status": "cached",
        "verification": "nonempty",
    }
    assert _asset(result, "vad", "silero")["status"] == "missing"
    assert str(tmp_path) not in repr(result)


def test_empty_and_explicit_stored_vad_files_are_checked_without_path_disclosure(
    tmp_path,
):
    empty = tmp_path / "empty-vad.bin"
    _write(empty, b"")
    usable = _write(tmp_path / "custom-vad.gguf")

    empty_result = diagnose_transcription_readiness(
        _settings(crispasr_vad_enabled=True, crispasr_vad_model=str(empty))
    )
    usable_result = diagnose_transcription_readiness(
        _settings(crispasr_vad_enabled=True, crispasr_vad_model=str(usable))
    )

    assert _asset(empty_result, "vad", "selected_local_file")["verification"] == "empty"
    assert _asset(usable_result, "vad", "selected_local_file") == {
        "kind": "vad",
        "id": "selected_local_file",
        "status": "cached",
        "verification": "nonempty",
    }
    assert str(empty) not in repr(empty_result)
    assert str(usable) not in repr(usable_result)


def test_webrtc_and_disabled_vad_need_no_vad_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CRISPASR_CACHE_DIR", str(tmp_path))
    _write(tmp_path / "ggml-large-v3.bin")

    webrtc = diagnose_transcription_readiness(
        _settings(crispasr_vad_enabled=True, crispasr_vad_model="webrtc")
    )
    disabled = diagnose_transcription_readiness(
        _settings(crispasr_vad_enabled=False, crispasr_vad_model="silero")
    )

    assert _asset(webrtc, "vad", "webrtc")["status"] == "not_required"
    assert _asset(disabled, "vad", "disabled")["status"] == "not_required"
    assert webrtc["ready"] is True
    assert disabled["ready"] is True


def test_wrong_quantization_does_not_adopt_another_installed_variant(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CRISPASR_CACHE_DIR", str(tmp_path))
    _write(tmp_path / "parakeet-tdt-0.6b-v3.gguf")

    result = diagnose_transcription_readiness(
        _settings(
            stt_engine="parakeet",
            stt_model_quantization="q5_0",
        )
    )

    selected = _asset(result, "asr", "parakeet_q5_0")
    assert selected["status"] == "missing"
    assert result["ready"] is False


def test_automatic_engine_is_unresolved_and_cloud_engine_needs_no_local_asset():
    automatic = diagnose_transcription_readiness({"stt_engine": "automatic"})
    cloud = diagnose_transcription_readiness(
        {"stt_engine": "azure_mai_transcribe_1_5"}
    )

    assert automatic["engine"] == "auto"
    assert automatic["status"] == "unresolved"
    assert automatic["ready"] is None
    assert automatic["assets"] == []
    assert cloud["engine"] == "azure_mai_transcribe_1_5"
    assert cloud["status"] == "not_required"
    assert cloud["ready"] is True
    assert cloud["assets"] == []


def test_moss_uses_its_vad_default_and_reports_conditional_aligner(tmp_path, monkeypatch):
    monkeypatch.setenv("CRISPASR_CACHE_DIR", str(tmp_path))
    _write(tmp_path / "moss-transcribe-diarize-0.9b-q8_0.gguf")
    _write(tmp_path / "firered-vad.gguf")
    settings = _settings(
        stt_engine="moss",
        stt_language="auto",
        stt_lid_backend="off",
        moss_ctc_alignment_enabled=True,
        moss_ctc_aligner_model="auto",
    )

    result = diagnose_transcription_readiness(settings)

    assert _asset(result, "vad", "disabled")["status"] == "not_required"
    assert _asset(result, "aligner", "canary_ctc_aligner")["status"] == "missing"
    assert _asset(result, "aligner", "language_dependent") == {
        "kind": "aligner",
        "id": "language_dependent",
        "status": "unresolved",
        "verification": "language_required",
    }
    assert result["status"] == "missing"
    assert result["ready"] is False

    moss_vad_enabled = diagnose_transcription_readiness(
        _settings(
            stt_engine="moss",
            stt_model_quantization="q8_0",
            moss_vad_enabled=True,
            crispasr_vad_enabled=False,
            crispasr_vad_model="firered",
            moss_ctc_alignment_enabled=False,
        )
    )
    assert _asset(moss_vad_enabled, "vad", "firered")["status"] == "cached"


def test_moss_can_disable_optional_alignment_and_uses_qwen_for_known_cjk(
    tmp_path, monkeypatch
):
    cache = tmp_path / "crisp"
    qwen_cache = tmp_path / "qwen-aligner"
    monkeypatch.setenv("CRISPASR_CACHE_DIR", str(cache))
    _write(cache / "moss-transcribe-diarize-0.9b-q8_0.gguf")
    _write(qwen_cache / qwen_alignment.MODEL_FILENAME)
    monkeypatch.setattr(qwen_alignment, "MODEL_SIZE", len(b"model"))

    disabled = diagnose_transcription_readiness(
        _settings(
            stt_engine="moss",
            stt_model_quantization="q8_0",
            moss_ctc_alignment_enabled=False,
            moss_vad_enabled=False,
        )
    )
    cjk = diagnose_transcription_readiness(
        _settings(
            stt_engine="moss",
            stt_model_quantization="q8_0",
            stt_language="ja",
            qwen_aligner_cache_dir=str(qwen_cache),
            moss_vad_enabled=False,
            moss_ctc_aligner_model="auto",
        )
    )

    assert _asset(disabled, "aligner", "disabled")["status"] == "not_required"
    assert disabled["ready"] is True
    assert _asset(cjk, "aligner", "qwen3_forced_aligner") == {
        "kind": "aligner",
        "id": "qwen3_forced_aligner",
        "status": "cached",
        "verification": "pinned_size_pending_hash",
    }
    assert cjk["ready"] is True


@pytest.mark.parametrize(
    ("model_id", "asset_key", "size", "expected_filename"),
    [
        ("qwen3_asr_0_6b", "qwen3_asr_0_6b", 5, "qwen3-asr-0.6b-q8_0.gguf"),
        ("qwen3_asr_1_7b", "qwen3_asr_1_7b", 7, "qwen3-asr-1.7b-q8_0.gguf"),
    ],
)
def test_qwen_checks_only_the_selected_model_and_resolved_chunk_mode(
    tmp_path, monkeypatch, model_id, asset_key, size, expected_filename
):
    cache = tmp_path / "crisp"
    selected_asset = crispasr_qwen_assets.ASSETS[asset_key]
    aligner_asset = crispasr_qwen_assets.ASSETS["qwen3_forced_aligner"]
    monkeypatch.setitem(
        crispasr_qwen_assets.ASSETS,
        asset_key,
        replace(selected_asset, size=size),
    )
    monkeypatch.setitem(
        crispasr_qwen_assets.ASSETS,
        "qwen3_forced_aligner",
        replace(aligner_asset, size=5),
    )
    _write(cache / expected_filename, b"x" * size)
    _write(
        cache / aligner_asset.filename,
        b"x" * 5,
    )

    result = diagnose_transcription_readiness(
        _settings(
            stt_engine="qwen3",
            stt_language="ja",
            qwen_asr_model=model_id,
            qwen_asr_chunk_mode="auto",
            crispasr_vad_enabled=False,
            crispasr_cache_dir=str(cache),
        )
    )

    assert _asset(result, "asr", model_id)["verification"] == "pinned_size_pending_hash"
    assert _asset(result, "vad", "disabled")["status"] == "not_required"
    assert _asset(result, "aligner", "qwen3_forced_aligner")["status"] == "cached"
    assert result["ready"] is True

    forced_vad = diagnose_transcription_readiness(
        _settings(
            stt_engine="qwen3",
            stt_language="ja",
            qwen_asr_model=model_id,
            qwen_asr_chunk_mode="vad",
            crispasr_vad_enabled=False,
            crispasr_cache_dir=str(cache),
        )
    )
    assert _asset(forced_vad, "vad", "silero")["status"] == "missing"
    assert forced_vad["ready"] is False


def test_auto_language_includes_tiny_lid_only_when_whisper_lid_is_selected(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CRISPASR_CACHE_DIR", str(tmp_path))
    _write(tmp_path / "ggml-large-v3.bin")
    _write(tmp_path / "ggml-tiny.bin")

    auto = diagnose_transcription_readiness(
        _settings(stt_language="auto", stt_lid_backend="whisper")
    )
    explicit = diagnose_transcription_readiness(_settings(stt_language="en"))
    disabled = diagnose_transcription_readiness(
        _settings(stt_language="auto", stt_lid_backend="off")
    )

    assert _asset(auto, "lid", "whisper_tiny_language_detector")["status"] == "cached"
    assert not any(asset["kind"] == "lid" for asset in explicit["assets"])
    assert not any(asset["kind"] == "lid" for asset in disabled["assets"])


def test_assessment_never_downloads_hashes_or_creates_cache_files(tmp_path, monkeypatch):
    cache = tmp_path / "absent-cache"
    monkeypatch.setenv("CRISPASR_CACHE_DIR", str(cache))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("readiness must not acquire or execute model assets")

    with (
        patch.object(crispasr, "_prefetch_windows_artifact", side_effect=forbidden),
        patch.object(crispasr_qwen_assets, "ensure_asset", side_effect=forbidden),
        patch.object(qwen_alignment, "ensure_model", side_effect=forbidden),
    ):
        result = diagnose_transcription_readiness(
            _settings(stt_engine="whisper", crispasr_vad_enabled=True)
        )

    assert not cache.exists()
    assert result["status"] == "missing"


def test_custom_vad_override_must_match_the_saved_selection_exactly(tmp_path):
    selected = str(tmp_path / "selected-vad.gguf")
    stored = {"crispasr_vad_model": selected}

    assert _validate_overrides(
        {"crispasr_vad_model": selected}, stored
    ) == {"crispasr_vad_model": selected}
    with pytest.raises(ReadinessRequestError):
        _validate_overrides(
            {"crispasr_vad_model": str(tmp_path / "other-vad.gguf")}, stored
        )


def test_custom_moss_aligner_override_must_match_saved_selection_exactly(tmp_path):
    selected = str(tmp_path / "selected-aligner.gguf")
    stored = {"moss_ctc_aligner_model": selected}

    assert _validate_overrides(
        {"moss_ctc_aligner_model": selected}, stored
    ) == {"moss_ctc_aligner_model": selected}
    with pytest.raises(ReadinessRequestError):
        _validate_overrides(
            {"moss_ctc_aligner_model": str(tmp_path / "other-aligner.gguf")},
            stored,
        )


@pytest.fixture
def readiness_web(tmp_path):
    prepare_web_test_data_root(str(tmp_path))
    app = create_app(
        data_root=str(tmp_path),
        testing=True,
        background_maintenance=False,
    )
    extension = app.extensions["pandrator"]
    owner_record, _owner_token = extension["auth"].create_api_token(
        "readiness owner fixture",
        scopes={"app.read", "app.write", "app.run"},
    )
    owner_id = owner_record.user_id
    owner_revision = owner_record.user_auth_revision
    with extension["database"].immediate_session() as db_session:
        personal_workspace = db_session.scalar(
            select(Workspace).where(
                Workspace.kind == "personal",
                Workspace.personal_owner_user_id == owner_id,
            )
        )
        assert personal_workspace is not None
        team_workspace = Workspace(
            id=new_id(),
            kind="team",
            name="Readiness cross-workspace fixture",
            personal_owner_user_id=None,
            state="active",
            revision=1,
        )
        db_session.add(team_workspace)
        db_session.flush()
        db_session.add(
            WorkspaceMembership(
                workspace_id=team_workspace.id,
                user_id=owner_id,
                role="owner",
            )
        )
        personal_workspace_id = personal_workspace.id
        team_workspace_id = team_workspace.id

    def workspace_client(label, workspace_id):
        _record, token = extension["auth"].create_api_token(
            f"readiness-{label}",
            user_id=owner_id,
            auth_revision=owner_revision,
            scopes={"app.read", "app.write", "app.run"},
            resource_grants=CredentialResources(
                workspace_ids=frozenset({workspace_id})
            ),
        )
        client = app.test_client()
        return client, {"Authorization": f"Bearer {token}"}

    alice, alice_headers = workspace_client("alice", personal_workspace_id)
    bob, bob_headers = workspace_client("bob", team_workspace_id)

    def create_session(client, headers, name, workspace_id):
        response = client.post(
            "/api/v1/sessions",
            json={
                "name": name,
                "workflow_kind": "subtitles",
                "workspace_id": workspace_id,
            },
            headers={
                **headers,
                "Idempotency-Key": f"readiness-session-{name.lower().replace(' ', '-')}",
            },
        )
        assert response.status_code == 201, response.get_json()
        return response.get_json()["id"]

    alice_session = create_session(
        alice, alice_headers, "Readiness Alice", personal_workspace_id
    )
    bob_session = create_session(bob, bob_headers, "Readiness Bob", team_workspace_id)
    yield SimpleNamespace(
        app=app,
        extension=extension,
        alice=alice,
        alice_headers=alice_headers,
        alice_user_id=owner_id,
        alice_workspace_id=personal_workspace_id,
        bob_session=bob_session,
        alice_session=alice_session,
        temp_path=tmp_path,
    )
    extension["quick_transcriptions"].stop_maintenance()
    extension["database"].dispose()


def test_route_requires_read_scope_and_session_read_before_file_probe(readiness_web):
    _record, limited_token = readiness_web.extension["auth"].create_api_token(
        "readiness-without-read",
        user_id=readiness_web.alice_user_id,
        auth_revision=1,
        scopes={"app.run"},
        resource_grants=CredentialResources(
            workspace_ids=frozenset({readiness_web.alice_workspace_id})
        ),
    )
    limited_client = readiness_web.app.test_client()
    limited_headers = {"Authorization": f"Bearer {limited_token}"}
    # An unauthenticated request is denied by the app.read guard before assessment.
    with patch(
        "pandrator.logic.dubbing.transcription_readiness.diagnose_transcription_readiness"
    ) as diagnose:
        unauthenticated = readiness_web.app.test_client().post(
            f"/api/v1/sessions/{readiness_web.alice_session}/transcription-readiness",
            json={"settings": {"stt_engine": "whisper"}},
        )
        no_read_scope = limited_client.post(
            f"/api/v1/sessions/{readiness_web.alice_session}/transcription-readiness",
            json={"settings": {"stt_engine": "whisper"}},
            headers=limited_headers,
        )
        cross_workspace = readiness_web.alice.post(
            f"/api/v1/sessions/{readiness_web.bob_session}/transcription-readiness",
            json={"settings": {"stt_engine": "whisper"}},
            headers=readiness_web.alice_headers,
        )
    assert unauthenticated.status_code == 401
    assert no_read_scope.status_code == 403
    assert cross_workspace.status_code in {403, 404}
    diagnose.assert_not_called()


def test_route_rejects_arbitrary_paths_and_privileged_overrides_before_assessment(
    readiness_web,
):
    invalid_settings = (
        {"crispasr_vad_model": "/private/custom-vad.gguf"},
        {"crispasr_cache_dir": str(readiness_web.temp_path)},
        {"crispasr_executable": "/private/crispasr"},
        {"credentials": "never accepted"},
    )
    with patch(
        "pandrator.logic.dubbing.transcription_readiness.diagnose_transcription_readiness"
    ) as diagnose:
        responses = [
            readiness_web.alice.post(
                f"/api/v1/sessions/{readiness_web.alice_session}/transcription-readiness",
                json={"settings": value},
                headers=readiness_web.alice_headers,
            )
            for value in invalid_settings
        ]

    assert all(response.status_code == 422 for response in responses)
    assert all(str(readiness_web.temp_path) not in response.get_data(as_text=True) for response in responses)
    diagnose.assert_not_called()


def test_route_returns_path_free_read_only_summary_and_openapi_scope(
    readiness_web, monkeypatch
):
    cache = readiness_web.temp_path / "local-models"
    _write(cache / "ggml-large-v3.bin")
    monkeypatch.setenv("CRISPASR_CACHE_DIR", str(cache))
    before_response = readiness_web.alice.get(
        f"/api/v1/sessions/{readiness_web.alice_session}/settings/stt",
        headers=readiness_web.alice_headers,
    )
    before = before_response.get_json()

    response = readiness_web.alice.post(
        f"/api/v1/sessions/{readiness_web.alice_session}/transcription-readiness",
        json={
            "settings": {
                "stt_engine": "whisper",
                "stt_model_quantization": "f16",
                "stt_language": "en",
                "stt_lid_backend": "off",
                "crispasr_vad_enabled": False,
            }
        },
        headers=readiness_web.alice_headers,
    )
    after_response = readiness_web.alice.get(
        f"/api/v1/sessions/{readiness_web.alice_session}/settings/stt",
        headers=readiness_web.alice_headers,
    )
    after = after_response.get_json()

    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["scope"] == "local_model_files"
    assert payload["engine"] == "whisper"
    assert payload["ready"] is True
    assert payload["runtime_check_required"] is True
    assert _asset(payload, "asr", "whisper_f16")["status"] == "cached"
    assert str(cache) not in response.get_data(as_text=True)
    assert before_response.headers["ETag"] == after_response.headers["ETag"]
    assert before["effective"] == after["effective"]

    operation = build_openapi_document()["paths"][
        "/api/v1/sessions/{sessionId}/transcription-readiness"
    ]["post"]
    assert operation["x-required-scopes"] == ["app.read"]
    assert operation["requestBody"]["content"]["application/json"]["schema"][
        "properties"
    ]["settings"]["additionalProperties"] is False


def test_assessment_request_size_is_bounded_before_json_diagnostics(readiness_web):
    body = b'{"settings":{"stt_language":"' + b"a" * (32 * 1024) + b'"}}'
    with patch(
        "pandrator.logic.dubbing.transcription_readiness.diagnose_transcription_readiness"
    ) as diagnose:
        response = readiness_web.alice.post(
            f"/api/v1/sessions/{readiness_web.alice_session}/transcription-readiness",
            data=body,
            content_type="application/json",
            headers=readiness_web.alice_headers,
        )

    assert response.status_code == 413
    diagnose.assert_not_called()


def test_deeply_nested_request_is_rejected_as_invalid_json(readiness_web):
    nesting = 2_000
    body = b'{"settings":' + b"[" * nesting + b"0" + b"]" * nesting + b"}"
    with patch(
        "pandrator.logic.dubbing.transcription_readiness.diagnose_transcription_readiness"
    ) as diagnose:
        response = readiness_web.alice.post(
            f"/api/v1/sessions/{readiness_web.alice_session}/transcription-readiness",
            data=body,
            content_type="application/json",
            headers=readiness_web.alice_headers,
        )

    assert response.status_code == 422
    diagnose.assert_not_called()


def test_workflow_plan_reports_files_for_transcribe_without_gating_creation(
    readiness_web, monkeypatch
):
    source = readiness_web.temp_path / "plan-source.wav"
    source.write_bytes(b"synthetic source audio")
    artifact = ArtifactService(
        readiness_web.extension["database"], readiness_web.extension["paths"]
    ).register(
        source,
        kind="wav",
        role="upload",
        session_id=readiness_web.alice_session,
        metadata={"original_filename": source.name},
    )
    asset = readiness_web.extension["source_library"].ensure_for_artifact(
        artifact.id,
        display_name=source.name,
        kind="wav",
    )
    readiness_web.extension["source_library"].attach(
        readiness_web.alice_session,
        asset.id,
    )
    monkeypatch.setenv(
        "CRISPASR_CACHE_DIR", str(readiness_web.temp_path / "missing-model-cache")
    )

    response = readiness_web.alice.post(
        f"/api/v1/sessions/{readiness_web.alice_session}/workflow-plans",
        json={
            "target_stage": "transcribe",
            "continuation": False,
            "overrides": {
                "stt_engine": "whisper",
                "stt_model_quantization": "f16",
                "stt_language": "en",
                "stt_lid_backend": "off",
                "crispasr_vad_enabled": False,
            },
        },
        headers=readiness_web.alice_headers,
    )

    assert response.status_code == 201, response.get_json()
    plan = response.get_json()
    assert any(
        item["stage"] == "transcribe" and item["decision"] == "run"
        for item in plan["ordered_steps"]
    )
    assert plan["transcription_readiness"]["status"] == "missing"
    assert plan["transcription_readiness"]["ready"] is False
