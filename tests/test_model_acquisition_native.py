"""Native CrispASR acquisition checks use synthetic files and fake runners."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pandrator.logic.dubbing import crispasr
from pandrator.logic.model_acquisition import (
    ModelAcquisitionDenied,
    host_model_acquisition_scope,
    model_acquisition_scope,
)


def _model_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"synthetic model")
    return path


def _local_command(tmp_path: Path) -> list[str]:
    model = _model_file(tmp_path / "asr.gguf")
    vad = _model_file(tmp_path / "vad.gguf")
    return [
        "crispasr",
        "--backend",
        "whisper",
        "-m",
        str(model),
        "-l",
        "en",
        "--vad",
        "--vad-model",
        str(vad),
        "--chunk-seconds",
        "0",
    ]


def _run(command: list[str], runner: Any) -> Any:
    return crispasr._run_tool(command, run_func=runner, cancel_event=None)


def _fail_if_run(*_args: object, **_kwargs: object) -> object:
    pytest.fail("native runner ran before acquisition authorization")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda command, _tmp: command.extend(("--hf-repo", "owner/repo:model.gguf")),
        lambda command, _tmp: command.extend(("--auto-download",)),
        lambda command, _tmp: command.extend(("-am", "auto")),
        lambda command, _tmp: command.__setitem__(command.index("--vad-model") + 1, "silero"),
        lambda command, _tmp: command.__setitem__(command.index("-l") + 1, "auto"),
        lambda command, _tmp: command.extend(("--detect-language",)),
        lambda command, _tmp: command.extend(("--lid-on-transcript", "auto")),
        lambda command, _tmp: command.extend(("--diarize-method", "pyannote")),
        lambda command, _tmp: command.extend(("--diarize-embedder", "auto")),
        lambda command, _tmp: command.extend(("--sherpa-segment-model", "auto")),
        lambda command, _tmp: command.extend(("--aligner-model", "auto")),
    ],
    ids=[
        "hf-repo",
        "auto-download",
        "aligner-auto",
        "missing-vad-model",
        "auto-language-lid",
        "detect-language",
        "text-lid",
        "model-diarization-method",
        "model-diarization-embedder",
        "model-diarization-segmenter",
        "nonlocal-explicit-aligner",
    ],
)
def test_native_command_acquisition_flags_deny_before_runner(tmp_path: Path, mutate: Any) -> None:
    command = _local_command(tmp_path)
    mutate(command, tmp_path)

    with pytest.raises(ModelAcquisitionDenied):
        _run(command, _fail_if_run)


def test_implicit_vad_denies_when_chunk_choice_and_vad_model_are_absent(
    tmp_path: Path,
) -> None:
    command = _local_command(tmp_path)
    del command[command.index("--chunk-seconds") : command.index("--chunk-seconds") + 2]
    del command[command.index("--vad-model") : command.index("--vad-model") + 2]
    command.remove("--vad")

    with pytest.raises(ModelAcquisitionDenied):
        _run(command, _fail_if_run)


def test_local_asr_language_vad_and_align_only_run_without_authority(
    tmp_path: Path,
) -> None:
    command = _local_command(tmp_path)
    result = _run(command, lambda *_args, **_kwargs: "completed")
    assert result == "completed"

    aligner = _model_file(tmp_path / "aligner.gguf")
    align_only = ["crispasr", "--align-only", "-am", str(aligner)]
    assert _run(align_only, lambda *_args, **_kwargs: "aligned") == "aligned"


def test_last_model_and_language_values_control_guard(tmp_path: Path) -> None:
    model = _model_file(tmp_path / "final-model.gguf")
    command = _local_command(tmp_path)
    command.extend(
        (
            "-m",
            "missing.gguf",
            "--model",
            str(model),
            "-l",
            "auto",
            "--language",
            "en",
            "--backend",
            "other",
            "--backend=whisper",
        )
    )

    assert _run(command, lambda *_args, **_kwargs: "last wins") == "last wins"


def test_literal_tilde_argv_path_is_not_expanded_for_locality(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = _local_command(tmp_path)
    command[command.index("-m") + 1] = "~/synthetic-model.gguf"
    monkeypatch.setattr(
        Path,
        "expanduser",
        lambda *_args, **_kwargs: pytest.fail("native argv path was expanded"),
    )

    with pytest.raises(ModelAcquisitionDenied):
        _run(command, _fail_if_run)


def test_audio_lid_off_or_explicit_local_lid_model_allows_auto_language(
    tmp_path: Path,
) -> None:
    command = _local_command(tmp_path)
    command[command.index("-l") + 1] = "auto"
    command.extend(("--lid-backend", "off"))
    assert _run(command, lambda *_args, **_kwargs: "off") == "off"

    command = _local_command(tmp_path)
    command[command.index("-l") + 1] = "auto"
    lid_model = _model_file(tmp_path / "lid.gguf")
    command.extend(("--lid-model", str(lid_model)))
    assert _run(command, lambda *_args, **_kwargs: "local lid") == "local lid"


def test_audio_lid_exemptions_do_not_suppress_text_lid_fetch(tmp_path: Path) -> None:
    command = _local_command(tmp_path)
    command.extend(("--lid-backend", "off", "--lid-on-transcript", "auto"))
    with pytest.raises(ModelAcquisitionDenied):
        _run(command, _fail_if_run)


def test_default_whisper_tiny_cache_is_reused_and_custom_lid_is_not_rewritten(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    tiny = _model_file(cache_dir / "ggml-tiny.bin")
    settings = {
        "stt_engine": "whisper",
        "stt_language": "auto",
        "crispasr_vad_enabled": False,
        "crispasr_cache_dir": str(cache_dir),
    }
    asr_model = _model_file(tmp_path / "whisper.ggml")
    command = crispasr.build_command(
        tmp_path / "audio.wav",
        tmp_path / "output",
        settings,
        model_path=asr_model,
    )
    assert command[command.index("--lid-model") + 1] == str(tiny)
    assert _run(command, lambda *_args, **_kwargs: "tiny cache") == "tiny cache"

    custom_settings = {**settings, "stt_lid_backend": "ecapa"}
    custom = crispasr.build_command(
        tmp_path / "audio.wav",
        tmp_path / "custom-output",
        custom_settings,
        model_path=asr_model,
    )
    assert custom[custom.index("--lid-backend") + 1] == "ecapa"
    assert "--lid-model" not in custom


def test_missing_tiny_requires_authority_but_lid_off_does_not(tmp_path: Path) -> None:
    model = _model_file(tmp_path / "asr.ggml")
    common = {
        "stt_engine": "whisper",
        "stt_language": "auto",
        "crispasr_vad_enabled": False,
        "crispasr_cache_dir": str(tmp_path / "empty-cache"),
    }
    missing_tiny = crispasr.build_command(
        tmp_path / "audio.wav",
        tmp_path / "output",
        common,
        model_path=model,
    )
    with pytest.raises(ModelAcquisitionDenied):
        _run(missing_tiny, _fail_if_run)

    lid_off = crispasr.build_command(
        tmp_path / "audio.wav",
        tmp_path / "output-off",
        {**common, "stt_lid_backend": "off"},
        model_path=model,
    )
    assert _run(lid_off, lambda *_args, **_kwargs: "lid off") == "lid off"


def test_authorizer_allows_fetch_and_is_rechecked_for_each_native_launch(
    tmp_path: Path,
) -> None:
    command = _local_command(tmp_path)
    command.extend(("--auto-download", "--hf-repo", "owner/repo:model.gguf", "-l", "auto"))
    allowed = [True]
    calls: list[None] = []

    def authorize() -> None:
        calls.append(None)
        if not allowed[0]:
            raise PermissionError("revoked")

    with model_acquisition_scope(authorize):
        assert _run(command, lambda *_args, **_kwargs: "first") == "first"
        allowed[0] = False
        with pytest.raises(ModelAcquisitionDenied):
            _run(command, _fail_if_run)
    assert len(calls) == 2

    with host_model_acquisition_scope():
        assert _run(command, lambda *_args, **_kwargs: "host") == "host"


def test_windows_artifact_cache_hit_needs_no_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_dir = tmp_path / "cache"
    cached = _model_file(cache_dir / "cached.gguf")
    monkeypatch.setattr(crispasr.platform, "system", lambda: "Windows")

    result = crispasr._prefetch_windows_artifact(
        {"crispasr_cache_dir": str(cache_dir)},
        crispasr.CrispASRArtifact("cached.gguf", "https://example.invalid/model"),
        opener=_fail_if_run,
    )
    assert result == cached


def test_windows_artifact_miss_denies_before_opener_and_transcribe_does_not_swallow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_dir = tmp_path / "missing-cache"
    monkeypatch.setattr(crispasr.platform, "system", lambda: "Windows")
    opens: list[None] = []

    def opener(*_args: object, **_kwargs: object) -> object:
        opens.append(None)
        pytest.fail("opener ran before acquisition authorization")

    with pytest.raises(ModelAcquisitionDenied):
        crispasr._prefetch_windows_artifact(
            {"crispasr_cache_dir": str(cache_dir)},
            crispasr.CrispASRArtifact("missing.gguf", "https://example.invalid/model"),
            opener=opener,
        )
    assert opens == []
    assert not cache_dir.exists()

    with pytest.raises(ModelAcquisitionDenied):
        crispasr.transcribe(
            tmp_path / "audio.wav",
            session_dir=tmp_path / "session",
            output_name="recording",
            settings={
                "stt_engine": "whisper",
                "stt_language": "en",
                "crispasr_vad_enabled": False,
                "crispasr_cache_dir": str(cache_dir),
            },
            run_func=_fail_if_run,
        )


@pytest.mark.parametrize("engine", ["whisper", "parakeet", "moss", "qwen3"])
def test_builders_emit_language_chunk_zero_and_disable_implicit_aligner(
    tmp_path: Path, engine: str
) -> None:
    model = _model_file(tmp_path / f"{engine}.gguf")
    aligner = _model_file(tmp_path / "explicit-aligner.gguf")
    settings = {
        "stt_engine": engine,
        "stt_language": "en",
        "stt_chunk_seconds": 0,
        "crispasr_vad_enabled": False,
        "moss_vad_enabled": False,
        "moss_ctc_alignment_enabled": False,
    }
    command = crispasr.build_command(
        tmp_path / "audio.wav",
        tmp_path / "output",
        settings,
        model_path=model,
        aligner_model_path=aligner if engine == "qwen3" else None,
    )

    assert command[command.index("-l") + 1] == "en"
    assert command[command.index("--chunk-seconds") + 1] == ("120" if engine == "moss" else "0")
    assert "--no-auto-aligner" in command
    if engine == "qwen3":
        assert command[command.index("-am") + 1] == str(aligner)
        assert "--require-word-timestamps" in command


@pytest.mark.parametrize("engine", ["whisper", "parakeet", "moss", "qwen3"])
def test_transcribe_explicit_model_path_skips_python_model_prefetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: str
) -> None:
    model = _model_file(tmp_path / f"provided-{engine}.ggml")
    aligner = _model_file(tmp_path / "explicit-aligner.gguf")

    def fail_prefetch(*_args: object, **_kwargs: object) -> Path:
        pytest.fail("explicit model path triggered an alternative model prefetch")

    monkeypatch.setattr(crispasr, "_prefetch_windows_model", fail_prefetch)
    monkeypatch.setattr(crispasr, "_validate_qwen_json", lambda *_args: None)

    def runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
        output_base = Path(command[command.index("-of") + 1])
        Path(f"{output_base}.srt").write_text("", encoding="utf-8")
        Path(f"{output_base}.json").write_text(json.dumps({"transcription": []}), encoding="utf-8")
        assert command[command.index("-m") + 1] == str(model)
        return SimpleNamespace(stdout=b"", stderr=b"")

    result = crispasr.transcribe(
        tmp_path / "audio.wav",
        session_dir=tmp_path / "session",
        output_name="recording",
        settings={
            "stt_engine": engine,
            "stt_language": "en",
            "crispasr_vad_enabled": False,
            "moss_vad_enabled": False,
            "moss_ctc_alignment_enabled": False,
        },
        model_path=model,
        aligner_model_path=aligner if engine == "qwen3" else None,
        run_func=runner,
    )
    assert Path(result.srt_path).is_file()
