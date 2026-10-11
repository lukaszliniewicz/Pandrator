"""Single-model detail does not carry all catalogue capability profiles."""

from copy import deepcopy

import pytest

from pandrator.web.tts_catalogue_projection import (
    TtsCatalogueModelNotFoundError,
    _filter_service_models,
)


def test_detail_filters_capabilities_without_restricting_chooser_ids():
    service = {
        "models": ["first", "second"],
        "expressive_capabilities": {
            "first": {"instructions": "none"},
            "second": {"instructions": "field"},
        },
    }
    result = _filter_service_models(service, ["second"], service_id="fixture")
    assert result["expressive_capabilities"] == {"second": {"instructions": "field"}}
    assert result["models"] == ["first", "second"]


def test_invalid_model_filter_does_not_mutate_capability_profiles():
    service = {"models": ["first"], "expressive_capabilities": {"first": {"instructions": "none"}}}
    original = deepcopy(service)
    with pytest.raises(TtsCatalogueModelNotFoundError):
        _filter_service_models(service, ["missing"], service_id="fixture")
    assert service == original
