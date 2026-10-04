"""Native orphan admission and executable-invocation ownership boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pandrator_installer.process_paths import uses_installation_executable


@pytest.mark.skipif(sys.platform != "linux", reason="Native orphan fixture uses Linux subreaping")
@pytest.mark.parametrize("cut", ["control", "orphan"])
def test_install_admission_after_native_launcher_exit(tmp_path: Path, cut: str) -> None:
    output = tmp_path / "result.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parent / "fixtures" / "installer_orphan_admission.py"),
            "accepted",
            cut,
            str(output),
        ],
        text=True,
        capture_output=True,
        timeout=15,
        check=True,
    )
    result = json.loads(completed.stdout)
    assert result["child_reaped"] and result["root_removed"]
    assert result["owned_invocation_symlink"] and result["native_executable_outside_root"]
    assert result["admission"]["metadata_preserved"]
    if cut == "orphan":
        assert result["host_returncode"] == 2 and result["host"]["starting_keys"]
        assert not result["host"]["managed_keys"] and not result["host"]["state_exists"]
        assert result["child_active_after_host_exit"]
        assert result["child_parent_after_exit"] == result["subreaper_pid"]
        assert result["child_still_same_identity_after_probe"]
        assert not result["admission"]["metadata_active"]
        assert not result["admission"]["admitted"] and result["admission"]["error"]
        record = next(
            r for r in result["admission"]["inventory"] if r["pid"] == result["child"]["pid"]
        )
        # Invocation discovery must preserve kernel identity for later revalidation.
        assert record["exe"] == result["native_executable"]
        assert record["create_time"] == result["host"]["identities"][0]["create_time"]
    else:
        assert result["host_returncode"] == 0 and not result["child_active_after_host_exit"]
        assert result["admission"]["admitted"] and not result["admission"]["inventory"]


@pytest.mark.skipif(os.name == "nt", reason="Native fixture uses POSIX executable symlinks")
def test_only_owned_final_executable_symlink_can_match_external_binary(tmp_path: Path) -> None:
    root = tmp_path / "installation"
    binary = tmp_path / "external" / "python"
    binary.parent.mkdir()
    binary.write_bytes(b"fixture executable")
    directory = root / "env" / "bin"
    directory.mkdir(parents=True)
    invocation = directory / "python"
    invocation.symlink_to(binary)
    assert uses_installation_executable(str(root), str(binary), [str(invocation)])
    assert not uses_installation_executable(str(root), str(binary), [str(binary), str(invocation)])
    assert not uses_installation_executable(str(root), str(binary), [str(invocation) + "-missing"])
    assert not uses_installation_executable(
        str(root), str(binary) + "-different", [str(invocation)]
    )
    outside_directory = root / "external-directory"
    outside_directory.symlink_to(binary.parent, target_is_directory=True)
    assert not uses_installation_executable(
        str(root), str(binary), [str(outside_directory / "python")]
    )
    sibling = tmp_path / "installation-extra"
    sibling.mkdir()
    sibling_invocation = sibling / "python"
    sibling_invocation.symlink_to(binary)
    assert not uses_installation_executable(str(root), str(binary), [str(sibling_invocation)])


@pytest.mark.parametrize("command", [None, [], "python", [None], [1], [""], ["env/bin/python"]])
def test_external_executable_requires_absolute_string_argv0(
    tmp_path: Path, command: object
) -> None:
    assert not uses_installation_executable(
        str(tmp_path / "installation"), str(tmp_path / "outside" / "python"), command
    )


def test_physical_installed_binary_still_matches_without_command_line(tmp_path: Path) -> None:
    root = tmp_path / "installation"
    assert uses_installation_executable(str(root), str(root / "bin" / "python"), None)
