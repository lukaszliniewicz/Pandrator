import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
repo_root_str = str(REPO_ROOT)
if repo_root_str not in sys.path:
    sys.path.insert(0, repo_root_str)


@pytest.fixture
def host_execution_actor() -> Iterator[None]:
    """Explicit host provenance for standalone command and workflow tests."""
    from pandrator.web.accounts.execution_actor import HostActor, execution_actor_scope

    with execution_actor_scope(HostActor("pandrator-cli")):
        yield


@pytest.fixture
def host_model_acquisition() -> Iterator[None]:
    """Explicit authority for standalone tests of synthetic model downloads."""
    from pandrator.logic.model_acquisition import host_model_acquisition_scope

    with host_model_acquisition_scope():
        yield
