"""Generate complete Pixi manifests and solve each environment once."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from packaging.requirements import Requirement

from ..context import WorkspaceLayout
from ..processes import CommandRunner, CommandSpec


@dataclass(frozen=True, slots=True)
class PixiEnvironmentSpec:
    name: str
    python: str
    conda_packages: tuple[str, ...] = ()
    pypi_packages: tuple[str, ...] = ()
    channels: tuple[str, ...] = ("conda-forge",)
    platforms: tuple[str, ...] = ("win-64", "linux-64", "linux-aarch64")


class PixiEnvironmentManager:
    def __init__(
        self,
        layout: WorkspaceLayout,
        runner: CommandRunner,
        *,
        pixi_executable: Path | None = None,
    ) -> None:
        self.layout = layout
        self.runner = runner
        self.pixi_executable = pixi_executable or (
            layout.bin / ("pixi.exe" if os.name == "nt" else "pixi")
        )

    @staticmethod
    def manifest(spec: PixiEnvironmentSpec) -> str:
        def quote(value: str) -> str:
            return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

        dependencies = [f"python = {quote(spec.python)}"]
        for package in spec.conda_packages:
            name, separator, version = package.partition("=")
            if not name.strip():
                raise ValueError("Conda package name cannot be empty.")
            dependencies.append(
                f"{quote(name.strip())} = {quote(version.strip() if separator else '*')}"
            )
        lines = [
            "[project]",
            f"name = {quote(spec.name)}",
            f"channels = [{', '.join(quote(value) for value in spec.channels)}]",
            f"platforms = [{', '.join(quote(value) for value in spec.platforms)}]",
            "",
            "[dependencies]",
            *dependencies,
        ]
        if spec.pypi_packages:
            requirements = [Requirement(package) for package in spec.pypi_packages]
            lines.extend(
                [
                    "",
                    "[pypi-dependencies]",
                    *(
                        f"{quote(requirement.name)} = "
                        f"{quote(str(requirement.specifier) or '*')}"
                        for requirement in requirements
                    ),
                ]
            )
        return "\n".join(lines) + "\n"

    def ensure(self, spec: PixiEnvironmentSpec) -> Path:
        target = self.layout.environments / spec.name
        target.mkdir(parents=True, exist_ok=True)
        manifest_path = target / "pixi.toml"
        content = self.manifest(spec)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".pixi.",
            suffix=".toml",
            dir=target,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, manifest_path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        arguments = [
            str(self.pixi_executable),
            "install",
            "--manifest-path",
            str(manifest_path),
        ]
        if (target / "pixi.lock").is_file():
            arguments.append("--locked")
        self.runner.run(
            CommandSpec(
                argv=tuple(arguments),
                cwd=target,
                label=f"pixi-{spec.name}",
            )
        )
        return target
