"""Tests for the shared Pydantic models and their match with the database schema."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from business_finder.models import (
    ListingFields,
    ListingSource,
    ListingStatus,
    LocationPrecision,
    SavedSearch,
)

PROJECT_ROOT = Path(__file__).parent.parent
SCHEMA_PATH = PROJECT_ROOT / "db" / "schema.sql"
SAVED_SEARCHES_DIR = PROJECT_ROOT / "saved_searches"


def make_saved_search(**overrides) -> SavedSearch:
    """Build a valid saved search with fake data, changing only what a test cares about."""
    values = {
        "name": "Test search",
        "business_types": ["residential cleaning"],
        "target_city": "Petaluma",
        "target_state": "CA",
    }
    values.update(overrides)
    return SavedSearch(**values)


# --- ListingFields ---------------------------------------------------------


def test_extracted_fields_default_to_unknown_not_zero():
    fields = ListingFields()

    assert fields.asking_price is None
    assert fields.gross_revenue is None
    assert fields.seller_discretionary_earnings is None


# --- SavedSearch -----------------------------------------------------------


def test_saved_search_rejects_min_price_above_max():
    with pytest.raises(ValidationError, match="asking_price_min"):
        make_saved_search(asking_price_min=1_200_000, asking_price_max=1_000_000)


def test_saved_search_rejects_target_price_above_max():
    with pytest.raises(ValidationError, match="asking_price_target"):
        make_saved_search(asking_price_target=1_100_000, asking_price_max=1_000_000)


@pytest.mark.parametrize("path", sorted(SAVED_SEARCHES_DIR.glob("*.json")), ids=lambda p: p.name)
def test_saved_search_files_are_valid(path: Path):
    SavedSearch.model_validate(json.loads(path.read_text()))


# --- Schema stays in sync with the models ----------------------------------


def test_schema_allows_every_source_and_status():
    schema_text = SCHEMA_PATH.read_text()

    for source in ListingSource:
        assert f"'{source.value}'" in schema_text
    for status in ListingStatus:
        assert f"'{status.value}'" in schema_text
    for precision in LocationPrecision:
        assert f"'{precision.value}'" in schema_text
