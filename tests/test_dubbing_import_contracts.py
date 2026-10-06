"""Exercise the dubbing package's lazy exports in fresh Python processes."""

import subprocess
import sys


def test_package_import_keeps_domain_services_unloaded():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import pandrator.logic.dubbing as dubbing; "
            "assert not any('pandrator.logic.dubbing.' + name in sys.modules "
            "for name in dubbing.__all__)",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_declared_exports_resolve_to_the_concrete_modules():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib; import pandrator.logic.dubbing as dubbing; "
            "namespace = {}; exec('from pandrator.logic.dubbing import *', namespace); "
            "assert all(namespace[name] is "
            "importlib.import_module('pandrator.logic.dubbing.' + name) "
            "for name in dubbing.__all__)",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
