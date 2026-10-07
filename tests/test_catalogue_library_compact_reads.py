"""Bounded browse projections preserve labels and omit heavy stored payloads."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import event

from pandrator.logic.language_capabilities import canonical_language_tag
from pandrator.logic.model_catalogue import catalogue_page
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import (
    Artifact,
    AudioTake,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationSegment,
    SessionRecord,
    SessionSource,
    SourceAsset,
)
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def browse_case(tmp_path, monkeypatch):
    def unexpected(*_args, **_kwargs):
        raise AssertionError("Unexpected network call")

    monkeypatch.setattr("requests.sessions.Session.request", unexpected)
    monkeypatch.setattr("socket.socket.connect", unexpected)
    prepare_web_test_data_root(tmp_path)
    bootstrap = BootstrapTokenStore()
    app = create_app(data_root=tmp_path, testing=True, bootstrap_tokens=bootstrap)
    client = app.test_client()
    client.post("/api/v1/auth/bootstrap", json={"token": bootstrap.issue()})
    services = app.extensions["pandrator"]
    db = services["database"]
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with db.session() as session:
        session.add_all(
            [
                SessionRecord(id="a", name="Alpha", source_language="en", target_language="ja"),
                SessionRecord(id="b", name="Beta", trashed_at=stamp),
            ]
        )
        session.flush()
        session.add_all(
            [
                SourceAsset(
                    id="source",
                    display_name="Readable source",
                    kind="txt",
                    metadata_json={"large": "x" * 10000},
                ),
                SourceAsset(id="hidden", display_name="Hidden", kind="txt", state="trashed"),
            ]
        )
        session.flush()
        session.add_all(
            [
                SessionSource(
                    id="current",
                    source_asset_id="source",
                    session_id="a",
                    is_current=True,
                    updated_at=stamp,
                ),
                SessionSource(
                    id="historical",
                    source_asset_id="source",
                    session_id="a",
                    role="old",
                    is_current=False,
                    updated_at=stamp,
                ),
                SessionSource(
                    id="trash",
                    source_asset_id="source",
                    session_id="b",
                    is_current=True,
                    updated_at=stamp,
                ),
                SessionSource(id="other", source_asset_id="hidden", session_id="b"),
            ]
        )
    yield client, services, stamp
    services["tts_catalogue"].close()
    services["tts_catalogue"].providers.close()
    db.dispose()


def test_catalogue_global_languages_are_canonical_filter_independent_and_isolated():
    first = catalogue_page(limit=1)
    expected = list(first["languages"])
    assert expected == sorted(set(expected))
    assert {"en", "ja"} <= set(expected)
    assert all(canonical_language_tag(tag) == tag for tag in expected)
    assert not {"unknown", "multilingual", "mul", "any", "all", "zxx"} & set(expected)
    first["languages"].clear()
    first["families"][0]["id"] = "changed"
    filtered = catalogue_page(provider="missing", query="no match", limit=1)
    assert filtered["items"] == []
    assert filtered["languages"] == expected
    assert "changed" not in {row["id"] for row in filtered["families"]}


def test_sources_compact_omissions_counts_projection_and_full_compatibility(browse_case):
    client, services, _stamp = browse_case
    full = client.get("/api/v1/sources").get_json()["items"][0]
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(services["database"].engine, "before_cursor_execute", capture)
    try:
        compact = client.get("/api/v1/sources?view=compact").get_json()["items"][0]
    finally:
        event.remove(services["database"].engine, "before_cursor_execute", capture)
    assert compact == {
        key: value
        for key, value in full.items()
        if key not in {"metadata", "external_path", "path"}
    }
    assert compact["reference_count"] == 3
    assert compact["current_reference_count"] == 2
    assert not any("source_assets.metadata_json" in sql for sql in statements)
    assert sum("GROUP BY" in sql for sql in statements) == 1
    assert (
        len(client.get("/api/v1/sources?view=compact&include_trashed=true").get_json()["items"])
        == 2
    )
    attached = services["source_library"].list(session_id="a", view="compact")
    assert {row["attachment"]["id"] for row in attached} == {"current", "historical"}
    attached[0]["attachment"]["role"] = "mutated"
    assert (
        services["source_library"].list(session_id="a", view="compact")[0]["attachment"]["role"]
        != "mutated"
    )
    assert client.get("/api/v1/sources?view=invalid").status_code == 400


def test_source_references_include_history_trashed_and_stable_paging(browse_case):
    client, _services, _stamp = browse_case
    url = "/api/v1/sources/source/references"
    first = client.get(url + "?limit=2").get_json()
    assert first["total"] == 3 and first["next_offset"] == 2
    assert [row["attachment_id"] for row in first["items"]] == ["current", "trash"]
    assert first["items"][0]["session_name"] == "Alpha"
    assert first["items"][1]["status"] == "trashed"
    last = client.get(url + "?limit=2&offset=2").get_json()
    assert [row["attachment_id"] for row in last["items"]] == ["historical"]
    assert last["next_offset"] is None
    assert client.get("/api/v1/sources/missing/references").status_code == 404
    for query in ("offset=-1", "offset=no", "limit=0", "limit=101"):
        assert client.get(url + "?" + query).status_code == 400
    assert not {"metadata", "path", "settings"} & set(first["items"][0])
    assert client.application.test_client().get(url).status_code in {401, 403}


def test_artifact_compact_audio_pages_take_labels_and_legacy_full(browse_case):
    client, services, stamp = browse_case
    with services["database"].session() as session:
        session.add_all(
            [
                Artifact(
                    id="audio-a",
                    session_id="a",
                    kind="wav",
                    role="assembled_audio",
                    relative_path="a/book.wav",
                    created_at=stamp,
                    metadata_json={"large": "x" * 10000},
                ),
                Artifact(
                    id="audio-b",
                    session_id="a",
                    kind="bin",
                    mime_type="audio/mpeg",
                    role="take",
                    relative_path="a/take.mp3",
                    created_at=stamp,
                ),
                Artifact(
                    id="text",
                    session_id="a",
                    kind="txt",
                    role="upload",
                    relative_path="a/input.txt",
                    created_at=stamp,
                ),
                Artifact(
                    id="video",
                    session_id="a",
                    kind="mp4",
                    mime_type="video/mp4",
                    role="upload",
                    relative_path="a/video.mp4",
                    created_at=stamp,
                ),
                Artifact(
                    id="other-audio",
                    session_id="b",
                    kind="flac",
                    relative_path="b/audio.flac",
                    created_at=stamp,
                ),
            ]
        )
        plan = GenerationPlan(session_id="a")
        session.add(plan)
        session.flush()
        revision = GenerationPlanRevision(
            plan_id=plan.id, revision_number=1, content_hash="fixture"
        )
        session.add(revision)
        session.flush()
        session.add_all(
            [
                GenerationSegment(
                    id="later", plan_revision_id=revision.id, ordinal=8, text="Later", speaker="B"
                ),
                GenerationSegment(
                    id="earlier",
                    plan_revision_id=revision.id,
                    ordinal=3,
                    text="Readable " + "x" * 140,
                    speaker="A",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                AudioTake(generation_segment_id="later", artifact_id="audio-b"),
                AudioTake(generation_segment_id="earlier", artifact_id="audio-b"),
            ]
        )
    url = "/api/v1/artifacts?session_id=a"
    full = client.get(url).get_json()
    assert set(full) == {"items"}
    assert {"metadata_json", "path", "content_hash"} <= set(full["items"][0])
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(services["database"].engine, "before_cursor_execute", capture)
    try:
        first = client.get(url + "&view=compact&media_type=audio&limit=1").get_json()
    finally:
        event.remove(services["database"].engine, "before_cursor_execute", capture)
    assert not any("artifacts.metadata_json" in sql for sql in statements)
    assert first["total"] == 2 and first["next_offset"] == 1
    assert first["items"][0]["id"] == "audio-a"
    second = client.get(url + "&view=compact&media_type=audio&limit=1&offset=1").get_json()
    item = second["items"][0]
    assert item["display_name"] == "take.mp3" and item["session_name"] == "Alpha"
    assert item["segment_ordinal"] == 3 and item["speaker"] == "A"
    assert len(item["segment_text"]) == 120 and item["segment_text"].startswith("Readable")
    assert not {"metadata_json", "path", "relative_path", "content_hash"} & set(item)
    assert second["next_offset"] is None
    assert [
        row["id"]
        for row in client.get(url + "&media_type=audio&output_only=true").get_json()["items"]
    ] == ["audio-a"]
    assert [
        row["id"] for row in client.get(url + "&view=compact&media_type=text").get_json()["items"]
    ] == ["text"]
    for query in ("offset=-1", "media_type=video", "view=invalid"):
        assert client.get(url + "&" + query).status_code == 400
