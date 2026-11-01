"""A named-user video-edit handoff through public commands and real workers."""

from __future__ import annotations

import io
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from werkzeug.test import TestResponse

from pandrator.logic.dubbing.llm_correction import CorrectionResult
from pandrator.web import models as m
from pandrator.web.accounts.actor_models import JobActorRecord
from pandrator.web.jobs import Worker
from pandrator.web.media_process import resolve_ffmpeg_executable
from pandrator.web.projects.copy_publication import ProjectCopyResult
from pandrator.web.subtitle_sources import parse_subtitle_source
from tests.project_copy_test_support import seed_copy_model
from tests.test_account_authentication_integration import AuthCase
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_collaboration_authentication import enable, new_account
from tests.test_two_person_collaboration_flow import SOURCE_SRT, _headers, _login


def _body(response: TestResponse, expected: int = 200) -> dict[str, Any]:
    value = response.get_json()
    assert response.status_code == expected, value
    assert isinstance(value, dict)
    return value


def _worker(case: AuthCase, queued: dict[str, Any], user_id: str) -> dict[str, Any]:
    services = case.app.extensions["pandrator"]["services"]
    # Worker handler registration does not filter claim(). Every job in this
    # isolated installation must belong to this exact requested operation.
    with case.database.snapshot_session() as db:
        pending = list(db.scalars(select(m.Job.id).where(m.Job.status.in_(["queued", "running"]))))
        assert pending == [queued["id"]]
    worker = Worker(services.jobs, "public-media-copy", services.workflow_handlers.handlers())
    assert worker.run_once()
    with case.database.snapshot_session() as db:
        job = db.get_one(m.Job, queued["id"])
        assert job.status == "succeeded", (job.error_code, job.error_message)
        assert db.get_one(JobActorRecord, job.id).user_id == user_id
        return dict(job.result_json or {})


def _video(path: Path) -> bytes:
    subprocess.run(
        [
            resolve_ffmpeg_executable(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x90:r=4:d=10",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=8000:cl=mono",
            "-shortest",
            "-c:v",
            "mpeg4",
            "-q:v",
            "20",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return path.read_bytes()


def _render(case, client, csrf, session_id, revision, user_id, key):
    queued = _body(
        client.post(
            f"/api/v1/sessions/{session_id}/media-edit/render",
            json={"revision": revision, "subtitles_only": False},
            headers=_headers(csrf, key),
        ),
        202,
    )
    assert queued["kind"] == "media_edit.render"
    return _worker(case, queued, user_id)


def _export(case, client, csrf, session_id, user_id, selection, key):
    queued = _body(
        client.post(
            f"/api/v1/sessions/{session_id}/stages/export/run",
            json={
                "output": {
                    "export_mode": "subtitles",
                    "subtitle_format": "srt",
                    "subtitle_selection": selection,
                }
            },
            headers=_headers(csrf, key),
        ),
        202,
    )
    result = _worker(case, queued, user_id)
    artifact_id = result["artifact_ids"][0]
    response = client.get(f"/api/v1/artifacts/{artifact_id}/content")
    assert response.status_code == 200
    return parse_subtitle_source(response.data.decode("utf-8-sig"), "srt")


def _source_state(case, project_id):
    services = case.app.extensions["pandrator"]["services"]
    with case.database.snapshot_session() as db:
        plans = list(
            db.scalars(
                select(m.MediaEditPlan)
                .join(
                    m.SessionRecord,
                    m.SessionRecord.id == m.MediaEditPlan.session_id,
                )
                .where(m.SessionRecord.project_id == project_id)
            )
        )
        headers = {plan.id: plan.active_revision_id for plan in plans}
        revisions = {
            row.id: (
                row.plan_id,
                row.content_hash,
                list(row.keep_ranges_json),
                list(row.cues_json),
                row.instructions,
                row.reviewed,
            )
            for row in db.scalars(
                select(m.MediaEditPlanRevision).where(
                    m.MediaEditPlanRevision.plan_id.in_(headers),
                )
            )
        }
        files = {
            artifact.id: services.paths.managed_path(artifact.relative_path).read_bytes()
            for artifact in db.scalars(
                select(m.Artifact).where(
                    m.Artifact.project_id == project_id,
                    m.Artifact.state != "deleted",
                )
            )
        }
    return headers, revisions, files


def test_named_users_copy_video_cuts_and_continue_after_creator_leaves(
    case: AuthCase,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    new_account(case, "editor")
    new_account(case, "alice")
    enable(case, monkeypatch)

    def block_network(*_args, **_kwargs):
        raise AssertionError("This media handoff must not use the network.")

    monkeypatch.setattr(socket.socket, "connect", block_network)
    editor, editor_csrf, editor_id = _login(case, "editor")
    alice, alice_csrf, alice_id = _login(case, "alice")
    owner, _, _ = _login(case, "owner")
    source = _body(
        editor.post(
            "/api/v1/sessions",
            json={
                "name": "Private recorded lesson",
                "workflow_kind": "media_edit",
                "source_language": "en",
                "multilingual_setup": {"target_languages": ["de"], "generate_voiceover": False},
            },
            headers=_headers(editor_csrf, "media-flow-create"),
        ),
        201,
    )
    sid, root = source["id"], source["project_id"]
    transcript = _body(
        editor.post(
            "/api/v1/uploads",
            data={
                "session_id": sid,
                "file": (io.BytesIO(SOURCE_SRT.encode()), "captions.srt"),
            },
            headers=_headers(editor_csrf, "media-flow-captions"),
        ),
        201,
    )
    _body(
        editor.post(
            "/api/v1/uploads",
            data={
                "session_id": sid,
                "file": (io.BytesIO(_video(tmp_path / "recording.mp4")), "recording.mp4"),
            },
            headers=_headers(editor_csrf, "media-flow-video"),
        ),
        201,
    )
    current = _body(editor.get(f"/api/v1/sessions/{sid}"))
    _body(
        editor.post(
            f"/api/v1/sessions/{sid}/sources",
            json={
                "source_asset_id": transcript["source_asset_id"],
                "role": "transcript",
            },
            headers={
                **_headers(editor_csrf, "media-flow-transcript"),
                "If-Match": f'"{current["revision"]}"',
            },
        ),
        201,
    )
    prepared = _body(
        editor.post(
            f"/api/v1/sessions/{sid}/media-edit/prepare",
            json={},
            headers=_headers(editor_csrf, "media-flow-prepare"),
        ),
        201,
    )
    plan = prepared["plan"]
    ranges = [
        {"id": "opening", "start_ms": 0, "end_ms": 4000, "label": "Opening"},
        {
            "id": "explanation",
            "start_ms": 5000,
            "end_ms": plan["duration_ms"],
            "label": "Explanation",
        },
    ]
    edited = _body(
        editor.put(
            f"/api/v1/sessions/{sid}/media-edit",
            json={
                "keep_ranges": ranges,
                "instructions": "Remove the pause; keep both explanations.",
                "reviewed": True,
            },
            headers={
                **_headers(editor_csrf, "media-flow-cut"),
                "If-Match": f'"{plan["revision"]}"',
            },
        )
    )
    source_plan = edited["plan"]
    rendered = _render(
        case, editor, editor_csrf, sid, source_plan["revision"], editor_id, "media-flow-render"
    )
    assert rendered["subtitle_artifact_id"]

    correction_inputs = []

    def unchanged_correction(directory, input_path, _settings, **_kwargs):
        text = Path(input_path).read_text(encoding="utf-8-sig")
        correction_inputs.append(text)
        output = Path(directory) / "reviewed-captions.srt"
        output.write_text(text, encoding="utf-8")
        return CorrectionResult(srt_content=text, output_path=str(output))

    # The external model boundary alone is synthetic. The actual correction
    # command, registered worker, document publication and ancestry still run.
    monkeypatch.setattr(
        "pandrator.logic.dubbing.llm_correction.correct_srt_file_with_result", unchanged_correction
    )
    provider = _body(
        editor.post(
            "/api/v1/providers",
            json={
                "provider_key": "openai",
                "label": "Private synthetic correction provider",
                "base_url": "http://127.0.0.1:9/v1",
                "api_key": "disposable-media-test-value",
                "options": {"is_custom": True},
            },
            headers=editor_csrf,
        ),
        201,
    )
    _body(
        editor.post(
            f"/api/v1/providers/{provider['id']}/models",
            json={"model_id": "synthetic-caption-review", "is_active": True, "is_default": True},
            headers=editor_csrf,
        ),
        201,
    )
    corrected = _body(
        editor.post(
            f"/api/v1/sessions/{sid}/stages/correct/run",
            json={},
            headers=_headers(editor_csrf, "media-flow-correct"),
        ),
        202,
    )
    correction = _worker(case, corrected, editor_id)
    assert correction_inputs
    current = _body(editor.get(f"/api/v1/sessions/{sid}"))
    translation = _body(
        editor.post(
            f"/api/v1/sessions/{sid}/translation-project",
            json={
                "checkpoint_artifact_id": correction["artifact_id"],
                "expected_revision": current["revision"],
                "create_planned_branches": False,
            },
            headers=_headers(editor_csrf, "media-flow-project"),
        )
    )
    translation = translation["project"]
    branches = _body(
        editor.post(
            f"/api/v1/translation-projects/{translation['id']}/branches",
            json={
                "expected_revision": translation["revision"],
                "targets": [{"target_language": "de"}],
            },
            headers=_headers(editor_csrf, "media-flow-branch"),
        )
    )
    german = branches["project"]["branches"][0]["session_id"]
    german_text = ["Willkommen im Kurs.", "Wir prüfen die Übersetzung."]
    _body(
        editor.post(
            f"/api/v1/sessions/{german}/subtitles/translation/review",
            json={
                "expected_revision": 0,
                "segments": [
                    {"start_ms": start, "end_ms": end, "text": text}
                    for start, end, text in [
                        (1000, 3500, german_text[0]),
                        (5000, 8000, german_text[1]),
                    ]
                ],
            },
            headers=_headers(editor_csrf, "media-flow-german-review"),
        ),
        201,
    )
    assert alice.get(f"/api/v1/sessions/{sid}").status_code == 404
    assert owner.get(f"/api/v1/sessions/{sid}").status_code == 404

    team = _body(
        alice.post(
            "/api/v1/workspaces",
            json={"name": "Video translation workshop"},
            headers=_headers(alice_csrf, "media-flow-team"),
        ),
        201,
    )
    invitation = _body(
        alice.post(
            f"/api/v1/workspaces/{team['id']}/invitations",
            json={
                "user_id": editor_id,
                "role": "member",
                "expected_revision": team["revision"],
            },
            headers=_headers(alice_csrf, "media-flow-invite"),
        ),
        201,
    )
    _body(
        editor.post(
            f"/api/v1/workspace-invitations/{invitation['id']}/accept",
            json={},
            headers=_headers(editor_csrf, "media-flow-accept"),
        )
    )
    before = _source_state(case, root)
    destination_provider, destination_model = seed_copy_model(case, team["id"])
    preview = _body(
        editor.get(
            f"/api/v1/projects/{root}/copy-preview", query_string={"workspace_id": team["id"]}
        )
    )
    assert not preview["ready"]
    assert {item["purpose"] for item in preview["connection_requirements"]} == {
        "correction", "translation"
    }
    choices = [{"requirement_id": item["id"], "provider_id": destination_provider,
        "provider_model_id": destination_model} for item in preview["connection_requirements"]]
    preview = _body(editor.post(f"/api/v1/projects/{root}/copy-preview",
        json={"destination_workspace_id": team["id"], "destination_connections": choices},
        headers=editor_csrf))
    assert preview["ready"], preview["blockers"]
    assert preview["counts"]["media_edit_plans"] >= 1
    assert preview["counts"]["media_edit_plan_revisions"] >= 1
    queued = _body(
        editor.post(
            f"/api/v1/projects/{root}/copy",
            json={
                "destination_workspace_id": team["id"],
                "destination_connections": choices,
                "expected_fingerprint": preview["fingerprint"],
                "name": "Shared recorded lesson",
            },
            headers=_headers(editor_csrf, "media-flow-copy"),
        ),
        202,
    )
    _worker(case, queued, editor_id)
    receipt = _body(editor.get(f"/api/v1/project-copies/{queued['id']}"))
    assert receipt["state"] == "succeeded"
    copied = ProjectCopyResult(**receipt["result"])
    assert copied.translation_project_id is not None
    copied_state = _body(alice.get(f"/api/v1/sessions/{copied.source_session_id}/media-edit"))
    copied_plan = copied_state["plan"]
    assert copied_plan["revision_id"] != source_plan["revision_id"]
    assert copied_plan["content_hash"] != source_plan["content_hash"]
    assert copied_plan["keep_ranges"] == source_plan["keep_ranges"]
    assert copied_plan["cues"] == source_plan["cues"]
    assert copied_plan["instructions"] == source_plan["instructions"]
    view = _body(alice.get(f"/api/v1/translation-projects/{copied.translation_project_id}"))
    copied_german = view["project"]["branches"][0]["session_id"]
    with case.database.snapshot_session() as db:
        copied_group = db.get_one(m.TranslationProject, copied.translation_project_id)
        assert copied_group.source_media_edit_revision_id == copied_plan["revision_id"]
        assert copied_group.source_media_edit_content_hash == copied_plan["content_hash"]
    assert _source_state(case, root) == before
    team_state = _body(editor.get(f"/api/v1/workspaces/{team['id']}"))
    _body(
        editor.post(
            f"/api/v1/workspaces/{team['id']}/leave",
            json={
                "expected_revision": team_state["revision"],
            },
            headers=_headers(editor_csrf, "media-flow-leave"),
        )
    )
    assert editor.get(f"/api/v1/sessions/{copied.source_session_id}").status_code == 404
    exported = _export(
        case, alice, alice_csrf, copied_german, alice_id, "translation", "media-flow-export-german"
    )
    assert [cue.text for cue in exported] == german_text

    next_ranges = [
        {
            "id": "remaining",
            "start_ms": 500,
            "end_ms": copied_plan["duration_ms"],
            "label": "Reviewed lesson",
        }
    ]
    next_state = _body(
        alice.put(
            f"/api/v1/sessions/{copied.source_session_id}/media-edit",
            json={
                "keep_ranges": next_ranges,
                "instructions": "Tighten the opening.",
                "reviewed": True,
            },
            headers={
                **_headers(alice_csrf, "media-flow-destination-cut"),
                "If-Match": f'"{copied_plan["revision"]}"',
            },
        )
    )
    assert next_state["plan"]["revision"] == copied_plan["revision"] + 1
    _render(
        case,
        alice,
        alice_csrf,
        copied.source_session_id,
        next_state["plan"]["revision"],
        alice_id,
        "media-flow-destination-render",
    )
    english = _export(
        case,
        alice,
        alice_csrf,
        copied.source_session_id,
        alice_id,
        "source",
        "media-flow-export-english",
    )
    assert [cue.text for cue in english] == [
        "Welcome to the shared course.",
        "We review each translation together.",
    ]
    assert _source_state(case, root) == before
    assert alice.get(f"/api/v1/sessions/{sid}").status_code == 404
    assert (
        _body(editor.get(f"/api/v1/sessions/{sid}/media-edit"))["plan"]["revision_id"]
        == source_plan["revision_id"]
    )
