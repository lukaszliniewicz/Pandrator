"""Workspace boundaries for persisted voice catalogue data and cached discovery."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest

from pandrator.web.credentials import ResolvedCredential
from pandrator.web.manager_proxy import LocalManagerProxy
from pandrator.web.models import Voice
from pandrator.web.tts_catalogue_service import TtsCatalogueService
from pandrator.web.tts_providers import TtsProviderRegistry
from tests.test_voice_workspace_library import (
    VoiceHarness,
)
from tests.test_voice_workspace_library import (
    voice_harness as voice_harness,
)


@pytest.fixture
def catalogue(voice_harness: VoiceHarness) -> Iterator[TtsCatalogueService]:
    service = TtsCatalogueService(
        voice_harness.database,
        voice_harness.paths,
        TtsProviderRegistry(),
        manager_bridge=cast(LocalManagerProxy, SimpleNamespace()),
    )
    try:
        yield service
    finally:
        service.close()


def test_catalogue_previews_filter_owner_before_paths_or_metadata(
    voice_harness: VoiceHarness, catalogue: TtsCatalogueService
) -> None:
    harness = voice_harness
    identifiers = {}
    for key in ("alpha", "beta"):
        path = harness.paths.uploads / f"{key}-preview.wav"
        path.write_bytes(f"synthetic {key} preview".encode())
        artifact = harness.artifacts.register(
            path,
            kind="audio",
            role="tts_voice_preview",
            workspace_id=harness.workspaces[key],
            metadata={"voice": f"{key}-remote-id", "preview_text": f"Private {key} text"},
        )
        identifiers[key] = artifact.id
    alpha = catalogue._previews(workspace_id=harness.workspaces["alpha"])
    beta = catalogue._previews(workspace_id=harness.workspaces["beta"])
    assert [row["artifact_id"] for row in alpha] == [identifiers["alpha"]]
    assert [row["artifact_id"] for row in beta] == [identifiers["beta"]]
    assert alpha[0]["preview_text"] == "Private alpha text"
    assert beta[0]["preview_text"] == "Private beta text"
    assert catalogue._previews() == []


def test_remembered_google_voices_merge_only_into_the_selected_owner(
    voice_harness: VoiceHarness, catalogue: TtsCatalogueService, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = voice_harness
    for key in ("alpha", "beta"):
        with harness.database.session() as session:
            session.add(
                Voice(
                    workspace_id=harness.workspaces[key],
                    name=f"Private {key} voice",
                    metadata_json={
                        "providers": {
                            "gemini": {
                                "resource_kind": "designed_voice",
                                "status": "ready",
                                "scope_fingerprint": "same-provider-account",
                                "voice_id": f"voice_{key}",
                            }
                        }
                    },
                )
            )
    monkeypatch.setattr(
        "pandrator.logic.google_voice_scope.google_voice_scope",
        lambda *_args, **_kwargs: {"fingerprint": "same-provider-account"},
    )
    credential = cast(ResolvedCredential, SimpleNamespace(resolved_value=lambda: "synthetic"))
    for key in ("alpha", "beta"):
        service: dict[str, Any] = {"id": "gemini", "models": ["gemini-3.8-flash-tts"], "voices": []}
        catalogue._merge_remembered_google_voices(
            [service], [credential], workspace_id=harness.workspaces[key]
        )
        assert service["managed_google_voice_ids"] == [f"voice_{key}"]
        assert service["voice_catalogues"]["gemini-3.8-flash-tts"] == [f"voice_{key}"]
        metadata = service["voice_metadata"][f"gemini-3.8-flash-tts:voice_{key}"]
        assert metadata["display_name"] == f"Private {key} voice"


def test_discovery_and_health_cache_namespaces_do_not_cross_owners(
    voice_harness: VoiceHarness, catalogue: TtsCatalogueService
) -> None:
    service = {"id": "audio_cpp", "adapter": "audio_cpp", "api_base": "https://synthetic.invalid"}
    keys = {
        key: catalogue._native_discovery_key(service, {}, workspace_id=workspace_id)
        for key, workspace_id in voice_harness.workspaces.items()
    }
    assert keys["alpha"] is not None and keys["alpha"] != keys["beta"]
    catalogue._retain_native_discovery(keys["alpha"], {"voices": ["alpha-reference"]})
    alpha = catalogue._cached_native_discovery(keys["alpha"])
    assert alpha is not None and alpha["voices"] == ["alpha-reference"]
    assert catalogue._cached_native_discovery(keys["beta"]) is None
    assert catalogue._health_observation_key(
        service, {}, workspace_id=voice_harness.workspaces["alpha"]
    ) != catalogue._health_observation_key(
        service, {}, workspace_id=voice_harness.workspaces["beta"]
    )


def test_preview_projection_rejects_archived_context(
    voice_harness: VoiceHarness, catalogue: TtsCatalogueService
) -> None:
    with pytest.raises(ValueError, match="inactive workspace"):
        catalogue._previews(workspace_id=voice_harness.workspaces["archived"])
