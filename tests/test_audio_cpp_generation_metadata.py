"""The global/Manager catalogue reflects the implemented generation routes."""

from pandrator.logic.audio_cpp_catalogue import (
    catalogue_page,
    package_metadata,
    speech_model_catalog,
)
from pandrator.logic.audio_cpp_generation_models import all_models, unavailable_models


def test_generation_metadata_matches_implemented_kind_without_narrator_voices():
    speech_ids = {row["id"] for row in speech_model_catalog()}
    for model in all_models():
        metadata = package_metadata(model.id)
        assert metadata["voice_mode"] == "none"
        assert model.id not in speech_ids
        assert metadata["package_availability"]["status"] == "installable"
        features = metadata["pandrator_features"]
        assert features["speech_generation"] == "not_applicable"
        for kind, capability in (("sound", "sound_generation"), ("music", "music_generation")):
            supported = kind in model.kinds
            assert features[capability] == ("request_supported" if supported else "none")
            assert (capability in metadata["capabilities"]) is supported


def test_unavailable_packages_do_not_advertise_generation_support():
    for model in unavailable_models():
        metadata = package_metadata(str(model["id"]))
        assert metadata["package_availability"]["status"] != "installable"
        assert "sound_generation" not in metadata["capabilities"]
        assert "music_generation" not in metadata["capabilities"]


def test_generation_capability_filters_match_registry():
    for kind, capability in (("sound", "sound_generation"), ("music", "music_generation")):
        expected = {model.id for model in all_models() if kind in model.kinds}
        page = catalogue_page(capability=capability, limit=100)
        assert {model["id"] for model in page["items"]} == expected
