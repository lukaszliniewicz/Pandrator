"""Kokoro's Japanese add-on is explicit, verified and private to its slot."""

from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from pandrator_manager.application import create_application
from pandrator_manager.artifacts.download import ArtifactSpec
from pandrator_manager.components import audiocpp_kokoro as japanese
from pandrator_manager.components import builtin_registry
from pandrator_manager.components.audiocpp import MODEL_PACKAGES
from pandrator_manager.context import CancellationToken, ManagerContext, WorkspaceLayout
from pandrator_manager.models import ComputeVariant, DesiredComponentState, OperationKind
from pandrator_manager.operations.handlers import FilesystemTaskHandler, OperationTaskContext


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def assets(tmp_path, monkeypatch):
    dictionary = tmp_path / "dictionary.zip"
    with zipfile.ZipFile(dictionary, "w") as output:
        for name in japanese.DICTIONARY_FILES:
            output.writestr("unidic/" + name, ("dictionary:" + name).encode())
        output.writestr("unidic/licenses/BSD", "dictionary redistribution notice")
        output.writestr("unidic/README", "dictionary provenance")
        output.writestr("unidic/model.def", "unused training data")
    library_bytes = b"native standalone MeCab library"
    wheel = tmp_path / "mecab.whl"
    with zipfile.ZipFile(wheel, "w") as output:
        output.writestr("native/libmecab", library_bytes)
        for name in ("AUTHORS", "COPYING"):
            output.writestr("mecab_python3-1.0.12.dist-info/licenses/" + name, name)
        output.writestr("MeCab/python_binding.pyd", "not installed")
    dictionary_spec = ArtifactSpec(
        "https://example.test/dictionary.zip", digest(dictionary),
        dictionary.stat().st_size, dictionary.name,
    )
    wheel_spec = ArtifactSpec(
        "https://example.test/mecab.whl", digest(wheel), wheel.stat().st_size, wheel.name,
    )
    native = japanese.MeCabAsset(
        wheel_spec, "native/libmecab", hashlib.sha256(library_bytes).hexdigest(), "libmecab.so.2",
    )
    monkeypatch.setattr(japanese, "UNIDIC", dictionary_spec)
    monkeypatch.setitem(japanese.MECAB_ASSETS, "linux", native)
    slot = tmp_path / "slot"
    original = slot / MODEL_PACKAGES["kokoro_82m_q8_0"].config_path
    original.parent.mkdir(parents=True)
    original.write_bytes(b"immutable original GGUF")
    package = replace(MODEL_PACKAGES["kokoro_82m_q8_0"], sha256=(digest(original),))
    downloader = mock.Mock()
    def acquire(spec, destination):
        source = dictionary if spec == dictionary_spec else wheel
        assert spec.sha256 == digest(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return destination
    downloader.download.side_effect = acquire
    def embed(source, destination, resources, check_cancelled):
        # The binary-format helper has independent tests; this checks the
        # installer passes the original and the correct native resource set.
        assert source.name == original.name
        assert "unidic/sys.dic" in resources and "unidic/dicrc" in resources
        assert "licenses/unidic-dictionary/BSD" in resources
        assert not any("model.def" in name for name in resources)
        check_cancelled()
        destination.write_bytes(source.read_bytes() + resources["unidic/dicrc"].read_bytes())
    monkeypatch.setattr(japanese, "embed_gguf_files", embed)
    return slot, package, downloader, original, tmp_path / "cache"


def prepare(assets, check_cancelled=lambda: None):
    slot, package, downloader, _, cache = assets
    result = japanese.prepare_kokoro_japanese(
        slot, [package], system="Linux", cache_root=cache,
        downloader=downloader, check_cancelled=check_cancelled,
    )
    (slot / "server.json").write_text(json.dumps({
        "models": [{"id": package.id, "path": result["models"][package.id]["path"]}],
    }))
    return result


def test_default_does_not_download_japanese_resources(tmp_path):
    context = ManagerContext(layout=WorkspaceLayout.from_value(tmp_path), system="Linux", architecture="x86_64")
    registry = builtin_registry()
    definition = registry.definition("audio_cpp")
    driver = registry.driver("audio_cpp")
    desired = DesiredComponentState(
        present=True, compute=ComputeVariant.CPU,
        options={"models": ["kokoro_82m_q8_0"]},
    )
    inspection = driver.inspect(context, definition, desired)
    default = driver.plan_install(context, definition, desired, inspection)[0]
    enabled = desired.model_copy(update={"options": {**desired.options, "kokoro_japanese": "enabled"}})
    selected = driver.plan_install(context, definition, enabled, inspection)[0]
    assert default.inputs["kokoro_japanese"] is False
    assert selected.inputs["kokoro_japanese"] is True
    assert selected.inputs["models"] == default.inputs["models"]
    assert selected.estimated_download_bytes - default.estimated_download_bytes == japanese.kokoro_japanese_download_bytes("Linux")
    assert selected.estimated_disk_bytes - default.estimated_disk_bytes == 3 * 1024**3
    option = next(option for option in definition.install_options if option.key == "kokoro_japanese")
    assert option.default == "disabled"
    assert "MeCab" in option.description and "UniDic" in option.description


def test_unrelated_models_never_acquire_resources(tmp_path):
    downloader = mock.Mock()
    assert japanese.prepare_kokoro_japanese(
        tmp_path, [MODEL_PACKAGES["qwen3_tts_1_7b_base_q8_0"]],
        system="Linux", cache_root=tmp_path / "cache", downloader=downloader,
        check_cancelled=lambda: None,
    ) == {}
    downloader.download.assert_not_called()


def test_preparation_preserves_original_and_writes_verifiable_private_resources(assets):
    slot, package, downloader, original, _ = assets
    source_before = original.read_bytes()
    manifest = prepare(assets)
    assert downloader.download.call_count == 2
    assert original.read_bytes() == source_before
    assert manifest["models"][package.id]["source_sha256"] == package.sha256[0]
    prepared = slot / manifest["models"][package.id]["path"]
    assert prepared != original and prepared.is_file()
    assert japanese.kokoro_japanese_ready(slot, [package], "Linux")
    support = slot / japanese.RESOURCE_DIRECTORY
    assert (support / "licenses/mecab-BSD.txt").is_file()
    assert (support / "licenses/unidic-dictionary/BSD").is_file()
    assert not list(support.glob(".dictionary-*"))
    assert not list(slot.rglob("*.pyd"))
    assert json.loads((support / japanese.MANIFEST_NAME).read_text()) == manifest


@pytest.mark.parametrize("corrupt", ["model", "library", "recipe"])
def test_cached_stage_rejects_modified_prepared_outputs(assets, corrupt):
    slot, package, _, _, _ = assets
    manifest = prepare(assets)
    if corrupt == "model":
        (slot / manifest["models"][package.id]["path"]).write_bytes(b"broken")
    elif corrupt == "library":
        (slot / japanese.RESOURCE_DIRECTORY / "libmecab.so.2").write_bytes(b"broken")
    else:
        path = slot / japanese.RESOURCE_DIRECTORY / japanese.MANIFEST_NAME
        manifest["recipe"]["version"] = -1
        path.write_text(json.dumps(manifest))
    assert not japanese.kokoro_japanese_ready(slot, [package], "Linux")


def test_preparation_rejects_changed_source_before_creating_model(assets):
    slot, _, _, original, _ = assets
    original.write_bytes(b"changed model")
    with pytest.raises(ValueError, match="source.*SHA-256"):
        prepare(assets)
    assert not list(slot.rglob("*.japanese.gguf"))
    assert not (slot / japanese.RESOURCE_DIRECTORY / japanese.MANIFEST_NAME).exists()


def test_retry_rejects_manifest_published_before_server_config(assets):
    slot, package, _, _, _ = assets
    prepare(assets)
    (slot / "server.json").write_text(json.dumps({
        "models": [{"id": package.id, "path": package.config_path}],
    }))
    assert not japanese.kokoro_japanese_ready(slot, [package], "Linux")


def test_cancellation_does_not_publish_completed_marker_or_change_original(assets):
    slot, _, _, original, _ = assets
    before = original.read_bytes()
    calls = 0
    def cancel():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("cancelled")
    with pytest.raises(RuntimeError, match="cancelled"):
        prepare(assets, cancel)
    assert original.read_bytes() == before
    assert not (slot / japanese.RESOURCE_DIRECTORY / japanese.MANIFEST_NAME).exists()
    assert not list(slot.rglob(".dictionary-*"))


@pytest.mark.parametrize("system,filename", [("Linux", "libmecab.so.2"), ("Windows", "libmecab.dll")])
def test_process_uses_only_private_native_library(tmp_path, system, filename):
    assert japanese.kokoro_japanese_environment(tmp_path, system) == {}
    library = tmp_path / japanese.RESOURCE_DIRECTORY / filename
    library.parent.mkdir()
    library.write_bytes(b"native library")
    assert japanese.kokoro_japanese_environment(tmp_path, system) == {
        "AUDIOCPP_MECAB_LIBRARY": str(library.resolve()),
    }


def test_dictionary_archive_path_cannot_escape_staging(tmp_path):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for name in japanese.DICTIONARY_FILES:
            output.writestr("unidic/" + name, name)
        output.writestr("unidic/licenses/../../escape", "unsafe")
    with pytest.raises(ValueError, match="resource path"):
        japanese._dictionary_resources(archive, tmp_path / "output", lambda: None)
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("enabled", [False, True])
def test_real_staging_wires_addon_config_and_retries(assets, tmp_path, monkeypatch, enabled):
    _, package, resource_downloader, original, _ = assets
    monkeypatch.setitem(MODEL_PACKAGES, package.id, package)
    app = create_application(tmp_path / "workspace")
    monkeypatch.setitem(
        japanese.MECAB_ASSETS, app.context.system.casefold(), japanese.MECAB_ASSETS["linux"]
    )
    desired = DesiredComponentState(
        compute=ComputeVariant.CPU,
        options={"models": [package.id], "kokoro_japanese": "enabled" if enabled else "disabled"},
    )
    plan = app.plan(kind=OperationKind.INSTALL, desired={"audio_cpp": desired}, persist=False)
    stage = next(task for task in plan.tasks if task.kind == "stage_audio_cpp")
    archive = tmp_path / "runtime.zip"
    server = "audiocpp_server.exe" if app.context.system.casefold() == "windows" else "audiocpp_server"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(server, "server")
        output.writestr("tools/model_manager_v2.py", "installer")
        output.writestr("model_specs/kokoro_tts.json", json.dumps({
            "packages": [{"id": package.id, "download": {}}],
        }))
    execution = OperationTaskContext(
        context=app.context, store=app.store, registry=app.registry, supervisor=None,
        operation=SimpleNamespace(id="kokoro-addon-test"), plan=plan, prior_results={},
        cancellation=CancellationToken(),
    )
    def acquire(spec, destination, **kwargs):
        if spec == japanese.UNIDIC or spec == japanese.MECAB_ASSETS["linux"].artifact:
            return resource_downloader.download(spec, destination)
        return archive
    def install_model(spec):
        argv = list(spec.argv)
        models = Path(argv[argv.index("--models-root") + 1])
        for path in package.required_paths(models):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(original.read_bytes())
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    handler = FilesystemTaskHandler()
    with (
        mock.patch("pandrator_manager.operations.staging_tasks.ArtifactDownloader.download", side_effect=acquire),
        mock.patch("pandrator_manager.operations.staging_tasks.CommandRunner.run", side_effect=install_model),
    ):
        result = handler.execute(execution, stage)
        slot = Path(result["staged_path"])
        config_path = slot / "server.json"
        config = json.loads(config_path.read_text())
        assert config["models"][0]["path"].endswith(".japanese.gguf") == enabled
        assert resource_downloader.download.call_count == (2 if enabled else 0)
        assert handler.execute(execution, stage)["reused"] is True
        if enabled:
            config["models"][0]["path"] = package.config_path
            config_path.write_text(json.dumps(config))
            assert handler.execute(execution, stage)["reused"] is False
            assert json.loads(config_path.read_text())["models"][0]["path"].endswith(".japanese.gguf")
