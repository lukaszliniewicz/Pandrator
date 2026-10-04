"""Identify installation executables without replacing native process identity."""

import os


def uses_installation_executable(
    installation_root: str, executable: str, command_line: object
) -> bool:
    try:
        root = os.path.normcase(os.path.realpath(installation_root))
        actual = os.path.normcase(os.path.realpath(executable))
        if os.path.commonpath((root, actual)) == root:
            return True
        if not isinstance(command_line, (list, tuple)) or not command_line:
            return False
        invocation: object = command_line[0]
        if not isinstance(invocation, str) or not invocation or not os.path.isabs(invocation):
            return False
        invocation_entry = os.path.normcase(
            os.path.join(
                os.path.realpath(os.path.dirname(invocation)),
                os.path.basename(invocation),
            )
        )
        return (
            os.path.commonpath((root, invocation_entry)) == root
            and os.path.isfile(invocation_entry)
            and os.path.normcase(os.path.realpath(invocation_entry)) == actual
        )
    except (OSError, ValueError):
        return False
