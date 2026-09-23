"""No-network tests for shared audio.cpp on-demand model assets."""

import hashlib
import io
import json
import shutil
import threading
import wave
from pathlib import Path
from unittest.mock import Mock

import pytest

from pandrator.logic.audio_cpp_assets import (
    MODELS,
    AudioAssetsError,
    availability,
    cache_path,
    ensure_model,
    model_info,
    resolve_executable,
)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    monkeypatch.setenv("PANDRATOR_WORKSPACE", str(root))
    for key in ("AUDIO_CPP_CLI",):
        monkeypatch.delenv(key, raising=False)
    return root


@pytest.fixture
def settings(tmp_path):
    return {"audio_cpp_cache_dir": str(tmp_path / "cache")}


class FakeResponse:
    def __init__(self, payload: bytes):
        self._stream = io.BytesIO(payload)

    def read(self, size=-1):
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def probe_spec(filename="probe.gguf", size=64, digest=None, **extra):
    payload = extra.pop("payload", b"p" * size)
    entry = {
        "id": "probe",
        "label": "Probe",
        "family": "probe",
        "cli_task": "sep",
        "cli_family": "probe",
        "directory": "Probe",
        "filename": filename,
        "kind": "gguf",
        "repo": "audio-cpp/audio.cpp-gguf",
        "revision": "0" * 40,
        "url": "https://huggingface.co/audio-cpp/audio.cpp-gguf/resolve/"
        + "0" * 40
        + "/Probe/"
        + filename,
        "size_bytes": len(payload),
        "sha256": digest or hashlib.sha256(payload).hexdigest(),
        "utility": None,
        "notes": "",
    }
    entry.update(extra)
    return entry, payload


def test_allowlist_pins_all_five_models_with_immutable_sources():
    assert sorted(MODELS) == [
        "bs_roformer",
        "deepfilternet2",
        "mel_band_roformer",
        "qwen3_asr_0_6b",
        "qwen3_asr_1_7b",
    ]
    for _model_id, spec in MODELS.items():
        assert spec["size_bytes"] > 0
        assert spec["sha256"] is not None and len(spec["sha256"]) == 64
        assert spec["url"].startswith("https://")
        assert spec["revision"] in spec["url"]  # immutable pin, HF or git commit
    assert MODELS["qwen3_asr_0_6b"]["sha256"].startswith("6c44ec2f")
    assert MODELS["qwen3_asr_1_7b"]["sha256"].startswith("da4fc2ac")
    assert MODELS["bs_roformer"]["sha256"].startswith("9a55a8ca")
    assert MODELS["mel_band_roformer"]["sha256"].startswith("2dd898ce")
    dfn2 = MODELS["deepfilternet2"]
    assert dfn2["sha256"] == (
        "544740171cd81e55dcc7c5d1d889ec79df5808a75eb840a669a681f0d34b0626"
    )
    assert dfn2["size_bytes"] == 9368328
    assert "f2b4937306daa25f5c78520f3c626ed31495a37a" in dfn2["url"]


def test_dfn2_pin_matches_independent_verification():
    verified = Path("/tmp/pandrator-dfn2-verified.json")
    if not verified.is_file():
        pytest.skip("no independent DFN2 verification present")
    record = json.loads(verified.read_text(encoding="utf-8"))
    spec = model_info("deepfilternet2")
    assert spec["url"] == record["url"]
    assert spec["size_bytes"] == record["size"]
    assert spec["sha256"] == record["sha256"]
    model_file = Path(record["model_file"])
    if not model_file.is_file():  # read-only reuse; never created here
        pytest.skip("verified DFN2 copy not present")
    digest = hashlib.sha256()
    with model_file.open("rb") as source:
        while chunk := source.read(4 * 1024 * 1024):
            digest.update(chunk)
    assert model_file.stat().st_size == spec["size_bytes"]
    assert digest.hexdigest() == spec["sha256"]


def test_unknown_model_is_rejected_without_network(settings):
    opener = Mock(side_effect=AssertionError("must not download"))
    with pytest.raises(AudioAssetsError, match="Unknown audio model"):
        ensure_model("not_a_model", settings, opener=opener)
    with pytest.raises(AudioAssetsError, match="Unknown audio model"):
        model_info("not_a_model")
    with pytest.raises(AudioAssetsError, match="Unknown audio model"):
        cache_path("not_a_model", settings)
    opener.assert_not_called()


def test_availability_is_readonly_and_reports_minimum_version(
    settings, workspace, monkeypatch
):
    opener = Mock(side_effect=AssertionError("status must not download"))
    monkeypatch.setattr("pandrator.logic.audio_cpp_assets.urlopen", opener)
    result = availability(settings)
    assert result["runtime"]["minimum_version"] == "0.8.1"
    assert result["runtime"]["observed_version"] is None
    assert result["runtime"]["version_verified"] is False
    assert set(result["models"]) == set(MODELS)
    for model_id, row in result["models"].items():
        assert row["download_bytes"] == MODELS[model_id]["size_bytes"]
        assert row["cached"] is False
        assert row["source_url"] == MODELS[model_id]["url"]
    assert not (Path(settings["audio_cpp_cache_dir"])).exists()
    assert list(workspace.rglob("*")) == []


def test_settings_cannot_inject_arbitrary_urls(settings, monkeypatch):
    entry, payload = probe_spec()
    monkeypatch.setitem(MODELS, "probe", entry)
    seen = []

    def opener(request, timeout=30):
        seen.append(request.full_url)
        return FakeResponse(payload)

    evil = dict(
        settings,
        url="https://evil.example/model.gguf",
        model_url="https://evil.example/model.gguf",
        probe_url="https://evil.example/model.gguf",
        audio_cpp_model_url="https://evil.example/model.gguf",
    )
    path = ensure_model("probe", evil, opener=opener)
    assert path.read_bytes() == payload
    assert seen == [entry["url"]]


def test_ensure_downloads_verifies_and_reuses_cache(settings, monkeypatch):
    entry, payload = probe_spec()
    monkeypatch.setitem(MODELS, "probe", entry)
    opener = Mock(side_effect=lambda *a, **k: FakeResponse(payload))
    first = ensure_model("probe", settings, opener=opener)
    assert first.read_bytes() == payload
    assert ensure_model("probe", settings, opener=opener) == first
    assert opener.call_count == 1


def test_partial_or_corrupt_download_leaves_no_file(settings, monkeypatch):
    entry, payload = probe_spec(size=32)
    monkeypatch.setitem(MODELS, "probe", entry)
    with pytest.raises(AudioAssetsError, match="[Ss]ize|SHA"):
        ensure_model("probe", settings, opener=lambda *a, **k: FakeResponse(b"short"))
    assert not cache_path("probe", settings).exists()
    assert not list(Path(settings["audio_cpp_cache_dir"]).rglob(".probe-download-*"))
    bad = dict(entry, sha256="0" * 64)
    monkeypatch.setitem(MODELS, "probe", bad)
    with pytest.raises(AudioAssetsError, match="SHA-256"):
        ensure_model("probe", settings, opener=lambda *a, **k: FakeResponse(payload))
    assert not cache_path("probe", settings).exists()


def test_oversize_download_is_rejected(settings, monkeypatch):
    entry, payload = probe_spec(size=16)
    monkeypatch.setitem(MODELS, "probe", entry)
    with pytest.raises(AudioAssetsError, match="pinned size"):
        ensure_model(
            "probe", settings, opener=lambda *a, **k: FakeResponse(payload + b"extra")
        )
    assert not cache_path("probe", settings).exists()


def test_cancellation_prevents_download(settings):
    event = threading.Event()
    event.set()
    opener = Mock(side_effect=AssertionError("must not download"))
    with pytest.raises(Exception, match="[Cc]ancel"):
        ensure_model("qwen3_asr_0_6b", settings, cancel_event=event, opener=opener)
    opener.assert_not_called()


def test_insufficient_disk_space_is_rejected(settings, monkeypatch):
    entry, _payload = probe_spec(size=16)
    monkeypatch.setitem(MODELS, "probe", entry)

    class Usage:
        free = 1

    monkeypatch.setattr(shutil, "disk_usage", lambda path: Usage())
    with pytest.raises(AudioAssetsError, match="free space"):
        ensure_model("probe", settings, opener=lambda *a, **k: FakeResponse(b"x" * 16))


@pytest.mark.parametrize("slot", ["slot0", "versions/slot0"])
def test_manager_asset_is_detected_and_reused_without_download(
    settings, workspace, monkeypatch, slot
):
    entry, payload = probe_spec()
    monkeypatch.setitem(MODELS, "probe", entry)
    managed = (
        workspace
        / "Pandrator/services/audio_cpp" / slot / "models"
        / entry["directory"]
        / entry["filename"]
    )
    managed.parent.mkdir(parents=True)
    managed.write_bytes(payload)
    result = availability(settings)
    assert result["models"]["probe"]["manager_asset_detected"] is True
    opener = Mock(side_effect=AssertionError("must reuse, not download"))
    path = ensure_model("probe", settings, opener=opener)
    assert path.read_bytes() == payload
    opener.assert_not_called()


def test_manager_asset_with_wrong_digest_is_not_reused(
    settings, workspace, monkeypatch
):
    entry, _payload = probe_spec()
    monkeypatch.setitem(MODELS, "probe", entry)
    managed = (
        workspace
        / "Pandrator/services/audio_cpp/slot0/models"
        / entry["directory"]
        / entry["filename"]
    )
    managed.parent.mkdir(parents=True)
    managed.write_bytes(b"x" * entry["size_bytes"])
    opener = Mock(side_effect=lambda *a, **k: FakeResponse(b"p" * 64))
    path = ensure_model("probe", settings, opener=opener)
    assert path.read_bytes() == b"p" * 64
    assert opener.call_count == 1


def test_missing_runtime_reports_reason_without_downloading(settings, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(AudioAssetsError, match="audiocpp_cli"):
        resolve_executable(settings)
    result = availability(settings)
    assert result["runtime"]["available"] is False
    assert "audiocpp_cli" in result["runtime"]["reason"]
    assert result["runtime"]["observed_version"] is None


def test_resolve_executable_prefers_workspace_pointer(workspace, tmp_path):
    slot = tmp_path / "slot"
    slot.mkdir()
    binary = slot / "audiocpp_cli"
    binary.write_bytes(b"x")
    binary.chmod(0o755)
    pointer = workspace / "Pandrator/services/audio_cpp"
    pointer.mkdir(parents=True)
    (pointer / "current.json").write_text(json.dumps({"path": str(slot)}))
    assert resolve_executable({}) == binary.resolve()


def test_wav_helper_synthetic_fixture(tmp_path):
    path = tmp_path / "fixture.wav"
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"\0\0" * 1600)
    with wave.open(str(path), "rb") as wav:
        assert wav.getnframes() == 1600
