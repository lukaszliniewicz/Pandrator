"""Stable compatibility and PyInstaller entry point for the Pandrator installer."""

from typing import TYPE_CHECKING

from pandrator_installer.cli import (
    main,
    parse_headless_components,
    parse_launcher_cli_args,
    run_gui_app,
    run_headless_install_from_cli,
    run_self_check,
    run_tls_self_check,
)

if TYPE_CHECKING:
    from pandrator_installer.gui.main_window import PandratorInstaller
    from pandrator_installer.gui.support import (
        HeadlessSignalEmitter,
        HeadlessWorkerProxy,
        InfoDialog,
        QtLogEmitter,
        QtLogHandler,
        Worker,
    )

__all__ = [
    "PandratorInstaller",
    "Worker",
    "QtLogEmitter",
    "HeadlessSignalEmitter",
    "HeadlessWorkerProxy",
    "QtLogHandler",
    "InfoDialog",
    "parse_headless_components",
    "parse_launcher_cli_args",
    "run_headless_install_from_cli",
    "run_gui_app",
    "run_self_check",
    "run_tls_self_check",
    "main",
]


def __getattr__(name):
    if name == "PandratorInstaller":
        from pandrator_installer.gui.main_window import PandratorInstaller

        return PandratorInstaller

    if name in {
        "Worker",
        "QtLogEmitter",
        "HeadlessSignalEmitter",
        "HeadlessWorkerProxy",
        "QtLogHandler",
        "InfoDialog",
    }:
        from pandrator_installer.gui import support

        return getattr(support, name)

    raise AttributeError(name)


if __name__ == "__main__":
    raise SystemExit(main())
