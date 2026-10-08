"""Tests for extraction. Claude is replaced by a fake client; nothing calls the API."""


import pytest

from business_finder.config import ExtractionSettings
from business_finder.extraction.extractor import (
    ExtractionResult,
    extraction_price_ceiling,
    fields_from_extraction,
    load_output_schema,
    merge_fields,
    needs_model_extraction,
)
from business_finder.models import Listing, ListingFields, ListingSource, SavedSearch


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
        "region": None,
        "source_snippets": [],
        "conflict_notes": [],
    }
    answer.update(overrides)
    return answer


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


# --- ClaudeExtractor --------------------------------------------------------


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


def test_zero_and_negative_amounts_become_unknown():
    result = ExtractionResult(**make_answer(asking_price=0, gross_revenue=-5, employee_count=-1))

    fields = fields_from_extraction(result, current_year=2026)

    assert fields.asking_price is None
    assert fields.gross_revenue is None
    assert fields.employee_count is None


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


# --- Which listings to send to a model ---------------------------------------


def test_no_price_ceiling_when_any_search_has_no_maximum():
    searches = [make_saved_search(1_200_000), make_saved_search(None)]

    assert extraction_price_ceiling(searches) is None


def test_listing_over_the_ceiling_is_skipped():
    assert needs_model_extraction(make_listing(asking_price=1_500_000), price_ceiling=1_200_000) is False


def test_listing_without_a_price_is_extracted():
    assert needs_model_extraction(make_listing(asking_price=None), price_ceiling=1_200_000) is True


# --- Region and conflicts ----------------------------------------------------


def test_region_and_conflict_notes_pass_through_from_the_answer():
    result = ExtractionResult(
        **make_answer(region="Bay Area", conflict_notes=["county: Sonoma in details, Solano in description"])
    )

    fields = fields_from_extraction(result, current_year=2026)

    assert fields.region == "Bay Area"
    assert fields.conflict_notes == ["county: Sonoma in details, Solano in description"]


def test_merge_ignores_differences_that_are_not_conflicts():
    from_site = ListingFields(county="Sonoma", city="Petaluma", year_established=2021)
    from_model = ListingFields(county="Sonoma County", city="petaluma", year_established=2020)

    assert merge_fields(from_site, from_model).conflict_notes == []


def test_merge_keeps_model_conflict_notes_and_site_states():
    from_site = ListingFields(states=["CA", "OR"])
    from_model = ListingFields(conflict_notes=["county: Sonoma or Solano"], region="North Bay")

    merged = merge_fields(from_site, from_model)

    assert merged.states == ["CA", "OR"]
    assert merged.region == "North Bay"
    assert merged.conflict_notes == ["county: Sonoma or Solano"]


@pytest.mark.parametrize(("answer", "expected"), [("CA", "CA"), ("CA, OR", None), (None, None)])
def test_only_a_single_state_code_is_kept(answer, expected):
    result = ExtractionResult(**make_answer(state=answer))

    assert fields_from_extraction(result, current_year=2026).state == expected
