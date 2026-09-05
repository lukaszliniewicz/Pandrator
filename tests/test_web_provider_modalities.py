import tempfile
import unittest
from pathlib import Path
from unittest import mock

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from pandrator.logic.llm_handler import ModelDiscoveryResult
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.provider_settings import build_llm_settings
from tests.web_test_support import prepare_web_test_data_root


class ProviderModelModalitiesApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        prepare_web_test_data_root(self.temporary.name)
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name, testing=True, bootstrap_tokens=bootstrap
        )
        self.client = self.app.test_client()
        self.csrf = self.client.post(
            "/api/v1/auth/bootstrap", json={"token": token}
        ).get_json()["csrf_token"]
        self.headers = {"X-CSRF-Token": self.csrf}

    def tearDown(self):
        self.app.extensions["pandrator"]["database"].dispose()
        self.temporary.cleanup()

    def _provider(self):
        return self.client.post(
            "/api/v1/providers",
            json={"provider_key": "openai", "label": "OpenAI"},
            headers=self.headers,
        ).get_json()

    def _create_model(self, provider, **payload):
        response = self.client.post(
            f"/api/v1/providers/{provider['id']}/models",
            json={"model_id": "model", **payload},
            headers=self.headers,
        )
        self.assertEqual(201, response.status_code, response.get_json())
        return response.get_json()

    def test_create_list_and_settings_round_trip_audio_input(self):
        provider = self._provider()
        created = self._create_model(
            provider,
            is_default=True,
            input_modalities=["TEXT", " audio "],
            output_modalities=["audio"],
        )

        self.assertEqual(["text", "audio"], created["input_modalities"])
        self.assertEqual(["audio"], created["output_modalities"])
        self.assertTrue(created["supports_audio_input"])
        self.assertNotIn("input_modalities_json", created)
        self.assertNotIn("output_modalities_json", created)

        listed = self.client.get(
            f"/api/v1/providers/{provider['id']}/models"
        ).get_json()["items"]
        self.assertEqual(created["input_modalities"], listed[0]["input_modalities"])
        self.assertEqual(created["output_modalities"], listed[0]["output_modalities"])
        self.assertTrue(listed[0]["supports_audio_input"])

        extension = self.app.extensions["pandrator"]
        settings, _ = build_llm_settings(extension["database"], extension["paths"])
        record = settings.provider_configs[0]["models"][0]
        self.assertEqual(["text", "audio"], record["input_modalities"])
        self.assertEqual(["audio"], record["output_modalities"])
        self.assertTrue(record["supports_audio_input"])

    def test_update_modalities_honors_revision_and_recomputes_audio_support(self):
        provider = self._provider()
        created = self._create_model(provider)
        response = self.client.patch(
            f"/api/v1/providers/{provider['id']}/models/{created['id']}",
            json={
                "input_modalities": ["IMAGE", "audio"],
                "output_modalities": ["TEXT"],
            },
            headers={
                **self.headers,
                "If-Match": f'"{created["revision"]}"',
            },
        )
        self.assertEqual(200, response.status_code, response.get_json())
        updated = response.get_json()
        self.assertEqual(created["revision"] + 1, updated["revision"])
        self.assertEqual(["image", "audio"], updated["input_modalities"])
        self.assertEqual(["text"], updated["output_modalities"])
        self.assertTrue(updated["supports_audio_input"])

    def test_invalid_duplicate_and_empty_modalities_are_rejected(self):
        provider = self._provider()
        invalid_payloads = (
            {"input_modalities": ["text", "TEXT"]},
            {"input_modalities": []},
            {"output_modalities": ["video"]},
        )
        for index, payload in enumerate(invalid_payloads):
            response = self.client.post(
                f"/api/v1/providers/{provider['id']}/models",
                json={"model_id": f"invalid-{index}", **payload},
                headers=self.headers,
            )
            self.assertEqual(422, response.status_code, response.get_json())

    def test_refresh_preserves_manual_modalities_and_defaults_discovered_models(self):
        provider = self._provider()
        manual = self._create_model(
            provider,
            model_id="manual",
            is_default=True,
            input_modalities=["audio"],
            output_modalities=["image"],
        )
        with mock.patch(
            "pandrator.logic.llm_handler.discover_provider_models",
            return_value=ModelDiscoveryResult(
                models=("manual", "discovered"), source="endpoint"
            ),
        ):
            response = self.client.post(
                f"/api/v1/providers/{provider['id']}/models/refresh",
                json={},
                headers=self.headers,
            )
        self.assertEqual(200, response.status_code, response.get_json())
        records = self.client.get(
            f"/api/v1/providers/{provider['id']}/models"
        ).get_json()["items"]
        preserved = next(item for item in records if item["id"] == manual["id"])
        discovered = next(item for item in records if item["model_id"] == "discovered")
        self.assertEqual(["audio"], preserved["input_modalities"])
        self.assertEqual(["image"], preserved["output_modalities"])
        self.assertTrue(preserved["supports_audio_input"])
        self.assertEqual(["text"], discovered["input_modalities"])
        self.assertEqual(["text"], discovered["output_modalities"])
        self.assertFalse(discovered["supports_audio_input"])


class ProviderModelModalitiesMigrationTests(unittest.TestCase):
    def test_upgrade_backfills_existing_rows_and_downgrade_is_guarded(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "migration.sqlite3"
            engine = create_engine(f"sqlite:///{database_path}")
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        CREATE TABLE provider_models (
                            id VARCHAR(36) PRIMARY KEY,
                            provider_id VARCHAR(36) NOT NULL,
                            model_id VARCHAR(255) NOT NULL,
                            is_active BOOLEAN NOT NULL DEFAULT 0,
                            is_default BOOLEAN NOT NULL DEFAULT 0,
                            default_temperature FLOAT,
                            default_reasoning_effort VARCHAR(80),
                            input_cost_per_million FLOAT,
                            cached_input_cost_per_million FLOAT,
                            output_cost_per_million FLOAT,
                            context_window_tokens INTEGER NOT NULL DEFAULT 262144,
                            max_output_tokens INTEGER,
                            options_json JSON NOT NULL,
                            revision INTEGER NOT NULL DEFAULT 1,
                            created_at DATETIME NOT NULL,
                            updated_at DATETIME NOT NULL,
                            CONSTRAINT uq_provider_model UNIQUE (provider_id, model_id)
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO provider_models "
                        "(id, provider_id, model_id, options_json, created_at, updated_at) "
                        "VALUES ('m', 'p', 'legacy', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )
                connection.execute(
                    text(
                        "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO alembic_version VALUES ('0036_subtitle_evidence')"
                    )
                )

            config = Config()
            config.set_main_option(
                "script_location", str(Path("pandrator/web/migrations").resolve())
            )
            config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
            command.upgrade(config, "0037_provider_model_modalities")

            columns = {
                column["name"]: column
                for column in inspect(engine).get_columns("provider_models")
            }
            self.assertFalse(columns["input_modalities_json"]["nullable"])
            self.assertFalse(columns["output_modalities_json"]["nullable"])
            with engine.connect() as connection:
                row = connection.execute(
                    text(
                        "SELECT input_modalities_json, output_modalities_json "
                        "FROM provider_models"
                    )
                ).one()
            self.assertEqual('["text"]', row[0])
            self.assertEqual('["text"]', row[1])

            command.downgrade(config, "0036_subtitle_evidence")
            remaining = {
                column["name"]
                for column in inspect(engine).get_columns("provider_models")
            }
            self.assertNotIn("input_modalities_json", remaining)
            self.assertNotIn("output_modalities_json", remaining)


if __name__ == "__main__":
    unittest.main()
