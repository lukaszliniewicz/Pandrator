import json
import os
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

import pandrator_manager
from pandrator_manager import __version__
from pandrator_manager._version import __version__ as leaf_version
from pandrator_manager.autostart import LinuxSystemdAutostart
from pandrator_manager.context import WorkspaceLayout

REPOSITORY = Path(__file__).resolve().parents[1]


def test_lifecycle_uses_runtime_leaf_and_preserves_api_bindings(tmp_path):
    script = textwrap.dedent(
        """
        import json
        import sys
        import pandrator_manager.lifecycle as lifecycle
        assert 'pandrator_manager.launcher' not in sys.modules
        from pandrator_manager.launcher_runtime import external_cleanup_runtime
        assert lifecycle.external_cleanup_runtime is external_cleanup_runtime
        from pandrator_manager.launcher import external_cleanup_runtime as legacy_cleanup
        assert legacy_cleanup is external_cleanup_runtime
        from pandrator_manager.api import create_api, build_openapi
        from pandrator_manager.api.app import create_api as actual_api
        from pandrator_manager.api.openapi import build_openapi as actual_openapi
        assert create_api is actual_api
        assert build_openapi is actual_openapi
        print(json.dumps({'bindings_verified': True}))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(pandrator_manager.__file__).resolve().parent.parent),
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert json.loads(result.stdout) == {"bindings_verified": True}


def test_package_import_is_lazy_and_convenience_exports_are_actual_bindings(tmp_path):
    script = textwrap.dedent(
        """
        import json
        import sys
        import pandrator_manager

        assert 'pandrator_manager.application' not in sys.modules
        assert 'pandrator_manager.context' not in sys.modules
        assert 'pandrator_manager.releases' not in sys.modules

        import pandrator_manager.tray_lifecycle
        import pandrator_manager.uninstall
        assert 'pandrator_manager.tray' not in sys.modules

        from pandrator_manager import (
            ManagerApplication, ManagerContext, WorkspaceLayout, create_application,
        )
        from pandrator_manager.application import (
            ManagerApplication as actual_application,
            create_application as actual_factory,
        )
        from pandrator_manager.context import (
            ManagerContext as actual_context, WorkspaceLayout as actual_layout,
        )
        from pandrator_manager.tray import stop_tray_background
        assert stop_tray_background is pandrator_manager.tray_lifecycle.stop_tray_background
        assert ManagerApplication is actual_application
        assert create_application is actual_factory
        assert ManagerContext is actual_context
        assert WorkspaceLayout is actual_layout
        try:
            pandrator_manager.unknown_contract_export
        except AttributeError as error:
            assert error.args == ('unknown_contract_export',)
        else:
            raise AssertionError('Unknown export did not raise AttributeError')
        print(json.dumps({'exports_verified': True}))
        """
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(
            None,
            (
                str(Path(pandrator_manager.__file__).resolve().parent.parent),
                environment.get("PYTHONPATH"),
            ),
        )
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    assert json.loads(result.stdout) == {"exports_verified": True}


def test_legacy_version_matches_leaf_and_manager_project_version():
    with (REPOSITORY / "pandrator_manager" / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)
    assert __version__ == leaf_version == project["project"]["version"] == "0.9.28"


@pytest.mark.parametrize("executable", [None, ""])
def test_missing_systemctl_never_dispatches_subprocess(tmp_path, executable):
    layout = WorkspaceLayout.from_value(tmp_path / "workspace")
    with patch("pandrator_manager.autostart.shutil.which", return_value=None):
        adapter = LinuxSystemdAutostart(layout, unit_directory=tmp_path / "units")
    adapter.systemctl = executable
    with patch("pandrator_manager.autostart.subprocess.run") as run:
        assert adapter._systemctl_state("is-enabled") == ""
        with pytest.raises(RuntimeError, match=r"^systemctl is unavailable\.$"):
            adapter._systemctl("daemon-reload")
        run.assert_not_called()


def test_systemctl_preserves_process_options_and_state_normalization(tmp_path):
    layout = WorkspaceLayout.from_value(tmp_path / "workspace")
    adapter = LinuxSystemdAutostart(
        layout, unit_directory=tmp_path / "units", systemctl="/fixture/systemctl"
    )
    with patch("pandrator_manager.autostart.subprocess.run") as run:
        adapter._systemctl("disable", "--now", adapter.unit_name, check=False)
        run.assert_called_once_with(
            ["/fixture/systemctl", "--user", "disable", "--now", adapter.unit_name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            timeout=60,
            check=False,
        )
    with patch(
        "pandrator_manager.autostart.subprocess.run",
        return_value=subprocess.CompletedProcess([], 0, " Enabled \n", ""),
    ) as run:
        assert adapter._systemctl_state("is-enabled") == "enabled"
        run.assert_called_once_with(
            ["/fixture/systemctl", "--user", "is-enabled", adapter.unit_name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            timeout=15,
            check=False,
        )
