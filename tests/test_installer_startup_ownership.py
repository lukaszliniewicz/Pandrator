"""A spawned child remains owned before verified process identity is available."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(
    sys.platform != "linux", reason="Native startup fixture uses a Linux process group"
)
@pytest.mark.parametrize(
    "cut",
    [
        "control",
        "oneshot",
        "persistent",
        "optional-persistent",
        "optional-control",
        "interrupt",
        "transfer",
        "environment",
        "status",
        "popen",
    ],
)
def test_startup_retains_native_child_or_completes_cleanup(cut: str) -> None:
    command = [
        sys.executable,
        str(Path(__file__).parent / "fixtures" / "installer_startup_ownership.py"),
        cut,
    ]
    # Optional immutable source is used only for recorded before regressions.
    if revision := os.environ.get("PANDRATOR_TEST_SUPERVISOR_REVISION"):
        command.extend(["--revision", revision])
    completed = subprocess.run(command, text=True, capture_output=True, timeout=12, check=True)
    result = json.loads(completed.stdout)
    assert result["roots_reaped"] and result["root_removed"] and result["final_owner_released"]
    if cut in ("environment", "status", "popen"):
        assert result["children_created"] == 0
        assert result["startup_error"] is not None and result["log_closed"]
        assert not result["starting_owned"] and not result["lock_acquired"]
        return
    assert result["children_created"] == 1
    if cut in ("persistent", "optional-persistent"):
        assert result["root_active"] and result["starting_owned"]
        assert result["startup_error"] == "OSError" and result["termination_calls"] == 2
        assert not result["log_closed"] and not result["ready"]
        assert result["lock_acquired"] and result["lock_exists"]
        assert not result["managed_owned"] and not result["published_processes"]
    elif cut == "transfer":
        assert result["root_active"] and result["managed_owned"] and not result["starting_owned"]
        assert result["verified_identity_exact"] and result["ready"] and result["lock_acquired"]
        assert result["published_processes"]["service-fixture"]["process_create_time"] > 0
    else:
        assert not result["root_active"] and result["root_returncode"] is not None
        assert result["log_closed"] and not result["managed_owned"] and not result["starting_owned"]
        assert result["termination_calls"] == (2 if cut == "oneshot" else 1)
        if cut == "optional-control":
            assert result["startup_error"] is None and result["ready"] and result["lock_acquired"]
            assert not result["published_processes"]
        else:
            assert result["startup_error"] == (
                "KeyboardInterrupt"
                if cut == "interrupt"
                else "OSError"
                if cut == "oneshot"
                else "RuntimeError"
            )
            assert not result["ready"] and not result["lock_acquired"]
