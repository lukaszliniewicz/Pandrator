"""Binding export selectors fail before admission or worker output effects."""

from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.artifact_selection import clear_selection
from pandrator.web.export_inputs import resolve_export_inputs, select_media_export
from pandrator.web.jobs import JobQueue
from pandrator.web.models import (
    Artifact,
    Job,
    SessionSetting,
    WorkflowExecutionPlan,
    WorkspaceSetting,
)
from pandrator.web.settings_policy import BUILTIN_DEFAULTS, validate_output_settings
from pandrator.web.subtitle_selection import (
    SubtitleSelectionUnavailable,
    export_requires_captions,
    resolve_subtitle_selection,
)
from pandrator.web.workflows import WorkflowService
from pandrator.web.workspace_settings import WorkspaceSettingsService
from tests import test_export_subtitle_selection as track_fixtures
from tests import test_web_workflow_plans as plan_fixtures


@pytest.mark.parametrize("source,translation,selection,expected", [
    (True, False, "auto", "source"),
    (False, True, "auto", "translation"),
    (True, True, "auto", "dual"),
    (True, False, "source", "source"),
    (False, True, "translation", "translation"),
    (True, True, "dual", "dual"),
    (True, True, "both", "dual"),
])
def test_resolves_only_available_or_explicit_tracks(source, translation, selection, expected):
    assert resolve_subtitle_selection(
        {"subtitle_selection": selection}, source_available=source,
        translation_available=translation, captions_required=True,
    ) == expected


@pytest.mark.parametrize("source,translation,selection,missing", [
    (True, False, "translation", "translation"),
    (False, True, "source", "source"),
    (True, False, "dual", "translation"),
    (False, True, "dual", "source"),
    (False, False, "dual", "source and translation"),
    (False, False, "auto", "source or translation"),
])
def test_missing_required_documents_are_precise(source, translation, selection, missing):
    with pytest.raises(SubtitleSelectionUnavailable, match=missing) as error:
        resolve_subtitle_selection(
            {"subtitle_selection": selection}, source_available=source,
            translation_available=translation, captions_required=True,
        )
    assert error.value.code == "subtitle_document_unavailable"


@pytest.mark.parametrize("workflow,mode,captions,video,required", [
    ("voiceover", "subtitles", "none", False, True),
    ("voiceover", "text", "none", False, True),
    ("voiceover", "media", "soft", True, True),
    ("voiceover", "media", "burned", True, True),
    ("voiceover", "media", "none", True, False),
    ("voiceover", "audio", "soft", True, False),
    ("voiceover", "media", "soft", False, False),
    ("audiobook", "subtitles", "soft", False, False),
    ("audiobook", "video_book", "burned", False, False),
    ("audiobook", "media", "soft", True, False),
])
def test_caption_requirement_matches_export_paths(workflow, mode, captions, video, required):
    assert export_requires_captions(
        workflow_kind=workflow, export_mode=mode, subtitle_mode=captions,
        source_is_video=video,
    ) is required
    if not required:
        resolve_subtitle_selection(
            {"subtitle_selection": "dual"}, source_available=False,
            translation_available=False, captions_required=False,
        )


@pytest.mark.parametrize("settings", [{}, {"subtitle_selection": None}, {"subtitle_selection": ""}])
def test_legacy_implicit_selectors_resolve_auto(settings):
    for source, translation, expected in (
        (True, False, "source"), (False, True, "translation"), (True, True, "dual"),
    ):
        assert resolve_subtitle_selection(
            settings, source_available=source, translation_available=translation,
            captions_required=True,
        ) == expected
    for export_mode, subtitle_mode in (("audio", "soft"), ("media", "none")):
        required = export_requires_captions(
            workflow_kind="voiceover", export_mode=export_mode,
            subtitle_mode=subtitle_mode, source_is_video=True,
        )
        assert required is False
        assert resolve_subtitle_selection(
            settings, source_available=False, translation_available=False,
            captions_required=required,
        ) == "auto"


def test_defaults_and_saved_selector_validation():
    assert BUILTIN_DEFAULTS["output"]["subtitle_selection"] == "auto"
    assert resolve_subtitle_selection(
        {}, source_available=True, translation_available=False, captions_required=True
    ) == "source"
    for value in ("auto", "source", "translation", "dual", "both"):
        validate_output_settings({"subtitle_selection": value})
    for value in (None, "", "unavailable", 42):
        with pytest.raises(ValueError, match="subtitle_selection must"):
            validate_output_settings({"subtitle_selection": value})


class ExportSelectorWorkerTests(TestCase):
    setUp = track_fixtures.ExportSubtitleSelectionTests.setUp
    _artifact = track_fixtures.ExportSubtitleSelectionTests._artifact

    def test_legacy_worker_cannot_fallback_from_explicit_choice(self):
        for selector in ("translation", "dual"):
            payload = {
                "session_id": self.record.id,
                "settings": {"export_mode": "subtitles", "subtitle_selection": selector},
            }
            with self.subTest(selector=selector), patch.object(self.handlers, "_resolve_input") as read:
                inputs = resolve_export_inputs(self.handlers._output_workflow_context(), payload)
                with self.assertRaisesRegex(SubtitleSelectionUnavailable, "translation"):
                    select_media_export(inputs)
                read.assert_not_called()

    def test_translation_only_source_choice_rejects_while_auto_selects_translation(self):
        target = self._artifact("translation.srt", "translated", "translation")
        with self.database.session() as session:
            session.get(Artifact, self.display.id).state = "deleted"
            clear_selection(session, self.record.id, "transcribe")
        context = self.handlers._output_workflow_context()
        payload = {"session_id": self.record.id, "settings": {"export_mode": "subtitles"}}
        self.assertEqual([target.id], [
            item.id for item in select_media_export(resolve_export_inputs(context, payload)).selected_subtitles
        ])
        payload["settings"]["subtitle_selection"] = "source"
        with self.assertRaisesRegex(SubtitleSelectionUnavailable, "source"):
            select_media_export(resolve_export_inputs(context, payload))

    def test_legacy_auto_selects_both(self):
        target = self._artifact("translation.srt", "translated", "translation")
        payload = {"session_id": self.record.id, "settings": {"export_mode": "subtitles"}}
        selection = select_media_export(resolve_export_inputs(self.handlers._output_workflow_context(), payload))
        self.assertEqual([self.display.id, target.id], [item.id for item in selection.selected_subtitles])

    def test_saved_session_choice_stays_binding_and_run_auto_can_override(self):
        with self.database.session() as session:
            session.add(SessionSetting(
                session_id=self.record.id, section="output",
                value_json={"subtitle_selection": "translation"},
            ))
        service = WorkflowService(self.database, JobQueue(self.database))
        with self.assertRaisesRegex(SubtitleSelectionUnavailable, "translation"):
            service.resolve_stage(self.record.id, "export", {"export_mode": "subtitles"})
        resolved = service.resolve_stage(self.record.id, "export", {
            "export_mode": "subtitles", "output": {"subtitle_selection": "auto"},
        })
        self.assertEqual("auto", resolved.payload["settings"]["subtitle_selection"])

    def test_workflow_selector_default_and_saved_global_choice(self):
        settings = WorkspaceSettingsService(self.database)
        self.assertEqual("source", settings.get(self.record.id, "output")["effective"]["subtitle_selection"])
        with self.database.session() as session:
            workspace_id = initial_personal_workspace(session).id
            session.add(WorkspaceSetting(
                workspace_id=workspace_id, key="defaults.output",
                value_json={"subtitle_selection": "translation"},
            ))
        service = WorkflowService(self.database, JobQueue(self.database))
        self.assertEqual("translation", settings.get(self.record.id, "output")["effective"]["subtitle_selection"])
        with self.assertRaisesRegex(SubtitleSelectionUnavailable, "translation"):
            service.resolve_stage(self.record.id, "export", {"export_mode": "subtitles"})
        with self.database.session() as session:
            setting = session.get(WorkspaceSetting, (workspace_id, "defaults.output"))
            setting.value_json = {"subtitle_selection": "auto"}
        resolved = service.resolve_stage(self.record.id, "export", {"export_mode": "subtitles"})
        self.assertEqual("auto", resolved.payload["settings"]["subtitle_selection"])

    def test_mutated_queued_selector_cannot_use_late_translation(self):
        service = WorkflowService(self.database, JobQueue(self.database))
        payload = deepcopy(service.resolve_stage(self.record.id, "export", {
            "export_mode": "subtitles", "subtitle_selection": "auto",
        }).payload)
        self._artifact("late.srt", "late", "translation")
        payload["settings"]["subtitle_selection"] = "dual"
        with self.assertRaisesRegex(SubtitleSelectionUnavailable, "translation"):
            select_media_export(resolve_export_inputs(self.handlers._output_workflow_context(), payload))


class ExportSelectorAdmissionTests(TestCase):
    setUp = plan_fixtures.WorkflowExecutionPlanTests.setUp
    tearDown = plan_fixtures.WorkflowExecutionPlanTests.tearDown
    _ready_session = plan_fixtures.WorkflowExecutionPlanTests._ready_session

    def _counts(self):
        with self.extension["database"].session() as session:
            return tuple(session.scalar(select(func.count()).select_from(model))
                         for model in (Job, WorkflowExecutionPlan))

    def _submit(self, session_id, settings, *, plan):
        if plan:
            url = f"/api/v1/sessions/{session_id}/workflow-plans"
            body = {"target_stage": "export", "overrides": {"output": settings}, "continuation": False}
        else:
            url = f"/api/v1/sessions/{session_id}/stages/export/run"
            body = settings
        return self.client.post(url, json=body, headers=self.headers)

    def test_source_only_rejection_precedes_job_and_plan_creation(self):
        session_id, _, _ = self._ready_session(workflow_kind="subtitles", suffix="srt")
        for plan in (False, True):
            for selector in ("translation", "dual"):
                with self.subTest(plan=plan, selector=selector):
                    before = self._counts()
                    with patch("pathlib.Path.read_text", side_effect=AssertionError("admission file read")):
                        response = self._submit(session_id, {
                            "export_mode": "subtitles", "subtitle_selection": selector,
                        }, plan=plan)
                    self.assertEqual(422 if plan else 409, response.status_code, response.get_json())
                    self.assertEqual("subtitle_document_unavailable", response.get_json()["error"]["code"])
                    self.assertEqual(before, self._counts())
            for selector in ("source", "auto"):
                response = self._submit(session_id, {
                    "export_mode": "subtitles", "subtitle_selection": selector,
                }, plan=plan)
                self.assertEqual(201 if plan else 202, response.status_code, response.get_json())

    def test_source_video_caption_requirements_and_noncaption_exports(self):
        session_id, _, _ = self._ready_session(workflow_kind="voiceover", suffix="mp4")
        for plan in (False, True):
            for mode in ("soft", "burned"):
                before = self._counts()
                response = self._submit(session_id, {
                    "export_mode": "media", "audio_mode": "preserve",
                    "subtitle_mode": mode, "subtitle_selection": "dual",
                }, plan=plan)
                self.assertEqual(422 if plan else 409, response.status_code, response.get_json())
                self.assertEqual(before, self._counts())
            for export_mode, subtitle_mode in (("media", "none"), ("audio", "soft")):
                response = self._submit(session_id, {
                    "export_mode": export_mode, "audio_mode": "preserve",
                    "subtitle_mode": subtitle_mode, "subtitle_selection": "dual",
                }, plan=plan)
                self.assertEqual(201 if plan else 202, response.status_code, response.get_json())

    def test_translation_only_and_dual_available_tracks_at_admission(self):
        session_id, _, _ = self._ready_session(workflow_kind="voiceover", suffix="mp4")
        path = self.extension["paths"].uploads / "selector-translation.srt"
        path.write_text("1\n00:00:00,000 --> 00:00:01,000\nTranslation\n", encoding="utf-8")
        artifacts = self.extension["services"].artifacts
        artifacts.register(path, kind="srt", role="translation", session_id=session_id)
        for plan in (False, True):
            for selector in ("source", "dual"):
                before = self._counts()
                response = self._submit(session_id, {
                    "export_mode": "subtitles", "subtitle_selection": selector,
                }, plan=plan)
                self.assertEqual(422 if plan else 409, response.status_code, response.get_json())
                self.assertEqual(before, self._counts())
            for selector in ("translation", "auto"):
                response = self._submit(session_id, {
                    "export_mode": "subtitles", "subtitle_selection": selector,
                }, plan=plan)
                self.assertEqual(201 if plan else 202, response.status_code, response.get_json())
        source_path = self.extension["paths"].uploads / "selector-source.srt"
        source_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        artifacts.register(source_path, kind="srt", role="transcription", session_id=session_id)
        for plan in (False, True):
            for selector in ("dual", "auto"):
                response = self._submit(session_id, {
                    "export_mode": "subtitles", "subtitle_selection": selector,
                }, plan=plan)
                self.assertEqual(201 if plan else 202, response.status_code, response.get_json())
