"""Inspect and validate the Python environment selected for an update."""

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .subprocess_env import external_subprocess_environment

_ENVIRONMENT_QUERY = (
    "import json,sys,sysconfig; "
    "print(json.dumps({'prefix': sys.prefix, "
    "'purelib': sysconfig.get_path('purelib'), "
    "'platlib': sysconfig.get_path('platlib')}))"
)


@dataclass(frozen=True, slots=True)
class UpdateEnvironment:
    python: Path
    prefix: Path
    purelib: Path
    platlib: Path


def inspect_update_environment(python: Path) -> UpdateEnvironment:
    result = subprocess.run(
        [str(python), "-I", "-c", _ENVIRONMENT_QUERY],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
        env=external_subprocess_environment(),
    )
    payload: object = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("The update Python reported an invalid installation environment.")
    prefix = payload.get("prefix")
    purelib = payload.get("purelib")
    platlib = payload.get("platlib")
    if (
        not isinstance(prefix, str)
        or not prefix
        or not isinstance(purelib, str)
        or not purelib
        or not isinstance(platlib, str)
        or not platlib
    ):
        raise RuntimeError("The update Python reported an invalid installation environment.")
    return UpdateEnvironment(
        python=python,
        prefix=Path(prefix).resolve(),
        purelib=Path(purelib).resolve(),
        platlib=Path(platlib).resolve(),
    )


def validate_update_environment(python: Path, install_root: Path) -> UpdateEnvironment:
    resolved_root = install_root.resolve()
    selected_path = Path(os.path.abspath(python))
    if not selected_path.is_relative_to(resolved_root):
        raise RuntimeError(
            "Updates require a Python environment owned by this installation. Repair the installation before updating."
        )
    environment = inspect_update_environment(python)
    if (
        environment.prefix == resolved_root
        or not environment.prefix.is_relative_to(resolved_root)
        or not environment.purelib.is_relative_to(environment.prefix)
        or not environment.platlib.is_relative_to(environment.prefix)
    ):
        raise RuntimeError(
            "Updates require a Python environment owned by this installation. Repair the installation before updating."
        )
    return environment


_PACKAGE_QUERY = (
    "import csv\n"
    "import importlib.metadata\n"
    "import json\n"
    "import re\n"
    "import sys\n"
    "def normalize(name):\n"
    "    return re.sub(r'[-_.]+', '-', name).lower()\n"
    "requested = normalize(sys.argv[1])\n"
    "matches = []\n"
    "for dist in importlib.metadata.distributions():\n"
    "    if normalize(dist.metadata.get('Name') or '') != requested:\n"
    "        continue\n"
    "    record = dist.read_text('RECORD')\n"
    "    files = None if record is None else [\n"
    "        str(dist.locate_file(row[0])) if row[0] else ''\n"
    "        for row in csv.reader(record.splitlines()) if row\n"
    "    ]\n"
    "    matches.append({'location': str(dist.locate_file('')), 'files': files})\n"
    "print(json.dumps(matches))\n"
)


def validate_update_package(python: Path, prefix: Path, package_name: str = "pandrator") -> None:
    result = subprocess.run(
        [str(python), "-I", "-c", _PACKAGE_QUERY, package_name],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
        env=external_subprocess_environment(),
    )
    payload: object = json.loads(result.stdout)
    if not isinstance(payload, list):
        raise RuntimeError(
            "The installed package ownership could not be verified. Repair the installation before updating."
        )
    resolved_prefix = prefix.resolve()
    for distribution in payload:
        if not isinstance(distribution, dict):
            raise RuntimeError(
                "The installed package ownership could not be verified. Repair the installation before updating."
            )
        location = distribution.get("location")
        files = distribution.get("files")
        if not isinstance(location, str) or not location or not isinstance(files, list):
            raise RuntimeError(
                "The installed package ownership could not be verified. Repair the installation before updating."
            )
        if not Path(location).resolve().is_relative_to(resolved_prefix):
            raise RuntimeError(
                "The installed package contains files outside this installation. Repair the installation before updating."
            )
        for file in files:
            if not isinstance(file, str) or not file:
                raise RuntimeError(
                    "The installed package ownership could not be verified. Repair the installation before updating."
                )
            if not Path(file).resolve().is_relative_to(resolved_prefix):
                raise RuntimeError(
                    "The installed package contains files outside this installation. Repair the installation before updating."
                )
