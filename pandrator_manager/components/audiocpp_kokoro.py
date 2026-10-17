"""Optional, private Japanese resources for the pinned native Kokoro models.

Keep upstream GGUFs immutable. A separate GGUF contains the same tensors and
voices plus UniDic, matching audio.cpp's embedded-resource contract. Only the
standalone MeCab library is taken from its wheel; Python bindings are not used.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from importlib.resources import files as package_files
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Sequence

from ..artifacts.download import ArtifactDownloader, ArtifactSpec
from ..artifacts.gguf_resources import embed_gguf_files
from ..audio_cpp_packages import AudioCppModelPackage

RECIPE_VERSION = 1
RESOURCE_DIRECTORY = "kokoro-japanese"
MANIFEST_NAME = "install.json"
# The dictionary selected by the upstream converter's `unidic` package, rather
# than a newer, unverified NINJAL dictionary. Source: polm/unidic-py/dicts.json.
UNIDIC_VERSION = "3.1.0+2021-08-31"
UNIDIC = ArtifactSpec(
    url="https://cotonoha-dic.s3-ap-northeast-1.amazonaws.com/unidic-3.1.0.zip",
    sha256="638718c4c63625ab300de4c92c67925d54c0e9e3830009eaa992f29819d59c43",
    size_bytes=525_943_040,
    filename="unidic-3.1.0.zip",
)
DICTIONARY_FILES = ("sys.dic", "unk.dic", "char.bin", "matrix.bin", "dicrc")


@dataclass(frozen=True, slots=True)
class MeCabAsset:
    artifact: ArtifactSpec
    member: str
    library_sha256: str
    filename: str


# PyPI's published SHA-256 pins. The shared libraries expose the native C API
# independently of the CPython extension; no wheel installation is performed.
MECAB_ASSETS = {
    "linux": MeCabAsset(
        ArtifactSpec(
            url="https://files.pythonhosted.org/packages/c7/b8/91a4ef5c366e747bd6fe074f556dbe30efa97e8fb43a623255d1707dd27c/mecab_python3-1.0.12-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
            sha256="864fa2dabc4dda605e89314c03aef060548bb3758acd079f898148eda11a0497",
            size_bytes=589_124,
            filename="mecab_python3-1.0.12-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
        ),
        "mecab_python3.libs/libmecab-eada4a80.so.2.0.0",
        "5718aced8687227b57811c14a9fd238526fc9d74e206a35e0438b1de4f7beb66",
        "libmecab.so.2",
    ),
    "windows": MeCabAsset(
        ArtifactSpec(
            url="https://files.pythonhosted.org/packages/c8/0f/1675d5b9924e490012fe498aa876ba16c7e02fc0bb9c3c0037d92dd1310a/mecab_python3-1.0.12-cp311-cp311-win_amd64.whl",
            sha256="451b19fb0384f114b34e9cdcb1fb168922a94b3051c2c8bba605e0620d1ff533",
            size_bytes=501_742,
            filename="mecab_python3-1.0.12-cp311-cp311-win_amd64.whl",
        ),
        "mecab_python3-1.0.12.data/data/lib/site-packages/MeCab/libmecab.dll",
        "d8ddc0791437ce8c4a187ae7c69a2f683ce423683b7a5ecfda1c4cef436c1cab",
        "libmecab.dll",
    ),
}


def _mecab_asset(system: str) -> MeCabAsset:
    try:
        return MECAB_ASSETS[system.casefold()]
    except KeyError:
        raise ValueError(f"Kokoro Japanese resources are unavailable for {system}.") from None


def kokoro_japanese_download_bytes(system: str) -> int:
    return int(UNIDIC.size_bytes or 0) + int(_mecab_asset(system).artifact.size_bytes or 0)


def kokoro_japanese_environment(root: Path, system: str) -> dict[str, str]:
    library = root / RESOURCE_DIRECTORY / _mecab_asset(system).filename
    if not library.is_file():
        return {}
    return {"AUDIOCPP_MECAB_LIBRARY": str(library.resolve())}


def _sha256(path: Path, check_cancelled: Callable[[], None]) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            check_cancelled()
            digest.update(chunk)
    return digest.hexdigest()


def _prepared_path(slot: Path, package: AudioCppModelPackage) -> Path:
    return (slot / package.config_path).with_suffix(".japanese.gguf")


def _recipe(system: str) -> dict[str, Any]:
    asset = _mecab_asset(system)
    return {
        "version": RECIPE_VERSION,
        "system": system.casefold(),
        "unidic_version": UNIDIC_VERSION,
        "unidic_sha256": UNIDIC.sha256,
        "mecab_wheel_sha256": asset.artifact.sha256,
        "mecab_library_sha256": asset.library_sha256,
    }


def kokoro_japanese_ready(
    slot: Path,
    packages: Sequence[AudioCppModelPackage],
    system: str,
    check_cancelled: Callable[[], None] = lambda: None,
) -> bool:
    """Validate completed staging retries; never trust a partial enrichment."""
    selected = [package for package in packages if package.family == "kokoro_tts"]
    if not selected:
        return True
    try:
        support = slot / RESOURCE_DIRECTORY
        manifest = json.loads((support / MANIFEST_NAME).read_text(encoding="utf-8"))
        if manifest.get("recipe") != _recipe(system):
            return False
        asset = _mecab_asset(system)
        if _sha256(support / asset.filename, check_cancelled) != asset.library_sha256:
            return False
        models = manifest["models"]
        if set(models) != {package.id for package in selected}:
            return False
        config = json.loads((slot / "server.json").read_text(encoding="utf-8"))
        configured_paths = {entry["id"]: entry["path"] for entry in config["models"]}
        for package in selected:
            prepared = _prepared_path(slot, package)
            prepared.resolve().relative_to(slot.resolve())
            evidence = models[package.id]
            if (
                evidence["source_sha256"] != package.sha256[0]
                or evidence["path"] != prepared.relative_to(slot).as_posix()
                or configured_paths.get(package.id) != evidence["path"]
                or _sha256(prepared, check_cancelled) != evidence["sha256"]
            ):
                return False
        return True
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def _extract_member(
    archive: zipfile.ZipFile,
    member: str,
    destination: Path,
    check_cancelled: Callable[[], None],
) -> None:
    info = archive.getinfo(member)
    if info.file_size > 2 * 1024**3 or stat.S_ISLNK(info.external_attr >> 16):
        raise ValueError("Kokoro resource archive member is not a bounded regular file.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(info) as reader, destination.open("xb") as writer:
        while chunk := reader.read(1024 * 1024):
            check_cancelled()
            writer.write(chunk)


def _dictionary_resources(
    archive: Path,
    destination: Path,
    check_cancelled: Callable[[], None],
) -> dict[str, Path]:
    """Extract only native runtime files and notices, never training data."""
    resources: dict[str, Path] = {}
    with zipfile.ZipFile(archive) as source:
        names = source.namelist()
        if len(names) != len(set(names)) or len(names) > 50_000:
            raise ValueError("Invalid UniDic archive directory.")
        roots = [name.removesuffix("dicrc") for name in names if name.endswith("/dicrc")]
        if len(roots) != 1:
            raise ValueError("The pinned UniDic archive must contain one dictionary.")
        prefix = roots[0]
        selected = {f"unidic/{name}": prefix + name for name in DICTIONARY_FILES}
        for member in names:
            if not member.startswith(prefix) or member.endswith("/"):
                continue
            relative = member.removeprefix(prefix)
            path = PurePosixPath(relative)
            if path.is_absolute() or ".." in path.parts or "\\" in relative:
                raise ValueError("Invalid UniDic resource path.")
            if relative.startswith("licenses/"):
                selected["licenses/unidic-dictionary/" + relative.removeprefix("licenses/")] = member
            elif relative == "README":
                selected["licenses/unidic-dictionary-README"] = member
        if not any(name.startswith("licenses/") for name in selected):
            raise ValueError("The pinned UniDic archive has no redistribution notices.")
        for name, member in sorted(selected.items()):
            path = destination / name
            _extract_member(source, member, path, check_cancelled)
            resources[name] = path
    return resources


def prepare_kokoro_japanese(
    slot: Path,
    packages: Sequence[AudioCppModelPackage],
    *,
    system: str,
    cache_root: Path,
    downloader: ArtifactDownloader,
    check_cancelled: Callable[[], None],
) -> dict[str, Any]:
    selected = [package for package in packages if package.family == "kokoro_tts"]
    if not selected:
        return {}
    check_cancelled()
    asset = _mecab_asset(system)
    archive = downloader.download(UNIDIC, cache_root / str(UNIDIC.filename))
    wheel = downloader.download(asset.artifact, cache_root / str(asset.artifact.filename))
    support = slot / RESOURCE_DIRECTORY
    support.mkdir(parents=True, exist_ok=True)
    models: dict[str, dict[str, str]] = {}
    with tempfile.TemporaryDirectory(prefix=".dictionary-", dir=support) as temporary:
        resources = _dictionary_resources(archive, Path(temporary), check_cancelled)
        for name, path in resources.items():
            if name.startswith("licenses/"):
                target = support / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
        library = support / asset.filename
        with zipfile.ZipFile(wheel) as source:
            _extract_member(source, asset.member, library, check_cancelled)
            for name in ("COPYING", "AUTHORS"):
                _extract_member(
                    source,
                    f"mecab_python3-1.0.12.dist-info/licenses/{name}",
                    support / "licenses" / ("mecab-python3-" + name),
                    check_cancelled,
                )
        if _sha256(library, check_cancelled) != asset.library_sha256:
            raise ValueError("The native MeCab library failed SHA-256 verification.")
        (support / "licenses" / "mecab-BSD.txt").write_bytes(
            package_files("pandrator_manager").joinpath("mecab-BSD.txt").read_bytes()
        )
        for package in selected:
            original = slot / package.config_path
            if _sha256(original, check_cancelled) != package.sha256[0]:
                raise ValueError(f"Kokoro source {package.id} failed SHA-256 verification.")
            prepared = _prepared_path(slot, package)
            embed_gguf_files(original, prepared, resources, check_cancelled)
            models[package.id] = {
                "source_sha256": package.sha256[0],
                "path": prepared.relative_to(slot).as_posix(),
                "sha256": _sha256(prepared, check_cancelled),
            }
    manifest = {"recipe": _recipe(system), "models": models}
    temporary_manifest = support / ".install.json.part"
    with temporary_manifest.open("x", encoding="utf-8") as output:
        json.dump(manifest, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temporary_manifest.replace(support / MANIFEST_NAME)
    return manifest
