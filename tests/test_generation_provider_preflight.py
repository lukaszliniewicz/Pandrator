"""Read-only, redacted generation provider preflight coverage."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import func, select

from pandrator.runtime import DataPaths
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.credentials import ResolvedCredential
from pandrator.web.database import Database
from pandrator.web.generation_provider_preflight import generation_provider_preflight
from pandrator.web.models import GenerationRun, GenerationSegment, Job


class GenerationProviderPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = Database(self.root / "pandrator.sqlite3")
        self.addCleanup(self.database.dispose)

    def _vertex_snapshot(
        self,
        *,
        secret_ref: str = "",
        project: str = "",
        location: str = "global",
        model: str = "gemini-3.8-flash-tts",
    ) -> dict:
        return {
            "tts": {
                "service": "vertex_ai",
                "model": model,
                "voice": "Kore",
                "provider_configs": [
                    {
                        "id": "vertex_ai",
                        "secret_ref": secret_ref,
                        "vertex_project": project,
                        "vertex_location": location,
                    }
                ],
            }
        }

    def test_file_credential_redaction_and_explicit_project_precedence_without_probe(self):
        secret_path = self.root / "vertex-service-account.json"
        secret_path.write_text(
            json.dumps(
                {
                    "type": "service_account",
                    "project_id": "json-project",
                    "private_key": "PRIVATE_KEY_SENTINEL",
                }
            ),
            encoding="utf-8",
        )
        snapshot = self._vertex_snapshot(
            secret_ref=f"file-path:{secret_path}", project="explicit-project"
        )

        with (
            patch(
                "pandrator.web.generation_provider_preflight.resolve_provider_credential",
                return_value=ResolvedCredential(value=secret_path.read_text(encoding="utf-8"), source="file"),
            ) as resolve,
            patch("pandrator.logic.tts_handler._vertex_access_token", side_effect=AssertionError("auth probe")),
            patch("requests.sessions.Session.request", side_effect=AssertionError("HTTP request")),
        ):
            result = generation_provider_preflight(self.database, snapshot)

        resolve.assert_called_once_with(
            self.database,
            DataPaths(self.database.path.parent),
            "vertex_ai",
            f"file-path:{secret_path}",
            fallback_environment_variable="",
        )
        self.assertEqual("vertex_ai", result["route_id"])
        self.assertEqual("gemini-3.8-flash-tts", result["model"])
        self.assertEqual("Kore", result["voice"])
        self.assertEqual("file", result["credential_origin"])
        self.assertIs(result["credential_configured"], True)
        self.assertEqual("explicit-project", result["project"])
        self.assertEqual("service", result["project_resolution"])
        self.assertEqual("global", result["location"])
        self.assertFalse(result["access_checked"])
        serialized = repr(result)
        self.assertNotIn("PRIVATE_KEY_SENTINEL", serialized)
        self.assertNotIn(str(secret_path), serialized)
        self.assertNotIn("secret_ref", serialized)

    def test_file_credential_project_is_used_when_service_project_is_empty(self):
        secret_path = self.root / "vertex-project.json"
        secret_path.write_text(
            json.dumps({"type": "service_account", "project_id": "json-project"}),
            encoding="utf-8",
        )
        with (
            patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "", "GCLOUD_PROJECT": ""}),
            patch(
                "pandrator.web.generation_provider_preflight.resolve_provider_credential",
                return_value=ResolvedCredential(
                    value=secret_path.read_text(encoding="utf-8"), source="file"
                ),
            ),
        ):
            result = generation_provider_preflight(
                self.database,
                self._vertex_snapshot(secret_ref=f"file-path:{secret_path}"),
            )

        self.assertEqual("json-project", result["project"])
        self.assertEqual("credential", result["project_resolution"])
        self.assertEqual("file", result["credential_origin"])
        self.assertIs(result["credential_configured"], True)

    def test_adc_is_unknown_and_project_resolution_does_not_call_google_auth(self):
        with (
            patch.dict(
                os.environ,
                {
                    "GOOGLE_CLOUD_PROJECT": "",
                    "GCLOUD_PROJECT": "",
                    "VERTEX_SERVICE_ACCOUNT_JSON": "",
                },
            ),
            patch("pandrator.logic.tts_handler._vertex_access_token", side_effect=AssertionError("auth probe")),
            patch("requests.sessions.Session.request", side_effect=AssertionError("HTTP request")),
        ):
            cases = (
                ("", ResolvedCredential(source="none")),
                ("db:shared:vertex_ai", ResolvedCredential(source="none")),
                (
                    "env:VERTEX_SERVICE_ACCOUNT_JSON",
                    ResolvedCredential(
                        environment_variable="VERTEX_SERVICE_ACCOUNT_JSON",
                        source="environment",
                    ),
                ),
            )
            for secret_ref, credential in cases:
                with self.subTest(secret_ref=secret_ref), patch(
                    "pandrator.web.generation_provider_preflight.resolve_provider_credential",
                    return_value=credential,
                ):
                    result = generation_provider_preflight(
                        self.database, self._vertex_snapshot(secret_ref=secret_ref)
                    )

                self.assertEqual("application_default", result["credential_origin"])
                self.assertIsNone(result["credential_configured"])
                self.assertEqual("", result["project"])
                self.assertEqual("runtime", result["project_resolution"])
                self.assertFalse(result["access_checked"])

    def test_adc_uses_public_project_environment_without_access_check(self):
        with patch.dict(
            os.environ,
            {"GOOGLE_CLOUD_PROJECT": "public-project", "GCLOUD_PROJECT": "fallback-project"},
        ), patch(
            "pandrator.web.generation_provider_preflight.resolve_provider_credential",
            return_value=ResolvedCredential(source="none"),
        ):
            result = generation_provider_preflight(
                self.database, self._vertex_snapshot()
            )

        self.assertEqual("public-project", result["project"])
        self.assertEqual("environment", result["project_resolution"])
        self.assertEqual("application_default", result["credential_origin"])
        self.assertIsNone(result["credential_configured"])

    def test_invalid_vertex_json_returns_only_fixed_warning(self):
        secret_path = self.root / "invalid-credential.json"
        secret_path.write_text("NOT_JSON_SECRET_SENTINEL", encoding="utf-8")

        with patch(
            "pandrator.web.generation_provider_preflight.resolve_provider_credential",
            return_value=ResolvedCredential(
                value=secret_path.read_text(encoding="utf-8"), source="file"
            ),
        ):
            result = generation_provider_preflight(
                self.database,
                self._vertex_snapshot(secret_ref=f"file-path:{secret_path}"),
            )

        self.assertEqual(
            "Vertex credentials must be valid service-account JSON.",
            result["configuration_warning"],
        )
        self.assertNotIn("NOT_JSON_SECRET_SENTINEL", repr(result))
        self.assertNotIn(str(secret_path), repr(result))

    def test_structured_vertex_model_warns_for_non_global_location(self):
        with (
            patch(
                "pandrator.web.generation_provider_preflight.resolve_provider_credential",
                return_value=ResolvedCredential(source="none"),
            ),
            patch("pandrator.logic.tts_handler._vertex_access_token", side_effect=AssertionError("auth probe")),
        ):
            result = generation_provider_preflight(
                self.database,
                self._vertex_snapshot(location="us-central1"),
            )

        self.assertEqual(
            "Gemini 3.8 TTS on Vertex requires the global location.",
            result["configuration_warning"],
        )
        self.assertEqual("us-central1", result["location"])
        self.assertFalse(result["access_checked"])

    def test_gemini_api_key_is_redacted_without_treating_it_as_vertex_json(self):
        with (
            patch.dict(os.environ, {"PREFLIGHT_GEMINI_KEY_SENTINEL": "API_KEY_SENTINEL"}),
            patch(
                "pandrator.web.generation_provider_preflight.resolve_provider_credential",
                return_value=ResolvedCredential(
                    value="API_KEY_SENTINEL", source="environment"
                ),
            ),
        ):
            result = generation_provider_preflight(
                self.database,
                {
                    "tts": {
                        "service": "gemini",
                        "model": "gemini-3.8-flash-tts",
                        "voice": "Kore",
                        "provider_configs": [
                            {
                                "id": "gemini",
                                "secret_ref": "env:PREFLIGHT_GEMINI_KEY_SENTINEL",
                            }
                        ],
                    }
                },
            )

        self.assertEqual("gemini", result["route_id"])
        self.assertEqual("environment", result["credential_origin"])
        self.assertIs(result["credential_configured"], True)
        self.assertNotIn("API_KEY_SENTINEL", repr(result))
        self.assertNotIn("PREFLIGHT_GEMINI_KEY_SENTINEL", repr(result))
        self.assertNotIn("configuration_warning", result)

    def test_gemini_missing_credential_is_unconfigured_not_application_default(self):
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": ""}),
            patch(
                "pandrator.web.generation_provider_preflight.resolve_provider_credential",
                return_value=ResolvedCredential(
                    environment_variable="GEMINI_API_KEY", source="environment"
                ),
            ),
        ):
            result = generation_provider_preflight(self.database, {"tts": {"service": "gemini"}})

        self.assertEqual("unconfigured", result["credential_origin"])
        self.assertIs(result["credential_configured"], False)

    def test_resolver_failure_is_unavailable_without_environment_or_error_details(self):
        secret_ref = "keyring:Pandrator/vertex_ai"
        with (
            patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "must-not-be-used"}),
            patch(
                "pandrator.web.generation_provider_preflight.resolve_provider_credential",
                side_effect=RuntimeError("PRIVATE_PATH_SENTINEL"),
            ),
        ):
            result = generation_provider_preflight(
                self.database, self._vertex_snapshot(secret_ref=secret_ref)
            )

        self.assertEqual("unavailable", result["credential_origin"])
        self.assertIs(result["credential_configured"], False)
        self.assertEqual("", result["project"])
        self.assertEqual("unavailable", result["project_resolution"])
        self.assertNotIn(secret_ref, repr(result))
        self.assertNotIn("PRIVATE_PATH_SENTINEL", repr(result))


class GenerationProviderPreflightWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        bootstrap = BootstrapTokenStore()
        app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=bootstrap,
        )
        self.addCleanup(app.extensions["pandrator"]["database"].dispose)
        client = app.test_client()
        token = bootstrap.issue()
        csrf_token = client.post(
            "/api/v1/auth/bootstrap", json={"token": token}
        ).get_json()["csrf_token"]
        self.headers = {"X-CSRF-Token": csrf_token}
        self.client = client
        self.services = app.extensions["pandrator"]
        self.database = self.services["database"]
        self.generation = self.services["generation"]

    def test_preview_and_prepare_add_projection_without_mutating_snapshot_or_preview_state(self):
        created = self.client.post(
            "/api/v1/sessions",
            json={"name": "provider preflight", "workflow_kind": "audiobook"},
            headers=self.headers,
        )
        self.assertEqual(201, created.status_code, created.get_json())
        session_id = created.get_json()["id"]
        plan = self.generation.create_plan(
            session_id,
            source_revision_id=None,
            settings={},
            segments=[{"text": "Provider preflight test."}],
        )
        with self.database.session() as session:
            segment_id = session.scalar(
                select(GenerationSegment.id).where(
                    GenerationSegment.plan_revision_id == plan["active_revision_id"]
                )
            )
            jobs_before = session.scalar(select(func.count()).select_from(Job))
            runs_before = session.scalar(
                select(func.count()).select_from(GenerationRun)
            )

        run_override = {"tts": {"service": "xtts"}}
        preview = self.generation.preview_selection(
            session_id, run_override=run_override
        )

        self.assertEqual("xtts", preview["provider_preflight"]["route_id"])
        self.assertFalse(preview["provider_preflight"]["access_checked"])
        with self.database.session() as session:
            self.assertEqual(jobs_before, session.scalar(select(func.count()).select_from(Job)))
            self.assertEqual(
                runs_before,
                session.scalar(select(func.count()).select_from(GenerationRun)),
            )

        prepared = self.generation.prepare_start(
            session_id, run_override=run_override, segment_ids=[segment_id]
        )

        self.assertEqual("xtts", prepared["provider_preflight"]["route_id"])
        self.assertEqual([segment_id], prepared["requested_segment_ids"])
        self.assertNotIn("provider_preflight", prepared["frozen_snapshot"])

    def test_preview_reports_configuration_warning_and_prepare_rejects_before_queueing(self):
        created = self.client.post(
            "/api/v1/sessions",
            json={"name": "provider preflight warning", "workflow_kind": "audiobook"},
            headers=self.headers,
        )
        self.assertEqual(201, created.status_code, created.get_json())
        session_id = created.get_json()["id"]
        plan = self.generation.create_plan(
            session_id,
            source_revision_id=None,
            settings={},
            segments=[{"text": "Provider warning test."}],
        )
        with self.database.session() as session:
            segment_id = session.scalar(
                select(GenerationSegment.id).where(
                    GenerationSegment.plan_revision_id == plan["active_revision_id"]
                )
            )
        run_override = {
            "tts": {
                "service": "vertex_ai",
                "model": "gemini-3.8-flash-tts",
                "xtts_model": "gemini-3.8-flash-tts",
                "provider_configs": [
                    {"id": "vertex_ai", "vertex_location": "us-central1"}
                ],
            }
        }

        preview = self.generation.preview_selection(
            session_id, run_override=run_override
        )

        warning = "Gemini 3.8 TTS on Vertex requires the global location."
        self.assertEqual(warning, preview["provider_preflight"]["configuration_warning"])
        with self.assertRaisesRegex(ValueError, warning):
            self.generation.prepare_start(
                session_id,
                run_override=run_override,
                segment_ids=[segment_id],
            )
        with self.database.session() as session:
            self.assertEqual(
                0,
                session.scalar(
                    select(func.count()).select_from(GenerationRun).where(
                        GenerationRun.session_id == session_id
                    )
                ),
            )


if __name__ == "__main__":
    unittest.main()
