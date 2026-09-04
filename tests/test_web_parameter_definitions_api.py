import json
import tempfile
import unittest

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from tests.web_test_support import prepare_web_test_data_root


class ParameterDefinitionsApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        prepare_web_test_data_root(self.temporary.name)
        self.bootstrap = BootstrapTokenStore()
        self.token = self.bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=self.bootstrap,
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.extensions["pandrator"]["database"].dispose()
        self.temporary.cleanup()

    def authenticate(self):
        response = self.client.post(
            "/api/v1/auth/bootstrap", json={"token": self.token}
        )
        self.assertEqual(200, response.status_code)
        return response.get_json()["csrf_token"]

    def test_endpoint_requires_authentication(self):
        response = self.client.get("/api/v1/parameter-definitions?section=tts")

        self.assertEqual(401, response.status_code)
        self.assertEqual(
            "authentication_required", response.get_json()["error"]["code"]
        )

    def test_single_and_repeated_filters_return_stable_named_results(self):
        self.authenticate()
        response = self.client.get(
            "/api/v1/parameter-definitions",
            query_string=[
                ("section", "output"),
                ("section", "tts"),
                ("name", "language"),
                ("name", "voice"),
            ],
        )

        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertEqual(
            [("tts", "language"), ("tts", "voice"), ("output", "language")],
            [(item["section"], item["name"]) for item in payload["items"]],
        )

        exact = self.client.get(
            "/api/v1/parameter-definitions",
            query_string={"section": "tts", "name": "temperature"},
        )
        self.assertEqual(200, exact.status_code)
        exact_item = exact.get_json()["items"]
        self.assertEqual(1, len(exact_item))
        self.assertEqual("tts", exact_item[0]["section"])
        self.assertEqual("temperature", exact_item[0]["name"])

    def test_workflow_and_query_filters_intersect(self):
        self.authenticate()
        response = self.client.get(
            "/api/v1/parameter-definitions",
            query_string={
                "workflow_kind": "subtitles",
                "query": "local",
            },
        )

        self.assertEqual(200, response.status_code)
        items = response.get_json()["items"]
        self.assertTrue(items)
        self.assertTrue(all(item["section"] in {"stt", "subtitles"} for item in items))
        self.assertTrue(
            all(
                "local"
                in " ".join(
                    str(item.get(field, ""))
                    for field in (
                        "section",
                        "name",
                        "label",
                        "description",
                        "applicability",
                        "caveat",
                    )
                ).casefold()
                for item in items
            )
        )

    def test_no_filter_blank_filter_and_invalid_values_are_validation_errors(self):
        self.authenticate()
        query_strings = (
            {},
            {"section": ""},
            {"query": "   "},
            {"section": "unknown"},
            {"workflow_kind": "unknown", "query": "speech"},
            {"limit": "not-an-integer", "section": "tts"},
            {"limit": "0", "section": "tts"},
            {"limit": "301", "section": "tts"},
        )

        for query_string in query_strings:
            with self.subTest(query_string=query_string):
                response = self.client.get(
                    "/api/v1/parameter-definitions", query_string=query_string
                )
                self.assertEqual(422, response.status_code)
                self.assertEqual(
                    "validation_error", response.get_json()["error"]["code"]
                )

    def test_limit_reports_counts_and_truncation(self):
        self.authenticate()
        response = self.client.get(
            "/api/v1/parameter-definitions",
            query_string={"section": "tts", "limit": "2"},
        )

        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertEqual(2, payload["returned_count"])
        self.assertEqual(80, payload["matched_count"])
        self.assertTrue(payload["truncated"])
        self.assertEqual(2, len(payload["items"]))

    def test_response_shape_defaults_and_json_serialization(self):
        self.authenticate()
        response = self.client.get(
            "/api/v1/parameter-definitions",
            query_string={"section": "stt", "name": "stt_engine"},
        )

        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertEqual(1, payload["schema_version"])
        self.assertEqual(
            [
                "text",
                "stt",
                "subtitles",
                "correction",
                "translation",
                "tts",
                "audio",
                "rvc",
                "source_cleaning",
                "output",
            ],
            payload["available_sections"],
        )
        item = payload["items"][0]
        self.assertTrue(
            {
                "section",
                "name",
                "label",
                "description",
                "default",
                "value_type",
            }.issubset(item)
        )
        self.assertEqual("stt", item["section"])
        self.assertEqual("stt_engine", item["name"])
        self.assertEqual("string", item["value_type"])
        self.assertEqual("whisper", item["default"])
        json.dumps(payload)

    def test_openapi_declares_operation_parameters_schema_and_security(self):
        document = self.client.get("/api/v1/openapi.json").get_json()
        operation = document["paths"]["/api/v1/parameter-definitions"]["get"]

        self.assertEqual("getParameterDefinitions", operation["operationId"])
        self.assertEqual(
            {
                "cookieAuth": [],
            },
            operation["security"][0],
        )
        self.assertIn({"nativeOAuth": ["app.read"]}, operation["security"])
        parameters = {
            parameter["name"]: parameter for parameter in operation["parameters"]
        }
        for name in ("section", "name"):
            parameter = parameters[name]
            self.assertEqual("query", parameter["in"])
            self.assertEqual("array", parameter["schema"]["type"])
            self.assertEqual("form", parameter["style"])
            self.assertTrue(parameter["explode"])
        self.assertEqual(
            ["audiobook", "subtitles", "voiceover"],
            parameters["workflow_kind"]["schema"]["enum"],
        )
        self.assertEqual(1, parameters["limit"]["schema"]["minimum"])
        self.assertEqual(300, parameters["limit"]["schema"]["maximum"])
        self.assertEqual(100, parameters["limit"]["schema"]["default"])

        schemas = document["components"]["schemas"]
        item_schema = schemas["ParameterDefinition"]
        response_schema = schemas["ParameterDefinitionsResponse"]
        self.assertEqual(
            {
                "section",
                "name",
                "label",
                "description",
                "default",
                "value_type",
            },
            set(item_schema["required"]),
        )
        self.assertEqual(1, response_schema["properties"]["schema_version"]["const"])
        self.assertEqual(
            "#/components/schemas/ParameterDefinition",
            response_schema["properties"]["items"]["items"]["$ref"],
        )


if __name__ == "__main__":
    unittest.main()
