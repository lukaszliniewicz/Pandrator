from datetime import date

import pytest

from pandrator.logic.model_catalogue import catalogue_page
from pandrator.logic.tts_generation_limits import generation_limit_metadata
from pandrator.logic.tts_service_catalogue import _default_tts_pricing


@pytest.mark.parametrize("day,rates", [(date(2026, 10, 8), (22, 11)), (date(2026, 10, 12), (22, 11)), (date(2026, 10, 13), (80, 40))])
def test_v4_launch_prices_expire_without_changing_older_models(day, rates):
    pricing = _default_tts_pricing("elevenlabs", as_of=day)
    assert [pricing[model]["input_cost_per_million_characters"] for model in ("eleven_v4", "eleven_v4_turbo")] == list(rates)
    assert pricing["eleven_v3"]["input_cost_per_million_characters"] == 80
    assert pricing["eleven_multilingual_v2"]["input_cost_per_million_characters"] == 80
    assert pricing["eleven_flash_v2_5"]["input_cost_per_million_characters"] == 40


@pytest.mark.parametrize("model,context", [("eleven_v4", "field"), ("eleven_v4_turbo", "none")])
def test_v4_catalogue_has_exact_route_controls_languages_and_safe_limit(model, context):
    item = next(row for row in catalogue_page(provider="elevenlabs", limit=100)["items"] if row["id"] == model)
    assert item["label"] == ("Eleven v4" if model == "eleven_v4" else "Eleven v4 Turbo")
    assert item["pandrator_features"]["instructions"] == "inline"
    assert item["pandrator_features"]["semantic_context"] == context
    assert item["language_support"]["source_key"] == "eleven_v4_languages"
    assert len(item["supported_languages"]) == 91
    assert {"pl", "ja", "ast", "yue", "or"} <= set(item["supported_languages"])
    assert "ori" not in item["supported_languages"]
    limits = generation_limit_metadata(service="elevenlabs", model=model)
    assert limits["input_characters"] == 2000
    assert limits["default_segment_characters"] == 1350
    assert limits["model_input_characters"] == (10000 if model == "eleven_v4" else None)
