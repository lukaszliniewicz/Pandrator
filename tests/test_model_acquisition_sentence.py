"""Sentence-segmenter asset policy tests use only fake cache and download APIs."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest

from pandrator.logic import sentence_segmenter
from pandrator.logic import sentence_segmenter_assets as assets
from pandrator.logic.model_acquisition import (
    ModelAcquisitionDenied,
    model_acquisition_scope,
)

MODEL_REPOSITORY = "segment-any-text/sat-12l-sm"
TOKENIZER_REPOSITORY = "facebookAI/xlm-roberta-base"
MODEL_FILENAME = "model_optimized.onnx"
TOKENIZER_FILENAME = "tokenizer.json"
MODEL_COMMIT = "a" * 40
TOKENIZER_COMMIT = "b" * 64
INCOMPLETE_CACHE_MESSAGE = "The sentence segmentation model cache is incomplete."


def _write(path: Path, contents: bytes = b"synthetic sentence asset") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    return path


def _bundle(root: Path) -> assets.SentenceSegmenterAssets:
    model_directory = root / "models--segment-any-text--sat-12l-sm" / "snapshots" / MODEL_COMMIT
    tokenizer_path = (
        root
        / "models--facebookAI--xlm-roberta-base"
        / "snapshots"
        / TOKENIZER_COMMIT
        / TOKENIZER_FILENAME
    )
    _write(model_directory / MODEL_FILENAME)
    _write(model_directory / "config.json")
    _write(tokenizer_path)
    return assets.SentenceSegmenterAssets(
        model_directory=model_directory,
        tokenizer_path=tokenizer_path,
    )


def _cached_lookup(
    monkeypatch: pytest.MonkeyPatch,
    paths: dict[tuple[str, str], object],
) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    def lookup(*, repo_id: str, filename: str) -> object:
        calls.append((repo_id, filename))
        return paths.get((repo_id, filename))

    monkeypatch.setattr(assets, "try_to_load_from_cache", lookup)
    return calls


def _unexpected_download(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("hf_hub_download ran unexpectedly")


def test_complete_cached_bundle_needs_no_grant_or_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    cache_calls = _cached_lookup(
        monkeypatch,
        {
            (MODEL_REPOSITORY, MODEL_FILENAME): str(bundle.model_directory / MODEL_FILENAME),
            (TOKENIZER_REPOSITORY, TOKENIZER_FILENAME): str(bundle.tokenizer_path),
        },
    )
    downloads: list[None] = []

    def unexpected_download(*_args: object, **_kwargs: object) -> object:
        downloads.append(None)
        return _unexpected_download(*_args, **_kwargs)

    monkeypatch.setattr(assets, "hf_hub_download", unexpected_download)

    with model_acquisition_scope(None):
        resolved = assets.resolve_sentence_segmenter_assets()

    assert resolved == bundle
    assert cache_calls == [
        (MODEL_REPOSITORY, MODEL_FILENAME),
        (TOKENIZER_REPOSITORY, TOKENIZER_FILENAME),
    ]
    assert downloads == []


def test_all_missing_bundle_is_denied_before_any_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_calls = _cached_lookup(monkeypatch, {})
    downloads: list[None] = []

    def unexpected_download(*_args: object, **_kwargs: object) -> object:
        downloads.append(None)
        return _unexpected_download(*_args, **_kwargs)

    monkeypatch.setattr(assets, "hf_hub_download", unexpected_download)

    with model_acquisition_scope(None), pytest.raises(ModelAcquisitionDenied):
        assets.resolve_sentence_segmenter_assets()

    assert cache_calls == [(MODEL_REPOSITORY, MODEL_FILENAME)]
    assert downloads == []


def test_each_missing_file_requires_a_fresh_grant_and_pins_config_to_model_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cached_lookup(monkeypatch, {})
    model_directory = tmp_path / "models--segment-any-text--sat-12l-sm" / "snapshots" / MODEL_COMMIT
    tokenizer_path = (
        tmp_path
        / "models--facebookAI--xlm-roberta-base"
        / "snapshots"
        / TOKENIZER_COMMIT
        / TOKENIZER_FILENAME
    )
    authorization_calls: list[None] = []
    downloads: list[tuple[str, str, str | None]] = []

    def authorize() -> None:
        authorization_calls.append(None)

    def download(*, repo_id: str, filename: str, revision: str | None = None) -> str:
        downloads.append((repo_id, filename, revision))
        if (repo_id, filename) == (MODEL_REPOSITORY, MODEL_FILENAME):
            return str(_write(model_directory / MODEL_FILENAME))
        if (repo_id, filename) == (MODEL_REPOSITORY, "config.json"):
            assert revision == MODEL_COMMIT
            return str(_write(model_directory / "config.json"))
        if (repo_id, filename) == (TOKENIZER_REPOSITORY, TOKENIZER_FILENAME):
            return str(_write(tokenizer_path))
        raise AssertionError(f"unexpected download: {repo_id}/{filename}")

    monkeypatch.setattr(assets, "hf_hub_download", download)

    with model_acquisition_scope(authorize):
        resolved = assets.resolve_sentence_segmenter_assets()

    assert authorization_calls == [None, None, None]
    assert downloads == [
        (MODEL_REPOSITORY, MODEL_FILENAME, None),
        (MODEL_REPOSITORY, "config.json", MODEL_COMMIT),
        (TOKENIZER_REPOSITORY, TOKENIZER_FILENAME, None),
    ]
    assert resolved.model_directory == model_directory
    assert resolved.tokenizer_path == tokenizer_path
    assert (resolved.model_directory / "config.json").is_file()


def test_revocation_after_first_fetch_denies_the_next_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cached_lookup(monkeypatch, {})
    model_directory = tmp_path / "models--segment-any-text--sat-12l-sm" / "snapshots" / MODEL_COMMIT
    allowed = [True]
    authorization_calls: list[None] = []
    downloads: list[tuple[str, str]] = []

    def authorize() -> None:
        authorization_calls.append(None)
        if not allowed[0]:
            raise PermissionError("authorization was revoked")

    def download(*, repo_id: str, filename: str, revision: str | None = None) -> str:
        downloads.append((repo_id, filename))
        assert revision is None
        allowed[0] = False
        return str(_write(model_directory / MODEL_FILENAME))

    monkeypatch.setattr(assets, "hf_hub_download", download)

    with model_acquisition_scope(authorize), pytest.raises(ModelAcquisitionDenied):
        assets.resolve_sentence_segmenter_assets()

    assert authorization_calls == [None, None]
    assert downloads == [(MODEL_REPOSITORY, MODEL_FILENAME)]
    assert not (model_directory / "config.json").exists()


def test_missing_config_is_fetched_at_the_onnx_commit_and_wrong_snapshot_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_directory = tmp_path / "models--segment-any-text--sat-12l-sm" / "snapshots" / MODEL_COMMIT
    onnx_path = _write(model_directory / MODEL_FILENAME)
    _cached_lookup(monkeypatch, {(MODEL_REPOSITORY, MODEL_FILENAME): str(onnx_path)})
    downloads: list[tuple[str, str, str | None]] = []
    other_config = (
        tmp_path / "models--segment-any-text--sat-12l-sm" / "snapshots" / ("c" * 40) / "config.json"
    )

    def download(*, repo_id: str, filename: str, revision: str | None = None) -> str:
        downloads.append((repo_id, filename, revision))
        return str(_write(other_config))

    monkeypatch.setattr(assets, "hf_hub_download", download)

    with (
        model_acquisition_scope(lambda: None),
        pytest.raises(RuntimeError, match=f"^{INCOMPLETE_CACHE_MESSAGE}$"),
    ):
        assets.resolve_sentence_segmenter_assets()

    assert downloads == [(MODEL_REPOSITORY, "config.json", MODEL_COMMIT)]


@pytest.mark.parametrize("bad_result", ["missing", "empty", "relative"])
def test_invalid_download_results_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_result: str,
) -> None:
    _cached_lookup(monkeypatch, {})
    missing_path = tmp_path / "not-created" / MODEL_FILENAME
    empty_path = tmp_path / "empty" / MODEL_FILENAME
    if bad_result == "empty":
        _write(empty_path, b"")
    result: str | Path = {
        "missing": missing_path,
        "empty": empty_path,
        "relative": Path("relative") / MODEL_FILENAME,
    }[bad_result]
    authorization_calls: list[None] = []

    def authorize() -> None:
        authorization_calls.append(None)

    monkeypatch.setattr(
        assets,
        "hf_hub_download",
        lambda **_kwargs: str(result),
    )

    with (
        model_acquisition_scope(authorize),
        pytest.raises(RuntimeError, match=f"^{INCOMPLETE_CACHE_MESSAGE}$"),
    ):
        assets.resolve_sentence_segmenter_assets()

    assert authorization_calls == [None]


@pytest.mark.parametrize("bad_result", ["missing", "empty", "relative"])
def test_invalid_cached_onnx_paths_fail_closed_without_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_result: str,
) -> None:
    missing_path = tmp_path / "not-created" / MODEL_FILENAME
    empty_path = tmp_path / "empty" / MODEL_FILENAME
    if bad_result == "empty":
        _write(empty_path, b"")
    result: str | Path = {
        "missing": missing_path,
        "empty": empty_path,
        "relative": Path("relative") / MODEL_FILENAME,
    }[bad_result]
    _cached_lookup(monkeypatch, {(MODEL_REPOSITORY, MODEL_FILENAME): str(result)})
    monkeypatch.setattr(assets, "hf_hub_download", _unexpected_download)

    with (
        model_acquisition_scope(None),
        pytest.raises(RuntimeError, match=f"^{INCOMPLETE_CACHE_MESSAGE}$"),
    ):
        assets.resolve_sentence_segmenter_assets()


def test_cached_tokenizer_path_must_be_absolute_existing_and_nonempty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    empty_tokenizer = _write(tmp_path / "empty-tokenizer" / TOKENIZER_FILENAME, b"")
    _cached_lookup(
        monkeypatch,
        {
            (MODEL_REPOSITORY, MODEL_FILENAME): str(bundle.model_directory / MODEL_FILENAME),
            (TOKENIZER_REPOSITORY, TOKENIZER_FILENAME): str(empty_tokenizer),
        },
    )
    monkeypatch.setattr(assets, "hf_hub_download", _unexpected_download)

    with (
        model_acquisition_scope(None),
        pytest.raises(RuntimeError, match=f"^{INCOMPLETE_CACHE_MESSAGE}$"),
    ):
        assets.resolve_sentence_segmenter_assets()


def test_onnx_cache_path_must_name_a_commit_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invalid_onnx = _write(tmp_path / "snapshots" / "main" / MODEL_FILENAME)
    _cached_lookup(monkeypatch, {(MODEL_REPOSITORY, MODEL_FILENAME): str(invalid_onnx)})
    monkeypatch.setattr(assets, "hf_hub_download", _unexpected_download)

    with (
        model_acquisition_scope(None),
        pytest.raises(RuntimeError, match=f"^{INCOMPLETE_CACHE_MESSAGE}$"),
    ):
        assets.resolve_sentence_segmenter_assets()


def test_segmenter_constructor_uses_absolute_local_paths_and_no_hub_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    events: list[str] = []
    constructor = Mock(side_effect=lambda *_args, **_kwargs: events.append("construct"))
    fake_module = ModuleType("wtpsplit_lite")
    fake_module.__dict__["SaT"] = constructor
    monkeypatch.setitem(sys.modules, "wtpsplit_lite", fake_module)
    monkeypatch.setattr(sentence_segmenter, "_ONNX_RUNTIME_IMPORT_ERROR", None)

    def resolve() -> assets.SentenceSegmenterAssets:
        events.append("resolve")
        return bundle

    monkeypatch.setattr(sentence_segmenter, "resolve_sentence_segmenter_assets", resolve)

    sentence_segmenter._create_segmenter()

    assert events == ["resolve", "construct"]
    args, kwargs = constructor.call_args
    assert args == (bundle.model_directory,)
    assert isinstance(args[0], Path) and args[0].is_absolute()
    assert kwargs == {
        "tokenizer_name_or_path": bundle.tokenizer_path,
        "hub_prefix": None,
        "ort_providers": ["CPUExecutionProvider"],
    }
    assert isinstance(kwargs["tokenizer_name_or_path"], Path)
    assert kwargs["tokenizer_name_or_path"].is_absolute()


def test_transient_acquisition_denial_does_not_poison_loader_or_reload_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    segmenter = Mock()
    segmenter.split.return_value = ["One sentence."]
    constructor = Mock(return_value=segmenter)
    fake_module = ModuleType("wtpsplit_lite")
    fake_module.__dict__["SaT"] = constructor
    monkeypatch.setitem(sys.modules, "wtpsplit_lite", fake_module)
    monkeypatch.setattr(sentence_segmenter, "_ONNX_RUNTIME_IMPORT_ERROR", None)
    monkeypatch.setattr(sentence_segmenter, "_SEGMENTER", None)
    monkeypatch.setattr(sentence_segmenter, "_SEGMENTER_FAILED", False)
    resolve = Mock(side_effect=[ModelAcquisitionDenied(), bundle])
    monkeypatch.setattr(sentence_segmenter, "resolve_sentence_segmenter_assets", resolve)

    with model_acquisition_scope(None):
        assert sentence_segmenter.is_available() is False
    assert sentence_segmenter._SEGMENTER_FAILED is False

    with model_acquisition_scope(lambda: None):
        assert sentence_segmenter.is_available() is True
        assert sentence_segmenter.is_available() is True
        assert sentence_segmenter.split_text("One sentence.") == ["One sentence."]

    assert resolve.call_count == 2
    constructor.assert_called_once_with(
        bundle.model_directory,
        tokenizer_name_or_path=bundle.tokenizer_path,
        hub_prefix=None,
        ort_providers=["CPUExecutionProvider"],
    )
    assert segmenter.split.call_count == 1


def test_huggingface_hub_remains_an_optional_lazy_dependency() -> None:
    code = """import builtins
original_import = builtins.__import__
def block_huggingface_hub(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "huggingface_hub" or name.startswith("huggingface_hub."):
        raise ModuleNotFoundError("huggingface_hub blocked by optional-dependency smoke", name=name)
    return original_import(name, globals, locals, fromlist, level)
builtins.__import__ = block_huggingface_hub
from pandrator.logic import sentence_segmenter
sentence_segmenter._ONNX_RUNTIME_IMPORT_ERROR = None
assert sentence_segmenter.split_text("One sentence.") is None
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
