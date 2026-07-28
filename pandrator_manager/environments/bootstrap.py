"""Pinned, verified, rollback-safe bootstrap of shared runtime tools."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from ..artifacts import ArtifactDownloader, ArtifactSpec, SafeExtractor
from ..context import ManagerContext
from ..errors import CancellationRequested, ManagerError
from ..processes import CommandRunner, CommandSpec

PIXI_VERSION = "0.72.0"
_PIXI_RELEASE_BASE = (
    f"https://github.com/prefix-dev/pixi/releases/download/v{PIXI_VERSION}"
)


@dataclass(frozen=True, slots=True)
class PixiAsset:
    system: str
    architecture: str
    url: str
    sha256: str
    member: str

    @property
    def filename(self) -> str:
        return Path(urlsplit(self.url).path).name

    @property
    def artifact(self) -> ArtifactSpec:
        return ArtifactSpec(
            url=self.url,
            sha256=self.sha256,
            filename=self.filename,
        )


_PIXI_ASSETS = {
    ("windows", "x86_64"): PixiAsset(
        system="windows",
        architecture="x86_64",
        url=f"{_PIXI_RELEASE_BASE}/pixi-x86_64-pc-windows-msvc.zip",
        sha256="dc3a55c204692ad38a52a8c745ff2a0d2e7a48fad2c0d2109f12a486cf8937c4",
        member="pixi.exe",
    ),
    ("linux", "x86_64"): PixiAsset(
        system="linux",
        architecture="x86_64",
        url=f"{_PIXI_RELEASE_BASE}/pixi-x86_64-unknown-linux-musl.tar.gz",
        sha256="2c086608809f7bdd9918323cf6f6278bb43b025f4d957ddfd55295cf151c6f21",
        member="pixi",
    ),
    ("linux", "aarch64"): PixiAsset(
        system="linux",
        architecture="aarch64",
        url=f"{_PIXI_RELEASE_BASE}/pixi-aarch64-unknown-linux-musl.tar.gz",
        sha256="8b48fd8b315552ee48d340e89d654a177d1f001810ab741f51f7dcdd7e00e1c1",
        member="pixi",
    ),
}


def _normalized_system(value: str) -> str:
    selected = value.strip().casefold()
    if selected in {"windows", "win32", "nt", "cygwin"}:
        return "windows"
    if selected in {"linux", "posix"}:
        return "linux"
    return selected


def _normalized_architecture(value: str) -> str:
    selected = value.strip().casefold()
    if selected in {"amd64", "x64", "x86_64"}:
        return "x86_64"
    if selected in {"arm64", "aarch64"}:
        return "aarch64"
    return selected


def pixi_asset_for(system: str, architecture: str) -> PixiAsset:
    platform_key = (
        _normalized_system(system),
        _normalized_architecture(architecture),
    )
    try:
        return _PIXI_ASSETS[platform_key]
    except KeyError:
        raise ManagerError(
            "unsupported_runtime_tool_platform",
            "This platform has no qualified Pixi bootstrap artifact.",
            {
                "tool": "pixi",
                "system": platform_key[0],
                "architecture": platform_key[1],
            },
            409,
        ) from None


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class PixiBootstrapper:
    """Install one qualified Pixi executable inside the manager workspace."""

    def __init__(
        self,
        context: ManagerContext,
        *,
        runner: CommandRunner | None = None,
        downloader: ArtifactDownloader | None = None,
        extractor: SafeExtractor | None = None,
        asset: PixiAsset | None = None,
    ) -> None:
        self.context = context
        self.asset = asset or pixi_asset_for(
            context.system,
            context.architecture,
        )
        self.runner = runner or CommandRunner(
            cancellation=context.cancellation,
            base_environment=context.environment,
        )
        self.downloader = downloader or ArtifactDownloader(
            cancellation=context.cancellation,
            environment=context.environment,
        )
        self.extractor = extractor or SafeExtractor(
            maximum_entries=32,
            maximum_uncompressed_bytes=256 * 1024 * 1024,
        )

    @property
    def target(self) -> Path:
        return self.context.layout.bin / self.asset.member

    @staticmethod
    def _journal_path(operation_staging: Path) -> Path:
        return operation_staging / "runtime" / "pixi-bootstrap.json"

    def _version(self, executable: Path) -> str:
        result = self.runner.run(
            CommandSpec(
                argv=(str(executable), "--version"),
                timeout_seconds=30,
                output_limit_bytes=64 * 1024,
                label="pixi-version",
            )
        )
        output = f"{result.stdout}\n{result.stderr}".strip()
        match = re.search(
            rf"(?<![0-9.]){re.escape(PIXI_VERSION)}(?![0-9.])",
            output,
        )
        if match is None:
            raise RuntimeError(
                f"Pixi {PIXI_VERSION} was required, but the executable "
                f"reported {output[:200] or 'no version'}."
            )
        return PIXI_VERSION

    def _existing_is_qualified(self) -> bool:
        target = self.target
        if not target.is_file():
            return False
        try:
            self._version(target)
        except CancellationRequested:
            raise
        except Exception:
            return False
        return True

    def _read_journal(self, operation_staging: Path) -> dict | None:
        journal_path = self._journal_path(operation_staging)
        if not journal_path.is_file():
            return None
        try:
            payload = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("The Pixi bootstrap rollback journal is invalid.") from error
        if (
            not isinstance(payload, dict)
            or payload.get("tool") != "pixi"
            or payload.get("version") != PIXI_VERSION
            or payload.get("target") != str(self.target)
        ):
            raise RuntimeError("The Pixi bootstrap rollback journal does not match.")
        return payload

    def ensure(
        self,
        operation_staging: Path,
        operation_backup: Path,
        *,
        replace_existing: bool = False,
        offline: bool = False,
    ) -> dict:
        layout = self.context.layout
        operation_staging = layout.require_within(
            operation_staging,
            roots=(layout.staging,),
        )
        operation_backup = layout.require_within(
            operation_backup,
            roots=(layout.backups,),
        )
        target = layout.require_within(
            self.target,
            roots=(layout.bin,),
        )
        journal_path = self._journal_path(operation_staging)
        journal = self._read_journal(operation_staging)
        if self._existing_is_qualified():
            if journal is None:
                return {
                    "tool": "pixi",
                    "version": PIXI_VERSION,
                    "path": str(target),
                    "changed": False,
                }
            return self._result(changed=True)

        if target.exists() and journal is None and not replace_existing:
            raise ManagerError(
                "runtime_tool_conflict",
                "An unqualified Pixi executable already exists at the manager "
                "runtime path and is not positively owned by the manager.",
                {"tool": "pixi", "path": str(target)},
                409,
            )
        if target.exists() and not target.is_file():
            raise ManagerError(
                "runtime_tool_conflict",
                "The manager Pixi runtime path is not a regular file.",
                {"tool": "pixi", "path": str(target)},
                409,
            )

        cache = layout.require_within(
            layout.cache
            / "artifacts"
            / f"pixi-{PIXI_VERSION}"
            / self.asset.filename,
            roots=(layout.cache,),
        )
        archive = self.downloader.download(
            self.asset.artifact,
            cache,
            offline=offline,
        )
        extraction = layout.require_within(
            operation_staging / "runtime" / "pixi",
            roots=(layout.staging,),
        )
        if extraction.exists():
            shutil.rmtree(extraction)
        self.extractor.extract(archive, extraction)
        candidate = layout.require_within(
            extraction / self.asset.member,
            roots=(extraction,),
        )
        if not candidate.is_file():
            raise RuntimeError(
                "The verified Pixi archive does not contain its declared executable."
            )

        backup = layout.require_within(
            operation_backup / "runtime" / target.name,
            roots=(layout.backups,),
        )
        if journal is None:
            journal = {
                "tool": "pixi",
                "version": PIXI_VERSION,
                "target": str(target),
                "backup": str(backup),
                "had_previous": target.is_file(),
            }
            _atomic_json(journal_path, journal)

        backup = Path(str(journal["backup"])).resolve(strict=False)
        layout.require_within(backup, roots=(layout.backups,))
        had_previous = bool(journal.get("had_previous"))
        if had_previous and target.exists() and not backup.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, backup)
        elif journal is not None and target.exists():
            # A journal plus a backup means this target was promoted by the
            # interrupted operation and can be replaced safely on retry.
            if backup.exists() or not had_previous:
                target.unlink()

        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(candidate, target)
        if self.asset.system != "windows":
            target.chmod(0o755)
        self._version(target)
        return self._result(changed=True)

    def _result(self, *, changed: bool) -> dict:
        target = self.target
        digest = _sha256(target)
        return {
            "tool": "pixi",
            "version": PIXI_VERSION,
            "path": str(target),
            "sha256": digest,
            "changed": changed,
            "ownership": {
                "path": str(target),
                "owner_kind": "runtime_tool",
                "owner_id": "pixi",
                "evidence": {
                    "version": PIXI_VERSION,
                    "sha256": digest,
                },
            },
        }

    def rollback(self, operation_staging: Path) -> None:
        layout = self.context.layout
        operation_staging = layout.require_within(
            operation_staging,
            roots=(layout.staging,),
        )
        journal = self._read_journal(operation_staging)
        if journal is None:
            return
        target = Path(str(journal["target"])).resolve(strict=False)
        backup = Path(str(journal["backup"])).resolve(strict=False)
        layout.require_within(target, roots=(layout.bin,))
        layout.require_within(backup, roots=(layout.backups,))
        if bool(journal.get("had_previous")):
            if not backup.exists():
                # The journal is written before the original is moved. If no
                # backup exists, the original target is still in place.
                return
            if target.exists():
                if not target.is_file():
                    raise RuntimeError(
                        "Cannot restore Pixi over a non-file runtime path."
                    )
                target.unlink()
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(backup, target)
            return
        if target.exists():
            if not target.is_file():
                raise RuntimeError(
                    "Cannot roll back a non-file Pixi runtime path."
                )
            target.unlink()
