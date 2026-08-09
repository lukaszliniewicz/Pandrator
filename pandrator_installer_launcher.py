"""Stable compatibility entry point for legacy headless installer automation."""

from pandrator_installer.cli import (
    main,
    parse_headless_components,
    parse_launcher_cli_args,
    run_headless_install_from_cli,
    run_self_check,
    run_tls_self_check,
)

__all__ = [
    "main",
    "parse_headless_components",
    "parse_launcher_cli_args",
    "run_headless_install_from_cli",
    "run_self_check",
    "run_tls_self_check",
]


if __name__ == "__main__":
    raise SystemExit(main())
