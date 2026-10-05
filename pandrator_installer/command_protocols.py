"""Static contracts for installer command providers."""

from collections.abc import Mapping, Sequence
from os import PathLike
from typing import Protocol

PathArgument = str | PathLike[str]


class CommandProvider(Protocol):
    def run_command(
        self,
        command: str | Sequence[str],
        use_shell: bool = False,
        cwd: PathArgument | None = None,
        env: Mapping[str, str] | None = None,
        log_errors: bool = True,
        timeout: float | None = ...,
    ) -> tuple[str, str]: ...

    def run_pixi_in_env(
        self,
        pandrator_path: PathArgument,
        env_name: str,
        command: list[str],
        cwd: PathArgument | None = None,
        log_errors: bool = True,
    ) -> tuple[str, str]: ...
