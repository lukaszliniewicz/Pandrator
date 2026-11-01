"""First save compares with the workflow defaults the reader actually saw."""

from __future__ import annotations

from copy import deepcopy

import pytest
from sqlalchemy import select

from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.models import Artifact, SessionStageSelection
from pandrator.web.outcome_plans import OutcomePlanService
from pandrator.web.sessions import SessionService
from tests.web_test_support import prepare_web_test_data_root


@pytest.mark.parametrize("included", [[], ["correct"]])
@pytest.mark.parametrize("change_input", [False, True])
def test_first_save_retains_selection_only_when_effective_input_is_unchanged(
    tmp_path,
    included,
    change_input,
):
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    try:
        record = SessionService(database).create(
            "Legacy outcome defaults", workflow_kind="voiceover", included_stages=included
        )
        path = paths.sessions / record.storage_key / "reviewed-translation.srt"
        path.parent.mkdir()
        path.write_text("1\n00:00:00,000 --> 00:00:01,000\nReviewed translation.\n")
        artifact = ArtifactService(database, paths).register(
            path, kind="srt", role="translation", session_id=record.id
        )
        before = path.read_bytes()
        service = OutcomePlanService(database)
        displayed = service.get(record.id)
        assert displayed["revision"] == 0
        value = deepcopy(displayed["value"])
        if change_input:
            value["inputs"]["translation"] = "source" if included else "correction"
        saved = service.update(record.id, 0, value)
        assert saved["revision"] == 1
        with database.session() as session:
            selection = session.scalar(
                select(SessionStageSelection).where(
                    SessionStageSelection.session_id == record.id,
                    SessionStageSelection.stage_key == "translate",
                )
            )
            assert selection is not None
            assert selection.artifact_id == (None if change_input else artifact.id)
            retained = session.get(Artifact, artifact.id)
            assert retained is not None and retained.state == "current"
        assert path.read_bytes() == before
    finally:
        database.dispose()
