"""Catalogue ownership, native settings reads and import compatibility."""

import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from pandrator.web.database import Database
from pandrator.web.models import AppSetting
from pandrator.web.tts_providers import TtsCatalogueService, TtsProviderRegistry
from tests.web_test_support import prepare_web_test_data_root

MISSING = object()
LEGACY = {"provider_configs": [{"id": "fixture", "api_base": "http://fixture.invalid"}]}


@pytest.mark.parametrize(
    ("connection", "defaults", "expected_connection", "expected_defaults"),
    [
        (MISSING, MISSING, {}, {}),
        (MISSING, LEGACY, LEGACY, LEGACY),
        ({}, {}, {}, {}),
        ({}, LEGACY, LEGACY, LEGACY),
        ({"api_base": "manual"}, LEGACY, {"api_base": "manual"}, LEGACY),
        ({"provider_configs": []}, LEGACY, {"provider_configs": []}, LEGACY),
        ([], LEGACY, LEGACY, LEGACY),
        (None, LEGACY, LEGACY, LEGACY),
        ("malformed", LEGACY, LEGACY, LEGACY),
        ({}, [], {}, {}),
        ({}, {"provider_configs": "malformed"}, {}, {"provider_configs": "malformed"}),
        ({}, {"provider_configs": {"id": "fixture"}}, {}, {"provider_configs": {"id": "fixture"}}),
    ],
)
def test_settings_fallback_preserves_revisions_without_writing(
    tmp_path,
    connection,
    defaults,
    expected_connection,
    expected_defaults,
):
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    providers = TtsProviderRegistry()
    catalogue = TtsCatalogueService(database, paths, providers)
    catalogue.manager_bridge.descriptor_path = None
    inputs = (("services.tts", connection, 7), ("defaults.tts", defaults, 11))
    try:
        with database.session() as session:
            for key, value, revision in inputs:
                if value is not MISSING:
                    session.add(AppSetting(key=key, value_json=deepcopy(value), revision=revision))
        with database.session() as session:
            persisted = {
                key: (deepcopy(row.value_json), row.revision, row.updated_at)
                for key, _, _ in inputs
                if (row := session.get(AppSetting, key)) is not None
            }
        result = catalogue._settings()
        assert result == (
            expected_connection,
            0 if connection is MISSING else 7,
            expected_defaults,
            0 if defaults is MISSING else 11,
        )
        # Legacy fallback is a read projection, not a persisted settings migration.
        with database.session() as session:
            for key, _, _ in inputs:
                row = session.get(AppSetting, key)
                if key not in persisted:
                    assert row is None
                else:
                    assert row is not None
                    assert (row.value_json, row.revision, row.updated_at) == persisted[key]
    finally:
        providers.close()
        catalogue.manager_bridge.session.close()
        database.dispose()


def test_catalogue_owner_imports_without_runtime_provider_facade():
    script = """
import sys
from typing import get_type_hints
from pandrator.web import tts_catalogue_service as owner

assert "pandrator.web.tts_providers" not in sys.modules
assert get_type_hints(owner.TtsCatalogueService.__init__)["providers"] is owner.TtsCatalogueProviders
from pandrator.web import tts_providers as facade
from pandrator.web import tts_catalogue_projection as projection
assert facade.TtsCatalogueService is owner.TtsCatalogueService
assert facade._supports_parallel_cloud_synthesis is projection._supports_parallel_cloud_synthesis
assert owner._supports_parallel_cloud_synthesis is projection._supports_parallel_cloud_synthesis
assert owner.tts_handler is facade.tts_handler
"""
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        timeout=30,
    )


def test_azure_preview_uses_model_specific_default_and_catalogue(tmp_path):
    from pandrator.logic import tts_handler

    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    providers = TtsProviderRegistry()
    catalogue = TtsCatalogueService(database, paths, providers)
    connections = {
        "provider_configs": [
            {
                "id": "azure",
                "provider": "azure",
                "api_base": "https://eastus.tts.speech.microsoft.com",
                "adapter": "azure_speech",
                "models": ["MAI-Voice-2", "MAI-Voice-2-Flash"],
                "default_model": "MAI-Voice-2",
                "default_voice": "en-US-Ethan:MAI-Voice-2",
                "default_voices": {
                    "MAI-Voice-2": "en-US-Ethan:MAI-Voice-2",
                    "MAI-Voice-2-Flash": "en-US-Ethan:MAI-Voice-2-Flash",
                },
                "voice_catalogues": {
                    "MAI-Voice-2-Flash": [
                        "en-US-Ethan:MAI-Voice-2-Flash",
                        "en-US-Harper:MAI-Voice-2-Flash",
                    ]
                },
            }
        ]
    }
    try:
        with database.session() as session:
            session.add(AppSetting(key="services.tts", value_json=connections, revision=7))
        settings = catalogue.preview_settings(
            "azure", model="MAI-Voice-2-Flash", voice=None, language="en"
        )
        assert settings is not None
        assert settings["voice"] == "en-US-Ethan:MAI-Voice-2-Flash"
        assert settings["speaker"] == "en-US-Ethan:MAI-Voice-2-Flash"
        service = tts_handler.get_service_config(connections, "azure")
        assert service is not None
        catalogues = service["voice_catalogues"]
        assert isinstance(catalogues, dict)
        assert catalogues["MAI-Voice-2-Flash"] == [
            "en-US-Ethan:MAI-Voice-2-Flash",
            "en-US-Harper:MAI-Voice-2-Flash",
        ]
        with database.session() as session:
            persisted = session.get(AppSetting, "services.tts")
            assert persisted is not None
            assert persisted.value_json == connections
            assert persisted.revision == 7
    finally:
        catalogue.close()
        providers.close()
        database.dispose()
