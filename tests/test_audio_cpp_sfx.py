import hashlib
import json
import math
import subprocess
import wave
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from pandrator.logic import audio_cpp_assets as assets
from pandrator.logic import audio_cpp_sfx as sfx
from pandrator.logic.audio_cpp_processing import AudioProcessingError
from pandrator.logic.cancellable_process import ProcessCancelled


def _version_result(text: str, *, returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=text, stderr="")


def _runtime_file(tmp_path: Path, monkeypatch, *, content: bytes = b"fake cli") -> Path:
    executable = tmp_path / "audiocpp_cli"
    executable.write_bytes(content)
    monkeypatch.setattr(assets, "resolve_executable", lambda settings: executable)
    return executable


def _write_wav(
    path: Path,
    *,
    frames: int,
    sample_rate: int = 44_100,
    channels: int = 2,
    sample_width: int = 2,
    nonzero: bool = True,
) -> None:
    sample = (b"\x01\x00" if nonzero else b"\x00\x00") * (channels * sample_width // 2)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(sample_rate)
        remaining = frames
        while remaining:
            count = min(8192, remaining)
            wav_file.writeframesraw(sample * count)
            remaining -= count


def _install_generator_fakes(monkeypatch, tmp_path: Path, runner):
    executable = tmp_path / "audiocpp_cli"
    executable.write_bytes(b"fake runtime")
    model_path = tmp_path / "stable-audio.gguf"
    model_path.write_bytes(b"pinned fake model")
    ensure_calls = []

    monkeypatch.setattr(assets, "resolve_executable", lambda settings: executable)
    monkeypatch.setattr(sfx, "runtime_identity", lambda settings: {"runtime": "pinned"})
    monkeypatch.setattr(sfx, "require_backend", lambda settings, backend: None)

    def ensure_model(model_id, settings, *, cancel_event, progress):
        ensure_calls.append((model_id, dict(settings), cancel_event, progress))
        return model_path

    monkeypatch.setattr(assets, "ensure_model", ensure_model)
    return executable, model_path, ensure_calls


def test_model_asset_is_pinned_and_baseline_runtime_version_is_unchanged():
    spec = assets.MODELS[sfx.MODEL_ID]
    assert assets.AUDIO_CPP_VERSION == "0.9.0"
    assert spec["id"] == "stable_audio_3_small_sfx_q8_0"
    assert spec["family"] == spec["cli_family"] == "stable_audio"
    assert spec["cli_task"] == "gen"
    assert spec["revision"] == "e36610ac69b5262e914a52635324050bee8f1ad2"
    assert spec["directory"] == "Stable-Audio-3-Small-SFX-GGUF"
    assert spec["filename"] == "stable-audio-3-small-sfx-q8_0.gguf"
    assert spec["size_bytes"] == 1683570688
    assert spec["sha256"] == ("5bb1ec653134e63dac46264e336ae198a2590195fc996fcd0605021215e9b26f")
    assert spec["minimum_runtime_version"] == "0.9.1"


def test_validate_request_canonical_defaults_and_resolved_seed(monkeypatch):
    monkeypatch.setattr(sfx.os, "cpu_count", lambda: 12)
    request = sfx.validate_request({"prompt": "  Rain on a roof.  "}, resolved_seed=42)

    assert request == {
        "model_id": sfx.MODEL_ID,
        "prompt": "Rain on a roof.",
        "duration_seconds": 4.0,
        "seed": 42,
        "threads": 4,
        "backend": "cpu",
        "num_inference_steps": 8,
        "guidance_scale": 1.0,
    }

    explicit = sfx.validate_request(
        {
            "model_id": sfx.MODEL_ID,
            "prompt": " wind ",
            "duration_seconds": 1.25,
            "seed": 0,
            "threads": 8,
            "backend": "CPU",
            "num_inference_steps": 8,
            "guidance_scale": 1,
        }
    )
    assert explicit["duration_seconds"] == 1.25
    assert explicit["seed"] == 0
    assert explicit["threads"] == 8
    assert explicit["backend"] == "cpu"


def test_validate_request_requires_seed_and_rejects_unknown_or_fixed_overrides():
    with pytest.raises(ValueError, match="seed is required"):
        sfx.validate_request({"prompt": "Wind"})
    with pytest.raises(ValueError, match="Unknown Stable Audio SFX request field"):
        sfx.validate_request({"prompt": "Wind", "seed": 1, "device": "cuda"})
    with pytest.raises(ValueError, match="model_id must be"):
        sfx.validate_request({"prompt": "Wind", "seed": 1, "model_id": "other"})
    with pytest.raises(ValueError, match="fixed at 8"):
        sfx.validate_request({"prompt": "Wind", "seed": 1, "num_inference_steps": 9})
    with pytest.raises(ValueError, match="fixed at 1.0"):
        sfx.validate_request({"prompt": "Wind", "seed": 1, "guidance_scale": 0.5})
    with pytest.raises(ValueError, match="seed must be an integer"):
        sfx.validate_request({"prompt": "Wind"}, resolved_seed=True)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("prompt", " \t "),
        ("prompt", "x" * 2001),
        ("duration_seconds", True),
        ("duration_seconds", math.nan),
        ("duration_seconds", math.inf),
        ("duration_seconds", 0.99),
        ("duration_seconds", 30.01),
        ("seed", False),
        ("seed", -1),
        ("seed", 2**32),
        ("threads", True),
        ("threads", 0),
        ("threads", 5),
        ("backend", "cuda"),
    ),
)
def test_validate_request_rejects_invalid_fields(field, value, monkeypatch):
    monkeypatch.setattr(sfx.os, "cpu_count", lambda: 4)
    payload = {"prompt": "Wind", "seed": 7, field: value}
    with pytest.raises(ValueError):
        sfx.validate_request(payload)


def test_sound_catalogue_probes_runtime_read_only_and_hides_paths(tmp_path, monkeypatch):
    executable = _runtime_file(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        assets,
        "cache_path",
        lambda _model_id, _settings: tmp_path / "missing-model.gguf",
    )

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return _version_result("audio.cpp 0.9.1\ngit: cf124a67\n")

    monkeypatch.setattr(sfx.subprocess, "run", run)
    monkeypatch.setattr(
        assets,
        "ensure_model",
        lambda *args, **kwargs: pytest.fail("catalogue must not fetch models"),
    )
    monkeypatch.setattr(
        sfx,
        "_hash_executable",
        lambda *args, **kwargs: pytest.fail("catalogue must not hash binaries"),
    )

    catalogue = sfx.sound_catalogue({"audio_cpp_executable": str(executable)})
    serialized = json.dumps(catalogue)
    assert catalogue["provider"] == "audio_cpp"
    assert catalogue["operation"] == "gen"
    assert catalogue["models"][0]["id"] == sfx.MODEL_ID
    assert catalogue["models"][0]["label"] == assets.model_info(sfx.MODEL_ID)["label"]
    assert catalogue["models"][0]["cached"] is False
    assert catalogue["runtime"] == {
        "available": True,
        "version": "0.9.1",
        "reason": None,
        "backends": ["cpu"],
    }
    assert catalogue["backends"] == ["cpu"]
    assert catalogue["models"][0]["backends"] == ["cpu"]
    assert catalogue["defaults"]["duration_seconds"] == 4.0
    assert str(executable) not in serialized
    assert catalogue["models"][0]["source_url"] == (
        "https://huggingface.co/audio-cpp/audio.cpp-gguf"
    )
    assert str(tmp_path) not in serialized
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == [str(executable), "--version"]
    assert kwargs["timeout"] == 2.0


def test_sound_catalogue_cache_flag_uses_size_without_hashing(tmp_path, monkeypatch):
    executable = _runtime_file(tmp_path, monkeypatch)
    monkeypatch.setattr(
        sfx.subprocess,
        "run",
        lambda *args, **kwargs: _version_result("audio.cpp 0.9.1\n"),
    )
    model_spec = assets.model_info(sfx.MODEL_ID)
    monkeypatch.setitem(
        assets.MODELS,
        sfx.MODEL_ID,
        {**model_spec, "size_bytes": 8},
    )
    cache_file = tmp_path / "model.gguf"
    monkeypatch.setattr(assets, "cache_path", lambda _model_id, _settings: cache_file)
    monkeypatch.setattr(
        assets,
        "ensure_model",
        lambda *args, **kwargs: pytest.fail("catalogue must not fetch models"),
    )
    monkeypatch.setattr(
        sfx,
        "_hash_executable",
        lambda *args, **kwargs: pytest.fail("catalogue must not hash binaries"),
    )

    settings = {"audio_cpp_executable": str(executable)}
    catalogue = sfx.sound_catalogue(settings)
    assert catalogue["models"][0]["cached"] is False
    assert catalogue["models"][0]["label"] == model_spec["label"]

    cache_file.write_bytes(b"not-hash")
    assert sfx.sound_catalogue(settings)["models"][0]["cached"] is True

    cache_file.write_bytes(b"wrong!!")
    assert sfx.sound_catalogue(settings)["models"][0]["cached"] is False


@pytest.mark.parametrize(
    ("version_output", "expected_version"),
    (
        ("audio.cpp 0.9.0\ngit: old\n", "0.9.0"),
        ("nightly build without a version\n", None),
    ),
)
def test_old_or_unknown_runtime_is_explicitly_unavailable(
    tmp_path, monkeypatch, version_output, expected_version
):
    _runtime_file(tmp_path, monkeypatch)
    monkeypatch.setattr(
        sfx.subprocess, "run", lambda *args, **kwargs: _version_result(version_output)
    )

    catalogue = sfx.sound_catalogue({})
    assert catalogue["runtime"]["available"] is False
    assert catalogue["runtime"]["version"] == expected_version
    assert "0.9.1" in catalogue["runtime"]["reason"]
    with pytest.raises(sfx.AudioCppSfxError, match="0.9.1"):
        sfx.runtime_identity({})


def test_runtime_identity_hashes_and_caches_exact_runtime(tmp_path, monkeypatch):
    executable = _runtime_file(tmp_path, monkeypatch, content=b"exact local binary")
    version_calls = []
    hash_calls = []
    original_hash = sfx._hash_executable

    def run(command, **kwargs):
        version_calls.append(command)
        return _version_result("audio.cpp 0.9.1\ngit: cf124a67\n")

    def hash_binary(path):
        hash_calls.append(path)
        return original_hash(path)

    monkeypatch.setattr(sfx.subprocess, "run", run)
    monkeypatch.setattr(sfx, "_hash_executable", hash_binary)

    identity = sfx.runtime_identity({})
    assert identity["runtime_version"] == "0.9.1"
    assert identity["runtime_sha256"] == hashlib.sha256(executable.read_bytes()).hexdigest()
    assert identity["model_sha256"] == sfx.MODEL_SHA256
    assert identity["model_revision"] == sfx.MODEL_REVISION
    assert str(executable) not in json.dumps(identity)
    assert sfx.runtime_identity({}) == identity
    assert len(version_calls) == len(hash_calls) == 1


def test_compiled_backends_are_cached_and_reprobed_when_binary_changes(tmp_path, monkeypatch):
    executable = _runtime_file(tmp_path, monkeypatch, content=b"runtime-one")
    outputs = iter(
        (
            "audio.cpp0.9.1\ngit: cf124a67\nbackends: cpu,vulkan\n",
            "audio.cpp 0.9.1\ngit: changed\nbackends: cuda,cpu\n",
        )
    )
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return _version_result(next(outputs))

    monkeypatch.setattr(sfx.subprocess, "run", run)
    settings = {"audio_cpp_executable": str(executable)}
    assert sfx.runtime_backends(settings) == ("cpu", "vulkan")
    assert sfx.runtime_backends(settings) == ("cpu", "vulkan")
    assert len(calls) == 1

    executable.write_bytes(b"runtime-two-is-larger")
    assert sfx.runtime_backends(settings) == ("cpu", "cuda")
    assert len(calls) == 2
    sfx.require_backend(settings, "cuda")
    with pytest.raises(sfx.AudioCppSfxError, match="vulkan backend is not compiled"):
        sfx.require_backend(settings, "vulkan")


def test_valid_runtime_without_backend_line_falls_back_to_cpu(tmp_path, monkeypatch):
    _runtime_file(tmp_path, monkeypatch)
    monkeypatch.setattr(
        sfx.subprocess, "run", lambda *args, **kwargs: _version_result("audio.cpp 0.9.1\n")
    )
    assert sfx.runtime_backends({}) == ("cpu",)
    sfx.require_backend({}, "cpu")
    with pytest.raises(sfx.AudioCppSfxError, match="vulkan backend is not compiled"):
        sfx.require_backend({}, "vulkan")


def test_runtime_backend_parser_ignores_unrecognized_build_flags(tmp_path, monkeypatch):
    _runtime_file(tmp_path, monkeypatch)
    monkeypatch.setattr(
        sfx.subprocess,
        "run",
        lambda *args, **kwargs: _version_result(
            "audio.cpp 0.9.1\nbackends: rocm,cpu,metal\n"
        ),
    )
    assert sfx.runtime_backends({}) == ("cpu",)
    with pytest.raises(sfx.AudioCppSfxError, match="Unknown audio.cpp backend"):
        sfx.require_backend({}, "metal")


def test_cpu_only_runtime_marks_cuda_only_model_unavailable_in_catalogue(
    tmp_path, monkeypatch
):
    _runtime_file(tmp_path, monkeypatch)
    monkeypatch.setattr(
        sfx.subprocess,
        "run",
        lambda *args, **kwargs: _version_result(
            "audio.cpp 0.9.1\nbackends: cpu\n"
        ),
    )
    monkeypatch.setattr(sfx.generation_assets, "is_cached", lambda *_args: False)
    catalogue = sfx.sound_catalogue({})
    int8 = next(
        model for model in catalogue["models"] if model["id"] == "minimax_h3_q4_k_int8_dit"
    )
    assert catalogue["backends"] == catalogue["runtime"]["backends"] == ["cpu"]
    assert int8["backends"] == []
    assert int8["available"] is False
    assert "No model-supported backend" in int8["reason"]
    assert any(item["id"] == int8["id"] for item in catalogue["unavailable_models"])


def test_backend_admission_rejects_uncompiled_backend_before_runtime_identity_or_download(
    tmp_path, monkeypatch
):
    executable = _runtime_file(tmp_path, monkeypatch)
    monkeypatch.setattr(
        sfx.subprocess,
        "run",
        lambda *args, **kwargs: _version_result("audio.cpp 0.9.1\nbackends: cpu\n"),
    )
    monkeypatch.setattr(
        sfx,
        "runtime_identity",
        lambda *_args, **_kwargs: pytest.fail("backend admission must precede runtime identity"),
    )
    monkeypatch.setattr(
        sfx.generation_assets,
        "ensure_model",
        lambda *_args, **_kwargs: pytest.fail("backend admission must precede package download"),
    )
    request = sfx.validate_request(
        {
            "model_id": "minimax_h3_q4_k",
            "prompt": "A short ambient sound",
            "seed": 3,
            "backend": "vulkan",
        }
    )
    with pytest.raises(sfx.AudioCppSfxError, match="vulkan backend is not compiled"):
        sfx.generate_sound(
            request,
            tmp_path / "stage.wav",
            settings={"audio_cpp_executable": str(executable)},
            cancel_event=Event(),
        )


def test_cuda_runtime_admits_h3_int8_model_before_download(tmp_path, monkeypatch):
    _runtime_file(tmp_path, monkeypatch)
    monkeypatch.setattr(
        sfx.subprocess,
        "run",
        lambda *args, **kwargs: _version_result(
            "audio.cpp 0.9.1\nbackends: cpu,cuda\n"
        ),
    )
    settings = {}
    assert sfx.validate_request(
        {
            "model_id": "minimax_h3_q4_k_int8_dit",
            "prompt": "A short ambient sound",
            "seed": 3,
        }
    )["backend"] == "cuda"
    sfx.require_backend(settings, "cuda")


def test_model_aware_runtime_identity_pins_every_asset_without_private_settings(
    tmp_path, monkeypatch
):
    executable = _runtime_file(tmp_path, monkeypatch, content=b"generation runtime")
    monkeypatch.setattr(
        sfx.subprocess, "run", lambda *args, **kwargs: _version_result("audio.cpp 0.9.1\n")
    )
    settings = {
        "audio_cpp_executable": str(executable),
        "audio_cpp_cache_dir": str(tmp_path / "private-cache"),
    }
    model = sfx.generation_models.get_model("minimax_music3_q4_0")

    identity = sfx.runtime_identity(settings, model_id=model.id)
    serialized = json.dumps(identity, sort_keys=True)
    assert identity["model_id"] == model.id
    assert identity["model_revision"] == model.revision
    assert identity["model_assets"] == [
        {"path": item.path, "size_bytes": item.size_bytes, "sha256": item.sha256}
        for item in model.files
    ]
    manifest = json.dumps(
        identity["model_assets"], sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert identity["model_sha256"] == hashlib.sha256(manifest).hexdigest()
    assert str(executable) not in serialized
    assert str(tmp_path / "private-cache") not in serialized


def test_generation_fingerprint_changes_with_request_or_runtime_identity():
    request = sfx.validate_request({"prompt": "Rain", "seed": 19})
    identity = {"runtime_sha256": "a" * 64, "model_sha256": sfx.MODEL_SHA256}
    fingerprint = sfx.generation_fingerprint(request, identity)

    assert fingerprint == sfx.generation_fingerprint(dict(request), dict(identity))
    assert len(fingerprint) == 64
    changed_request = {**request, "prompt": "Thunder"}
    changed_identity = {**identity, "runtime_sha256": "b" * 64}
    assert sfx.generation_fingerprint(changed_request, identity) != fingerprint
    assert sfx.generation_fingerprint(request, changed_identity) != fingerprint


@pytest.mark.parametrize("instrumental", (False, True))
def test_yue2_generation_only_installs_and_passes_adapter_when_selected(tmp_path, monkeypatch, instrumental):
    calls = []
    base_path = tmp_path / "base-model"
    adapter_path = tmp_path / "managed adapter.safetensors"
    executable = _runtime_file(tmp_path, monkeypatch)
    monkeypatch.setattr(sfx, "require_backend", lambda *_args: None)
    monkeypatch.setattr(sfx, "runtime_identity", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(sfx.generation_assets, "ensure_model", lambda *_args, **_kwargs: base_path)

    def ensure_adapter(settings, *, cancel_event, progress):
        calls.append((settings, cancel_event, progress))
        return adapter_path

    monkeypatch.setattr(sfx.yue2_adapter, "ensure_adapter", ensure_adapter)
    captured = []

    def runner(argv, **_kwargs):
        captured.append(argv)
        _write_wav(Path(argv[argv.index("--out") + 1]), frames=44100)

    sfx.generate_sound(
        {
            "model_id": "yue2_main_q8_0", "prompt": "Solo piano", "seed": 4,
            "duration_seconds": 8, "parameters": {"instrumental": instrumental},
        },
        tmp_path / "result.wav", settings={}, cancel_event=Event(), run_func=runner,
    )
    assert len(calls) == int(instrumental)
    assert captured[0][0] == str(executable)
    if instrumental:
        assert f"yue2.ar_lora={adapter_path}" in captured[0]
        assert "yue2.ar_lora_scale=1.0" in captured[0]
    else:
        assert all("ar_lora" not in arg for arg in captured[0])


def test_yue2_adapter_failure_prevents_native_generation(tmp_path, monkeypatch):
    _runtime_file(tmp_path, monkeypatch)
    monkeypatch.setattr(sfx, "require_backend", lambda *_args: None)
    monkeypatch.setattr(sfx, "runtime_identity", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(sfx.generation_assets, "ensure_model", lambda *_args, **_kwargs: tmp_path)

    def fail_adapter(*_args, **_kwargs):
        raise assets.AudioAssetsError("Adapter SHA-256 mismatch")

    monkeypatch.setattr(sfx.yue2_adapter, "ensure_adapter", fail_adapter)
    with pytest.raises(assets.AudioAssetsError, match="SHA-256"):
        sfx.generate_sound(
            {
                "model_id": "yue2_main_q8_0", "prompt": "Solo piano", "seed": 4,
                "parameters": {"instrumental": True},
            },
            tmp_path / "result.wav", settings={}, cancel_event=Event(),
            run_func=lambda *_args, **_kwargs: pytest.fail("unverified adapter reached native runtime"),
        )


def test_generate_sound_uses_expected_cpu_argv_and_returns_wav_properties(tmp_path, monkeypatch):
    captured = []

    def runner(command, **kwargs):
        captured.append((command, kwargs))
        output = Path(command[command.index("--out") + 1])
        requested_duration = float(command[command.index("--duration-seconds") + 1])
        _write_wav(output, frames=round(requested_duration * sfx.SAMPLE_RATE))

    executable, model_path, ensure_calls = _install_generator_fakes(monkeypatch, tmp_path, runner)
    output = tmp_path / "stage.wav"
    request = sfx.validate_request(
        {
            "prompt": "  A glass marble rolls down a wooden ramp. ",
            "duration_seconds": 1.5,
            "seed": 123,
            "threads": 2,
        }
    )
    progress = []
    result = sfx.generate_sound(
        request,
        output,
        settings={"audio_cpp_executable": str(executable)},
        cancel_event=Event(),
        progress=lambda *event: progress.append(event),
        run_func=runner,
    )

    assert result == {
        "frames": 66_150,
        "sample_rate": 44_100,
        "channels": 2,
        "sample_width": 2,
        "duration_seconds": 1.5,
    }
    assert set(result) == {
        "frames",
        "sample_rate",
        "channels",
        "sample_width",
        "duration_seconds",
    }
    assert len(ensure_calls) == 1
    assert ensure_calls[0][0] == sfx.MODEL_ID
    command, kwargs = captured[0]
    assert command[0] == str(executable)
    for pair in (
        ("--task", "gen"),
        ("--family", "stable_audio"),
        ("--model", str(model_path)),
        ("--backend", "cpu"),
        ("--threads", "2"),
        ("--text", "A glass marble rolls down a wooden ramp."),
        ("--duration-seconds", "1.5"),
        ("--seed", "123"),
        ("--num-inference-steps", "8"),
        ("--guidance-scale", "1"),
        ("--out", str(output.resolve())),
        ("--out-format", "pcm16"),
    ):
        index = command.index(pair[0])
        assert command[index + 1] == pair[1]
    assert "--device" not in command
    assert kwargs == {
        "check": True,
        "capture_output": True,
        "cwd": str(executable.parent),
    }
    assert progress == [("generate", 0, 1), ("generate", 1, 1)]


def test_generate_sound_rejects_existing_output_without_overwriting(tmp_path, monkeypatch):
    runner_calls = []
    _install_generator_fakes(
        monkeypatch, tmp_path, lambda *args, **kwargs: runner_calls.append(args)
    )
    output = tmp_path / "stage.wav"
    output.write_bytes(b"caller-owned bytes")

    with pytest.raises(AudioProcessingError, match="already exists"):
        sfx.generate_sound(
            sfx.validate_request({"prompt": "Wind", "seed": 7}),
            output,
            settings={},
            cancel_event=Event(),
            run_func=lambda *args, **kwargs: runner_calls.append(args),
        )
    assert output.read_bytes() == b"caller-owned bytes"
    assert runner_calls == []


def test_generate_sound_checks_cancellation_before_model_use(tmp_path, monkeypatch):
    runner_calls = []
    _, _, ensure_calls = _install_generator_fakes(
        monkeypatch,
        tmp_path,
        lambda *args, **kwargs: runner_calls.append(args),
    )
    cancellation = Event()
    cancellation.set()

    with pytest.raises(ProcessCancelled):
        sfx.generate_sound(
            sfx.validate_request({"prompt": "Wind", "seed": 7}),
            tmp_path / "stage.wav",
            settings={},
            cancel_event=cancellation,
            run_func=lambda *args, **kwargs: runner_calls.append(args),
        )
    assert ensure_calls == []
    assert runner_calls == []


def test_generate_sound_observes_cancellation_after_child_returns(tmp_path, monkeypatch):
    cancellation = Event()

    def runner(command, **kwargs):
        output = Path(command[command.index("--out") + 1])
        _write_wav(output, frames=44_100)
        cancellation.set()

    executable, _, _ = _install_generator_fakes(monkeypatch, tmp_path, runner)
    output = tmp_path / "stage.wav"
    with pytest.raises(ProcessCancelled):
        sfx.generate_sound(
            sfx.validate_request({"prompt": "Wind", "seed": 7}),
            output,
            settings={"audio_cpp_executable": str(executable)},
            cancel_event=cancellation,
            run_func=runner,
        )
    assert output.is_file()


@pytest.mark.parametrize(
    ("wav_settings", "message"),
    (
        ({"sample_rate": 48_000}, "44.1 kHz stereo PCM16"),
        ({"channels": 1}, "44.1 kHz stereo PCM16"),
        ({"sample_width": 4}, "44.1 kHz stereo PCM16"),
        ({"nonzero": False}, "nonzero audio"),
        ({"frames": 44_102}, "does not match the requested duration"),
    ),
)
def test_generate_sound_rejects_invalid_wav_properties(
    tmp_path, monkeypatch, wav_settings, message
):
    def runner(command, **kwargs):
        output = Path(command[command.index("--out") + 1])
        settings = {"frames": 44_100, **wav_settings}
        _write_wav(output, **settings)

    executable, _, _ = _install_generator_fakes(monkeypatch, tmp_path, runner)
    with pytest.raises(AudioProcessingError, match=message):
        sfx.generate_sound(
            sfx.validate_request({"prompt": "Wind", "seed": 7, "duration_seconds": 1.0}),
            tmp_path / "stage.wav",
            settings={"audio_cpp_executable": str(executable)},
            cancel_event=Event(),
            run_func=runner,
        )


def test_generate_sound_rejects_unreadable_wav(tmp_path, monkeypatch):
    def runner(command, **kwargs):
        Path(command[command.index("--out") + 1]).write_bytes(b"not a wave")

    executable, _, _ = _install_generator_fakes(monkeypatch, tmp_path, runner)
    with pytest.raises(AudioProcessingError, match="invalid WAV"):
        sfx.generate_sound(
            sfx.validate_request({"prompt": "Wind", "seed": 7}),
            tmp_path / "stage.wav",
            settings={"audio_cpp_executable": str(executable)},
            cancel_event=Event(),
            run_func=runner,
        )


@pytest.mark.parametrize(
    ("model_id", "requested_duration", "sample_rate", "channels", "frames"),
    (
        ("stable_audio_3_small_music_q8_0", 1.0, 48_000, 1, 48_000),
        ("heartmula_q8_0", 30.0, 24_000, 2, 24_000),
        ("minimax_music3_q4_0", 20.0, 32_000, 1, 16_000),
    ),
)
def test_generation_wav_accepts_pcm16_rate_and_channel_variants(
    tmp_path, model_id, requested_duration, sample_rate, channels, frames
):
    output = tmp_path / f"{model_id}.wav"
    _write_wav(
        output,
        frames=frames,
        sample_rate=sample_rate,
        channels=channels,
        sample_width=2,
    )
    properties = sfx._validate_output_wav(output, requested_duration, model_id)
    assert properties["sample_rate"] == sample_rate
    assert properties["channels"] == channels
    assert properties["frames"] == frames
    assert properties["sample_width"] == 2


def test_only_stable_audio_requires_exact_requested_frame_duration(tmp_path):
    stable = tmp_path / "stable.wav"
    _write_wav(stable, frames=48_000, sample_rate=48_000, channels=1)
    with pytest.raises(AudioProcessingError, match="does not match"):
        sfx._validate_output_wav(stable, 2.0, "stable_audio_3_small_music_q8_0")

    autoregressive = tmp_path / "music3.wav"
    _write_wav(autoregressive, frames=8_000, sample_rate=24_000, channels=1)
    assert sfx._validate_output_wav(autoregressive, 20.0, "minimax_music3_q4_0")[
        "duration_seconds"
    ] == pytest.approx(1 / 3)


def test_generate_sound_rejects_old_runtime_before_model_fetch(tmp_path, monkeypatch):
    _runtime_file(tmp_path, monkeypatch)
    monkeypatch.setattr(
        sfx.subprocess, "run", lambda *args, **kwargs: _version_result("audio.cpp 0.9.0\n")
    )
    monkeypatch.setattr(
        assets,
        "ensure_model",
        lambda *args, **kwargs: pytest.fail("old runtime must fail before model fetch"),
    )

    with pytest.raises(sfx.AudioCppSfxError, match="too old"):
        sfx.generate_sound(
            sfx.validate_request({"prompt": "Wind", "seed": 7}),
            tmp_path / "stage.wav",
            settings={},
            cancel_event=Event(),
            run_func=subprocess.run,
        )
