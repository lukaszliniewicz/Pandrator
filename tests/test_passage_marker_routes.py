"""Integration checks for passage-derived split anchors and immutable takes."""
from copy import deepcopy

import pytest
from sqlalchemy import func, select

from pandrator.web.models import AudioTake, GenerationPlanRevision, GenerationSegment
from tests.test_passage_markers import block


@pytest.fixture
def case():
    from tests.test_web_generation_topology import GenerationTopologyTests
    value = GenerationTopologyTests()
    value.setUp()
    original = block(["😀 A first clause,", "followed by detail."])
    with value.database.session() as session:
        segment = session.get(GenerationSegment, value.initial_segment_ids[0])
        segment.text = original.text
        segment.language = original.language
        segment.speech_block_provenance_json = deepcopy(original.speech_block_provenance_json)
    try:
        yield value
    finally:
        value.tearDown()


def payload(case):
    response = case.client.get(f"/api/v1/sessions/{case.session_id}/generation-segments")
    assert response.status_code == 200
    return response.get_json()


def split(case, page, marker, **changes):
    return case.client.post(
        f"/api/v1/sessions/{case.session_id}/generation-plan/topology",
        headers={**case.headers, "If-Match": f'"{page["plan_revision_id"]}"', "Idempotency-Key": "passage-boundary-route-test"},
        json={"expected_revision_id": page["plan_revision_id"], "action": "split",
              "segment_id": page["items"][0]["id"], "cursor": marker["offset"],
              "text_layer": "display", "passage_boundary_id": marker["id"], **changes},
    )


def test_verified_marker_route_preserves_source_windows_and_old_takes(case):
    page = payload(case)
    marker = page["items"][0]["passage_structure"]["layers"]["display"]["boundaries"][0]
    result = split(case, page, marker)
    assert result.status_code == 201, result.get_json()
    updated = payload(case)
    assert updated["plan_revision_id"] != page["plan_revision_id"]
    left, right = updated["items"][:2]
    assert left["text"] == "😀 A first clause,"
    assert right["text"] == "followed by detail."
    assert not left["takes"] and not right["takes"]
    assert [c["reference"] for c in left["speech_block_provenance"]["source_cues"]] == [1]
    assert [c["reference"] for c in right["speech_block_provenance"]["source_cues"]] == [2]
    with case.database.session() as session:
        assert session.get(AudioTake, case.initial_take_ids[0]) is not None


@pytest.mark.parametrize("alter", ["revision", "cursor", "overlap"])
def test_stale_or_unsafe_marker_never_creates_a_revision(case, alter):
    page = payload(case)
    marker = page["items"][0]["passage_structure"]["layers"]["display"]["boundaries"][0]
    with case.database.session() as session:
        count = session.scalar(select(func.count()).select_from(GenerationPlanRevision))
        segment = session.get(GenerationSegment, page["items"][0]["id"])
        if alter == "revision":
            segment.revision += 1
        elif alter == "overlap":
            provenance = deepcopy(segment.speech_block_provenance_json)
            provenance["source_cues"][1]["start_ms"] = 100
            segment.speech_block_provenance_json = provenance
    response = split(case, page, marker, **({"cursor": marker["offset"] - 1} if alter == "cursor" else {}))
    assert response.status_code in {409, 422}, response.get_json()
    with case.database.session() as session:
        assert session.scalar(select(func.count()).select_from(GenerationPlanRevision)) == count
        assert session.get(AudioTake, case.initial_take_ids[0]) is not None
