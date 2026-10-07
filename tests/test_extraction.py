"""Tests for extraction. Claude is replaced by a fake client; nothing calls the API."""

import json
from types import SimpleNamespace

import pytest

from business_finder.config import ExtractionSettings
from business_finder.extraction import make_extractor
from business_finder.extraction.claude_extractor import ClaudeExtractor
from business_finder.extraction.extractor import (
    ExtractionError,
    ExtractionResult,
    extraction_price_ceiling,
    fields_from_extraction,
    load_output_schema,
    load_system_prompt,
    merge_fields,
    needs_model_extraction,
)
from business_finder.models import Listing, ListingFields, ListingSource, SavedSearch

SETTINGS = ExtractionSettings(provider="anthropic", model="claude-haiku-5-5", effort="low")


def make_answer(**overrides) -> dict:
    """A complete model answer with every field null, changing only what a test needs."""
    answer = {
        "business_name": None,
        "business_type": None,
        "requires_contractor_license": None,
        "asking_price": None,
        "gross_revenue": None,
        "seller_discretionary_earnings": None,
        "down_payment": None,
        "year_established": None,
        "years_in_business": None,
        "employee_count": None,
        "city": None,
        "county": None,
        "state": None,
        "source_snippets": [],
    }
    answer.update(overrides)
    return answer


class FakeMessages:
    """Stands in for `client.messages`, returning a prepared response."""

    def __init__(self, response: SimpleNamespace) -> None:
        self.response = response
        self.requests: list[dict] = []

    def create(self, **request) -> SimpleNamespace:
        self.requests.append(request)
        return self.response


class FakeAnthropicClient:
    """Stands in for `anthropic.Anthropic`, with just the part the extractor uses."""

    def __init__(self, answer_text: str | None, stop_reason: str = "end_turn") -> None:
        content = []
        if answer_text is not None:
            content.append(SimpleNamespace(type="text", text=answer_text))
        self.messages = FakeMessages(SimpleNamespace(stop_reason=stop_reason, content=content))


def make_extractor_answering(answer: dict | str, stop_reason: str = "end_turn") -> tuple[ClaudeExtractor, FakeAnthropicClient]:
    """Build a ClaudeExtractor whose fake Claude gives `answer`."""
    answer_text = answer if isinstance(answer, str) else json.dumps(answer)
    client = FakeAnthropicClient(answer_text, stop_reason)
    return ClaudeExtractor(SETTINGS, client=client), client


def make_listing(asking_price: int | None) -> Listing:
    return Listing(
        source=ListingSource.BIZBEN,
        source_listing_id="1",
        url="https://example.com/1",
        raw_text="Fake listing",
        fields=ListingFields(asking_price=asking_price),
    )


def make_saved_search(asking_price_max: int | None) -> SavedSearch:
    return SavedSearch(
        name="Test search",
        business_types=["residential cleaning"],
        target_city="Petaluma",
        target_state="CA",
        asking_price_max=asking_price_max,
    )


# --- Prompt and schema files ------------------------------------------------


def test_schema_file_matches_the_result_model():
    schema = load_output_schema()

    assert set(schema["properties"]) == set(ExtractionResult.model_fields)
    assert set(schema["required"]) == set(ExtractionResult.model_fields)
    assert schema["additionalProperties"] is False


def test_system_prompt_tells_the_model_not_to_guess():
    prompt = load_system_prompt()

    assert "Return null" in prompt
    assert "Never guess" in prompt


def test_system_prompt_is_long_enough_to_cache():
    """Haiku 5.5 only caches prompts of at least 512 tokens; ~4 characters per token."""
    assert len(load_system_prompt()) > 512 * 4


# --- ClaudeExtractor --------------------------------------------------------


def test_extract_returns_fields_from_claudes_answer():
    extractor, _ = make_extractor_answering(
        make_answer(
            business_type="residential cleaning",
            requires_contractor_license=False,
            gross_revenue=1_300_000,
            seller_discretionary_earnings=280_000,
            state="CA",
            source_snippets=[{"field": "gross_revenue", "quote": "2025 revenue of $1.3M"}],
        )
    )

    fields = extractor.extract("Fake listing text")

    assert fields.business_type == "residential cleaning"
    assert fields.requires_contractor_license is False
    assert fields.gross_revenue == 1_300_000
    assert fields.seller_discretionary_earnings == 280_000
    assert fields.source_snippets == {"gross_revenue": "2025 revenue of $1.3M"}


def test_extract_sends_model_effort_schema_and_cached_instructions():
    extractor, client = make_extractor_answering(make_answer())

    extractor.extract("Fake listing text")

    request = client.messages.requests[0]
    assert request["model"] == "claude-haiku-5-5"
    assert request["output_config"]["effort"] == "low"
    assert request["output_config"]["format"] == {"type": "json_schema", "schema": load_output_schema()}
    assert request["system"][0]["text"] == load_system_prompt()
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Fake listing text" in request["messages"][0]["content"]


def test_extract_fails_on_refusal():
    extractor, _ = make_extractor_answering(make_answer(), stop_reason="refusal")

    with pytest.raises(ExtractionError, match="declined"):
        extractor.extract("Fake listing text")


def test_extract_fails_when_answer_is_cut_off():
    extractor, _ = make_extractor_answering('{"business_name": ', stop_reason="max_tokens")

    with pytest.raises(ExtractionError, match="cut off"):
        extractor.extract("Fake listing text")


def test_extract_fails_when_answer_does_not_match_schema():
    extractor, _ = make_extractor_answering({"business_name": "Only one field"})

    with pytest.raises(ExtractionError, match="schema"):
        extractor.extract("Fake listing text")


def test_extract_fails_when_there_is_no_text():
    client = FakeAnthropicClient(answer_text=None)
    extractor = ClaudeExtractor(SETTINGS, client=client)

    with pytest.raises(ExtractionError, match="no text"):
        extractor.extract("Fake listing text")


def test_make_extractor_rejects_unknown_provider():
    with pytest.raises(ValueError, match="Unknown extraction provider"):
        make_extractor(ExtractionSettings(provider="nobody"))


def test_settings_come_from_environment(monkeypatch):
    monkeypatch.setenv("BUSINESS_FINDER_EXTRACTION_MODEL", "claude-sonnet-5-5")
    monkeypatch.setenv("BUSINESS_FINDER_EXTRACTION_EFFORT", "medium")

    settings = ExtractionSettings.from_environment()

    assert settings.model == "claude-sonnet-5-5"
    assert settings.effort == "medium"
    assert settings.provider == "anthropic"


# --- Turning an answer into ListingFields -----------------------------------


def test_years_in_business_becomes_a_founding_year():
    result = ExtractionResult(**make_answer(years_in_business=20))

    fields = fields_from_extraction(result, current_year=2026)

    assert fields.year_established == 2006


def test_stated_year_wins_over_years_in_business():
    result = ExtractionResult(**make_answer(year_established=2001, years_in_business=20))

    fields = fields_from_extraction(result, current_year=2026)

    assert fields.year_established == 2001


def test_future_year_falls_back_to_years_in_business():
    result = ExtractionResult(**make_answer(year_established=2031, years_in_business=10))

    fields = fields_from_extraction(result, current_year=2026)

    assert fields.year_established == 2016


def test_zero_and_negative_amounts_become_unknown():
    result = ExtractionResult(**make_answer(asking_price=0, gross_revenue=-5, employee_count=-1))

    fields = fields_from_extraction(result, current_year=2026)

    assert fields.asking_price is None
    assert fields.gross_revenue is None
    assert fields.employee_count is None


def test_state_is_upper_cased():
    result = ExtractionResult(**make_answer(state="ca"))

    assert fields_from_extraction(result, current_year=2026).state == "CA"


# --- Merging site and model fields ------------------------------------------


def test_site_fields_win_over_model_fields():
    from_site = ListingFields(asking_price=850_000, gross_revenue=1_250_000)
    from_model = ListingFields(asking_price=900_000, gross_revenue=1_100_000, seller_discretionary_earnings=300_000)

    merged = merge_fields(from_site, from_model)

    assert merged.asking_price == 850_000
    assert merged.gross_revenue == 1_250_000
    assert merged.seller_discretionary_earnings == 300_000


def test_model_business_type_wins_over_site_category():
    from_site = ListingFields(business_type="Lawn/Landscaping")
    from_model = ListingFields(business_type="residential yard maintenance", requires_contractor_license=False)

    merged = merge_fields(from_site, from_model)

    assert merged.business_type == "residential yard maintenance"
    assert merged.requires_contractor_license is False


def test_site_business_type_is_kept_when_model_has_none():
    merged = merge_fields(ListingFields(business_type="Lawn/Landscaping"), ListingFields())

    assert merged.business_type == "Lawn/Landscaping"


def test_snippets_from_both_are_kept():
    from_site = ListingFields(source_snippets={"asking_price": "Asking $850,000"})
    from_model = ListingFields(source_snippets={"year_established": "Since 2009"})

    merged = merge_fields(from_site, from_model)

    assert merged.source_snippets == {"asking_price": "Asking $850,000", "year_established": "Since 2009"}


# --- Which listings to send to a model ---------------------------------------


def test_price_ceiling_is_the_highest_search_maximum():
    searches = [make_saved_search(1_200_000), make_saved_search(800_000)]

    assert extraction_price_ceiling(searches) == 1_200_000


def test_no_price_ceiling_when_any_search_has_no_maximum():
    searches = [make_saved_search(1_200_000), make_saved_search(None)]

    assert extraction_price_ceiling(searches) is None


def test_listing_over_the_ceiling_is_skipped():
    assert needs_model_extraction(make_listing(asking_price=1_500_000), price_ceiling=1_200_000) is False


def test_listing_at_the_ceiling_is_extracted():
    assert needs_model_extraction(make_listing(asking_price=1_200_000), price_ceiling=1_200_000) is True


def test_listing_without_a_price_is_extracted():
    assert needs_model_extraction(make_listing(asking_price=None), price_ceiling=1_200_000) is True


def test_everything_is_extracted_without_a_ceiling():
    assert needs_model_extraction(make_listing(asking_price=9_000_000), price_ceiling=None) is True
