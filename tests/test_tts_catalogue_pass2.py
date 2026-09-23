"""Second-pass TTS catalogue tests: compact view and selected detail."""

import json
import tempfile
import unittest
from unittest import mock

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.openapi import build_openapi_document
from pandrator.web.tts_providers import TtsHealth
from tests.web_test_support import prepare_web_test_data_root

HEAVY_MODEL_KEYS = frozenset({
    "catalogue_info",
    "request_parameters",
    "pandrator_features",
    "description",
    "upstream_features",
    "upstream_status",
    "sources",
    "repository_license",
    "package_availability",
    "reference_audio",
    "reference_text",
    "tasks",
    "verified_runtime",
    "expressive_capabilities",
})


class TtsCataloguePass2Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        prepare_web_test_data_root(self.temporary.name)
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=bootstrap,
        )
        self.client = self.app.test_client()
        self.client.post("/api/v1/auth/bootstrap", json={"token": token})

    def tearDown(self):
        self.app.extensions["pandrator"]["database"].dispose()
        self.temporary.cleanup()

    def _payload(self, response):
        self.assertEqual(200, response.status_code)
        return response.get_json()

    def test_default_full_shape_is_preserved(self):
        payload = self._payload(self.client.get("/api/v1/services/tts"))
        self.assertNotIn("view", payload)
        for key in (
            "value",
            "revision",
            "default_value",
            "recommended_service",
            "default_service",
            "default_revision",
            "builtin_defaults",
            "services",
            "profiles",
            "previews",
            "manager",
        ):
            self.assertIn(key, payload)
        audio_cpp = next(
            item for item in payload["services"] if item["id"] == "audio_cpp"
        )
        self.assertGreater(len(audio_cpp["model_catalog"]), 100)
        self.assertIn("catalogue_info", audio_cpp["model_catalog"][0])
        self.assertGreater(len(payload["profiles"]), 10)

    def test_compact_view_is_slim_but_complete_for_choosers(self):
        full = self._payload(self.client.get("/api/v1/services/tts"))
        compact = self._payload(
            self.client.get("/api/v1/services/tts?view=compact")
        )
        self.assertEqual("compact", compact["view"])
        for key in ("profiles", "value", "default_value", "builtin_defaults", "previews"):
            self.assertNotIn(key, compact)
        self.assertEqual(full["revision"], compact["revision"])
        self.assertEqual(full["default_service"], compact["default_service"])
        self.assertEqual(
            full["recommended_service"], compact["recommended_service"]
        )
        full_services = {item["id"]: item for item in full["services"]}
        compact_services = {item["id"]: item for item in compact["services"]}
        self.assertEqual(set(full_services), set(compact_services))
        for service_id, slim in compact_services.items():
            original = full_services[service_id]
            # Projection equivalence: every retained key matches full.
            for key, value in slim.items():
                if key == "model_catalog":
                    continue
                self.assertEqual(
                    original.get(key), value, f"{service_id}.{key}"
                )
            # Chooser fields survive on every row exactly as in full: rows
            # without a catalogue (e.g. openai) stay without one, and the
            # consumers' `?? []` fallbacks keep working.
            self.assertIn("id", slim)
            self.assertIn("name", slim)
            for key in (
                "models",
                "default_model",
                "voices",
                "voice_catalogues",
                "model_voice_modes",
                "model_catalog",
                "voice_metadata",
                "default_voice",
                "default_voices",
                "default_voices_by_language",
            ):
                if key == "model_catalog":
                    continue
                self.assertEqual(
                    key in original, key in slim, f"{service_id}.{key} parity"
                )
            # No heavy detail anywhere in the slim rows. voice_metadata and
            # the slim model_catalog are retained verbatim for describeVoice,
            # languagesForService and modelChoices; everything else heavy
            # stays on the full/detail views.
            for key in (
                "expressive_capabilities",
                "request_fields",
                "request_defaults",
                "settings",
                "pricing",
            ):
                self.assertNotIn(key, slim, f"{service_id} leaks {key}")
        audio_cpp = compact_services["audio_cpp"]
        full_audio = full_services["audio_cpp"]
        slim_catalog = audio_cpp["model_catalog"]
        self.assertEqual(len(full_audio["model_catalog"]), len(slim_catalog))
        self.assertEqual(
            {item["id"] for item in full_audio["model_catalog"]}
            | set(full_audio["models"])
            | {full_audio["default_model"]},
            {item["id"] for item in slim_catalog},
        )
        for entry in slim_catalog:
            self.assertTrue(
                HEAVY_MODEL_KEYS.isdisjoint(entry),
                f"heavy keys in {entry.get('id')}",
            )
            expected_mode = full_audio["model_voice_modes"].get(entry["id"])
            if expected_mode:
                self.assertEqual(expected_mode, entry.get("voice_mode"))
        # Substantial payload reduction on the realistic default catalogue.
        full_bytes = len(json.dumps(full).encode())
        compact_bytes = len(json.dumps(compact).encode())
        self.assertLess(compact_bytes, full_bytes / 4)

    def test_compact_view_validation(self):
        response = self.client.get("/api/v1/services/tts?view=tiny")
        self.assertEqual(422, response.status_code)
        response = self.client.get(
            "/api/v1/services/tts?service_id=xtts&services=xtts"
        )
        self.assertEqual(422, response.status_code)
        response = self.client.get(
            "/api/v1/services/tts?services=" + ",".join(f"s{i}" for i in range(21))
        )
        self.assertEqual(422, response.status_code)
        response = self.client.get("/api/v1/services/tts?service_id=" + "x" * 65)
        self.assertEqual(422, response.status_code)
        response = self.client.get("/api/v1/services/tts?service_id=nope")
        self.assertEqual(404, response.status_code)
        self.assertEqual("tts_service_not_found", response.get_json()["error"]["code"])

    def test_service_filter_selects_before_refresh(self):
        payload = self._payload(
            self.client.get("/api/v1/services/tts?service_id=audio.cpp")
        )
        self.assertEqual(["audio_cpp"], [item["id"] for item in payload["services"]])
        payload = self._payload(
            self.client.get("/api/v1/services/tts?services=xtts,kobold_qwen")
        )
        self.assertEqual(
            {"xtts", "kobold_qwen"},
            {item["id"] for item in payload["services"]},
        )
        # Successive selections are stateless: no cross-request leakage.
        payload = self._payload(
            self.client.get("/api/v1/services/tts?service_id=xtts")
        )
        self.assertEqual(["xtts"], [item["id"] for item in payload["services"]])

    def test_refresh_probes_only_selected_services(self):
        catalogue_service = self.app.extensions["pandrator"]["tts_catalogue"]
        seen: list[str] = []

        def fake_enrich(service, *, api_key=""):
            del api_key
            seen.append(str(service.get("id")))
            return {"models": ["live-model"]}

        with (
            mock.patch.object(
                catalogue_service.providers,
                "health",
                return_value=TtsHealth(True, True, ""),
            ),
            mock.patch.object(
                catalogue_service.providers,
                "enrich_catalog",
                side_effect=fake_enrich,
            ),
        ):
            payload = self._payload(
                self.client.get(
                    "/api/v1/services/tts?refresh=true&services=xtts,kobold_qwen"
                )
            )
        self.assertEqual({"xtts", "kobold_qwen"}, set(seen))
        services = {item["id"]: item for item in payload["services"]}
        self.assertTrue(services["xtts"]["available"])
        self.assertIn("live-model", services["xtts"]["models"])

    def test_compact_refresh_preserves_provider_errors(self):
        catalogue_service = self.app.extensions["pandrator"]["tts_catalogue"]
        with mock.patch.object(
            catalogue_service.providers,
            "health",
            return_value=TtsHealth(False, False, "Service is not running"),
        ):
            payload = self._payload(
                self.client.get("/api/v1/services/tts?view=compact&refresh=true")
            )
        audio_cpp = next(
            item for item in payload["services"] if item["id"] == "audio_cpp"
        )
        self.assertFalse(audio_cpp["available"])
        self.assertEqual("Service is not running", audio_cpp["availability_reason"])

    def test_service_detail_matches_full_entry(self):
        full = self._payload(self.client.get("/api/v1/services/tts"))
        detail = self._payload(self.client.get("/api/v1/services/tts/XTts"))
        self.assertEqual("detail", detail["view"])
        self.assertEqual(full["revision"], detail["revision"])
        expected = next(item for item in full["services"] if item["id"] == "xtts")
        self.assertEqual(expected, detail["service"])
        self.assertNotIn("selected_models", detail)

    def test_service_detail_model_filter(self):
        detail = self._payload(
            self.client.get(
                "/api/v1/services/tts/audio_cpp?model=qwen3_tts_1_7b_base_q8_0"
            )
        )
        self.assertEqual(
            ["qwen3_tts_1_7b_base_q8_0"], detail["selected_models"]
        )
        service = detail["service"]
        self.assertEqual(
            ["qwen3_tts_1_7b_base_q8_0"],
            [item["id"] for item in service["model_catalog"]],
        )
        self.assertIn("catalogue_info", service["model_catalog"][0])
        # Id lists and defaults stay whole as chooser context.
        full = self._payload(self.client.get("/api/v1/services/tts"))
        full_audio = next(
            item for item in full["services"] if item["id"] == "audio_cpp"
        )
        self.assertEqual(full_audio["models"], service["models"])
        self.assertEqual(full_audio["default_model"], service["default_model"])
        for key in (
            "voice_catalogues",
            "voice_metadata",
            "model_voice_modes",
            "default_voices",
            "generation_prompt_models",
        ):
            value = service.get(key)
            if isinstance(value, dict):
                self.assertTrue(
                    set(value) <= {"qwen3_tts_1_7b_base_q8_0"},
                    f"{key} not filtered",
                )
        # Combined compact + selected-model payload stays small.
        compact = self._payload(
            self.client.get("/api/v1/services/tts?view=compact")
        )
        combined = len(json.dumps(compact).encode()) + len(
            json.dumps(detail).encode()
        )
        self.assertLess(combined, len(json.dumps(full).encode()) / 4)

    def test_service_detail_model_validation(self):
        response = self.client.get("/api/v1/services/tts/xtts?model=a&models=b")
        self.assertEqual(422, response.status_code)
        response = self.client.get("/api/v1/services/tts/xtts?model=nope")
        self.assertEqual(404, response.status_code)
        self.assertEqual("tts_model_not_found", response.get_json()["error"]["code"])
        response = self.client.get("/api/v1/services/tts/nope")
        self.assertEqual(404, response.status_code)
        multi = self._payload(
            self.client.get(
                "/api/v1/services/tts/audio_cpp"
                "?models=qwen3_tts_1_7b_base_q8_0,fish_audio_s2_pro_q8_0"
            )
        )
        self.assertEqual(
            {"qwen3_tts_1_7b_base_q8_0", "fish_audio_s2_pro_q8_0"},
            {item["id"] for item in multi["service"]["model_catalog"]},
        )

    def test_catalogue_requires_auth(self):
        anonymous = self.app.test_client()
        self.assertEqual(401, anonymous.get("/api/v1/services/tts").status_code)
        self.assertEqual(
            401, anonymous.get("/api/v1/services/tts/xtts").status_code
        )

    def test_slim_model_catalog_honours_configured_overrides(self):
        service_definitions = [
            {
                "id": "audio_cpp",
                "name": "Audio C++",
                "adapter": "audio_cpp",
                "models": ["qwen3_tts_1_7b_base_q8_0", "my_custom_model"],
                "default_model": "my_custom_model",
                "model_catalog": [
                    {
                        "id": "qwen3_tts_1_7b_base_q8_0",
                        "label": "Custom Label",
                        "supported_languages": ["en", "xx"],
                        "voice_mode": "design",
                        "catalogue_info": {"note": "heavy detail"},
                        "request_parameters": {"speed": {"type": "number"}},
                    },
                    {
                        "id": "my_custom_model",
                        "label": "Mine",
                        "family": "custom_fam",
                    },
                ],
                "model_voice_modes": {"my_custom_model": "cloning"},
                "voices": ["v1"],
                "voice_catalogues": {"my_custom_model": ["v1"]},
            },
            {
                "id": "custom_cloud",
                "name": "Custom cloud",
                "adapter": "openai_compatible",
                "kind": "commercial",
                "is_custom": True,
                "models": ["pro-model"],
                "default_model": "pro-model",
                "model_catalog": [
                    {
                        "id": "pro-model",
                        "label": "Pro Model Label",
                        "family": "pro_fam",
                        "supported_languages": ["en", "pl"],
                    }
                ],
                "voices": ["v1"],
            },
        ]
        catalogue_service = self.app.extensions["pandrator"]["tts_catalogue"]
        with (
            mock.patch(
                "pandrator.web.tts_providers.tts_handler.get_service_configs",
                return_value=service_definitions,
            ),
            mock.patch.object(
                catalogue_service,
                "_settings",
                return_value=({}, 0, {"service": "audio_cpp"}, 0),
            ),
        ):
            full = self._payload(self.client.get("/api/v1/services/tts"))
            compact = self._payload(
                self.client.get("/api/v1/services/tts?view=compact")
            )
        full_services = {item["id"]: item for item in full["services"]}
        slim_services = {item["id"]: item for item in compact["services"]}

        # Same precedence as the full builder: the configured record wins
        # over static builtins, restricted to the retained scalar fields.
        slim_audio = {
            item["id"]: item
            for item in slim_services["audio_cpp"]["model_catalog"]
        }
        full_audio = {
            item["id"]: item
            for item in full_services["audio_cpp"]["model_catalog"]
        }
        builtin_override = slim_audio["qwen3_tts_1_7b_base_q8_0"]
        self.assertEqual("Custom Label", builtin_override["label"])
        self.assertEqual(["en", "xx"], builtin_override["supported_languages"])
        self.assertEqual("design", builtin_override["voice_mode"])
        for key in ("catalogue_info", "request_parameters"):
            self.assertNotIn(key, builtin_override)
            self.assertIn(key, full_audio["qwen3_tts_1_7b_base_q8_0"])
        # Every retained slim field matches the full record, falling back to
        # the full service's own model_voice_modes map exactly as the slim
        # builder does (record > builtin > service map).
        full_modes = full_services["audio_cpp"].get("model_voice_modes") or {}
        for model_id, slim_entry in slim_audio.items():
            full_entry = full_audio[model_id]
            for key, value in slim_entry.items():
                expected = full_entry.get(key)
                if expected is None and key == "voice_mode":
                    expected = full_modes.get(model_id)
                self.assertEqual(expected, value, f"{model_id}.{key}")
        custom_entry = slim_audio["my_custom_model"]
        self.assertEqual("Mine", custom_entry["label"])
        self.assertEqual("custom_fam", custom_entry["family"])
        self.assertEqual("cloning", custom_entry["voice_mode"])

        # Non-audio.cpp custom providers keep their own record metadata.
        slim_custom = slim_services["custom_cloud"]["model_catalog"][0]
        self.assertEqual("pro-model", slim_custom["id"])
        self.assertEqual("Pro Model Label", slim_custom["label"])
        self.assertEqual("pro_fam", slim_custom["family"])
        self.assertEqual(["en", "pl"], slim_custom["supported_languages"])

    def test_openapi_documents_catalogue_views(self):
        document = build_openapi_document()
        collection = document["paths"]["/api/v1/services/tts"]["get"]
        names = [item["name"] for item in collection["parameters"]]
        self.assertIn("view", names)
        self.assertIn("service_id", names)
        self.assertIn("services", names)
        detail = document["paths"]["/api/v1/services/tts/{serviceId}"]["get"]
        self.assertEqual("getTtsServiceDetail", detail["operationId"])
        detail_names = [item["name"] for item in detail["parameters"]]
        self.assertIn("model", detail_names)
        self.assertIn("models", detail_names)


if __name__ == "__main__":
    unittest.main()
