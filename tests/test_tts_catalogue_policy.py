import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator_mcp.schemas import TtsCatalogInput
from pandrator_mcp.tools.e2e import tts_catalog
from tests.web_test_support import prepare_web_test_data_root


class _McpCatalogueApplication:
    def __init__(self, payload):
        self.payload = payload

    def tts_catalog(self, *, refresh):
        del refresh
        return self.payload

    def list_voices(self):
        return {"items": []}


def _mcp_runtime(payload):
    application = _McpCatalogueApplication(payload)
    return SimpleNamespace(require_application=lambda: application)


class TtsCataloguePolicyTests(unittest.TestCase):
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

    def test_http_catalogue_policy_is_authoritative_and_preserves_default(self):
        service_definitions = [
            {
                "id": "audio_cpp",
                "name": "Audio C++",
                "models": [],
                "catalogue_role": "compatibility",
                "replacement_service_id": "evil",
                "replacement_model_family": "evil",
            },
            {
                "id": "kobold_qwen",
                "name": "Kobold Qwen",
                "models": [],
                "catalogue_role": "primary",
                "replacement_service_id": "evil",
                "replacement_model_family": "evil",
            },
            {
                "id": "fishs2",
                "name": "Fish S2",
                "models": [],
                "catalogue_role": "primary",
            },
            {
                "id": "voxcpm",
                "name": "VoxCPM",
                "models": [],
                "catalogue_role": "primary",
            },
            {
                "id": "chatterbox",
                "name": "Chatterbox",
                "models": [],
                "catalogue_role": "primary",
            },
            {
                "id": "magpie",
                "name": "Magpie",
                "models": [],
                "catalogue_role": "primary",
            },
            {
                "id": "openai",
                "name": "OpenAI",
                "kind": "commercial",
                "models": [],
                "catalogue_role": "compatibility",
                "replacement_service_id": "evil",
            },
            {
                "id": "custom_cloud",
                "name": "Custom cloud",
                "kind": "commercial",
                "is_custom": True,
                "models": [],
                "catalogue_role": "compatibility",
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
                return_value=(
                    {},
                    0,
                    {"service": "legacy-default"},
                    0,
                ),
            ),
        ):
            response = self.client.get("/api/v1/services/tts")

        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        services = {item["id"]: item for item in payload["services"]}
        self.assertEqual("audio_cpp", payload["recommended_service"])
        self.assertEqual("legacy-default", payload["default_service"])
        self.assertEqual(
            {
                "catalogue_role": "primary",
                "replacement_service_id": None,
                "replacement_model_family": None,
            },
            {
                key: services["audio_cpp"][key]
                for key in (
                    "catalogue_role",
                    "replacement_service_id",
                    "replacement_model_family",
                )
            },
        )
        compatibility_replacements = {
            "kobold_qwen": "qwen3_tts",
            "fishs2": "fish_audio_s2",
            "voxcpm": "voxcpm2",
            "chatterbox": "chatterbox",
            "magpie": "magpie_tts",
        }
        for service_id, model_family in compatibility_replacements.items():
            self.assertEqual("compatibility", services[service_id]["catalogue_role"])
            self.assertEqual(
                "audio_cpp", services[service_id]["replacement_service_id"]
            )
            self.assertEqual(
                model_family,
                services[service_id]["replacement_model_family"],
            )
        for service_id in ("openai", "custom_cloud"):
            self.assertEqual("external", services[service_id]["catalogue_role"])
            self.assertIsNone(services[service_id]["replacement_service_id"])
            self.assertIsNone(services[service_id]["replacement_model_family"])

    def test_mcp_catalogue_filters_compatibility_but_allows_explicit_selection(self):
        payload = {
            "default_service": "legacy-provider",
            "recommended_service": "audio_cpp",
            "revision": 3,
            "services": [
                {
                    "id": "audio_cpp",
                    "name": "Audio C++",
                    "catalogue_role": "primary",
                    "models": ["cpp-model"],
                },
                {
                    "id": "kobold_qwen",
                    "name": "Kobold Qwen",
                    "catalogue_role": "compatibility",
                    "replacement_service_id": "audio_cpp",
                    "replacement_model_family": "qwen3_tts",
                    "models": ["qwen-model"],
                    "model_catalog": [{"id": "qwen-model", "family": "qwen3_tts"}],
                    "model_voice_modes": {"qwen-model": ["prebuilt"]},
                },
                {
                    "id": "legacy-provider",
                    "name": "Legacy provider",
                    "models": ["legacy-model"],
                },
            ],
        }
        runtime = _mcp_runtime(payload)

        ordinary = tts_catalog(runtime, TtsCatalogInput())
        self.assertEqual(
            {"audio_cpp", "legacy-provider"},
            {service["id"] for service in ordinary["services"]},
        )
        self.assertEqual(
            "external",
            next(
                service["catalogue_role"]
                for service in ordinary["services"]
                if service["id"] == "legacy-provider"
            ),
        )
        self.assertEqual("legacy-provider", ordinary["default_service"])
        self.assertEqual("audio_cpp", ordinary["recommended_service"])

        query_only = tts_catalog(runtime, TtsCatalogInput(query="qwen"))
        self.assertEqual([], query_only["services"])

        explicit = tts_catalog(
            runtime,
            TtsCatalogInput(service_id="KOBOLD-QWEN", detail="full"),
        )
        self.assertEqual(["kobold_qwen"], [item["id"] for item in explicit["services"]])
        self.assertEqual("compatibility", explicit["services"][0]["catalogue_role"])
        self.assertEqual(
            "qwen3_tts",
            explicit["services"][0]["replacement_model_family"],
        )
        self.assertEqual(
            [{"id": "qwen-model", "family": "qwen3_tts"}],
            explicit["services"][0]["model_catalog"],
        )
        self.assertEqual(
            {"qwen-model": ["prebuilt"]},
            explicit["services"][0]["model_voice_modes"],
        )

        included = tts_catalog(
            runtime,
            TtsCatalogInput(include_compatibility=True),
        )
        self.assertEqual(
            {"audio_cpp", "kobold_qwen", "legacy-provider"},
            {service["id"] for service in included["services"]},
        )

    def test_tts_catalog_input_excludes_compatibility_by_default(self):
        self.assertFalse(TtsCatalogInput().include_compatibility)


if __name__ == "__main__":
    unittest.main()
