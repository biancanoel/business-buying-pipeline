"""Tests for the shared Pydantic models and their match with the database schema."""

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from business_finder.models import (
    Listing,
    ListingFields,
    ListingSource,
    ListingStatus,
    LocationPrecision,
    Match,
    SavedSearch,
)

PROJECT_ROOT = Path(__file__).parent.parent
SCHEMA_PATH = PROJECT_ROOT / "db" / "schema.sql"
SAVED_SEARCHES_DIR = PROJECT_ROOT / "saved_searches"


def make_listing(**overrides) -> Listing:
    """Build a valid listing with fake data, changing only what a test cares about."""
    values = {
        "source": ListingSource.TRANSWORLD,
        "source_listing_id": "TW-12345",
        "url": "https://example.com/listing/12345",
        "raw_text": "Established residential cleaning company. Asking $850,000.",
    }
    values.update(overrides)
    return Listing(**values)


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


def columns_in_table(table_name: str) -> set[str]:
    """Return the column names declared in one CREATE TABLE block of schema.sql.

    A column line is indented exactly four spaces and starts with a lowercase
    name followed by a type. Deeper-indented lines continue the previous
    line, and table constraints (unique, check) are skipped.
    """
    schema_text = SCHEMA_PATH.read_text()
    table_pattern = rf"create table if not exists {table_name} \((.*?)\n\);"
    table_body = re.search(table_pattern, schema_text, re.DOTALL).group(1)

    column_names = set()
    for line in table_body.splitlines():
        found = re.match(r"^ {4}([a-z_]+)\s+[a-z]", line)
        if found and found.group(1) not in {"unique", "check"}:
            column_names.add(found.group(1))
    return column_names


# --- Listing ---------------------------------------------------------------


def test_new_listing_gets_defaults():
    listing = make_listing()

    assert listing.status == ListingStatus.NEW
    assert listing.location_precision == LocationPrecision.UNKNOWN
    assert listing.fields == ListingFields()
    assert listing.first_seen_at.tzinfo is not None


def test_each_listing_gets_its_own_stable_id():
    first = make_listing()
    second = make_listing()

    assert first.id != second.id


def test_listing_rejects_unknown_source():
    with pytest.raises(ValidationError):
        make_listing(source="loopnet")


def test_listing_requires_a_source_listing_id():
    with pytest.raises(ValidationError):
        make_listing(source_listing_id="")


def test_listing_round_trips_through_json():
    listing = make_listing(
        fields=ListingFields(asking_price=850_000, city="Petaluma", county="Sonoma")
    )

    restored = Listing.model_validate_json(listing.model_dump_json())

    assert restored == listing


# --- ListingFields ---------------------------------------------------------


def test_extracted_fields_default_to_unknown_not_zero():
    fields = ListingFields()

    assert fields.asking_price is None
    assert fields.gross_revenue is None
    assert fields.seller_discretionary_earnings is None


def test_extracted_money_cannot_be_negative():
    with pytest.raises(ValidationError):
        ListingFields(asking_price=-1)


def test_extracted_year_must_be_plausible():
    with pytest.raises(ValidationError):
        ListingFields(year_established=15)


# --- SavedSearch -----------------------------------------------------------


def test_saved_search_needs_at_least_one_business_type():
    with pytest.raises(ValidationError):
        make_saved_search(business_types=[])


def test_saved_search_rejects_min_price_above_max():
    with pytest.raises(ValidationError, match="asking_price_min"):
        make_saved_search(asking_price_min=1_200_000, asking_price_max=1_000_000)


def test_saved_search_rejects_target_price_above_max():
    with pytest.raises(ValidationError, match="asking_price_target"):
        make_saved_search(asking_price_target=1_100_000, asking_price_max=1_000_000)


def test_saved_search_rejects_target_price_below_min():
    with pytest.raises(ValidationError, match="asking_price_target"):
        make_saved_search(asking_price_target=500_000, asking_price_min=600_000)


def test_saved_search_accepts_target_equal_to_max():
    search = make_saved_search(asking_price_target=1_000_000, asking_price_max=1_000_000)

    assert search.asking_price_target == search.asking_price_max


@pytest.mark.parametrize("path", sorted(SAVED_SEARCHES_DIR.glob("*.json")), ids=lambda p: p.name)
def test_saved_search_files_are_valid(path: Path):
    SavedSearch.model_validate(json.loads(path.read_text()))


# --- Schema stays in sync with the models ----------------------------------


def test_listings_table_has_a_column_for_every_listing_field():
    listing_columns = set(Listing.model_fields) - {"fields"}
    extracted_columns = set(ListingFields.model_fields)

    assert listing_columns | extracted_columns == columns_in_table("listings")


def test_saved_searches_table_has_a_column_for_every_field():
    assert set(SavedSearch.model_fields) == columns_in_table("saved_searches")


def test_matches_table_has_a_column_for_every_field():
    assert set(Match.model_fields) == columns_in_table("matches")


def test_schema_allows_every_source_and_status():
    schema_text = SCHEMA_PATH.read_text()

    for source in ListingSource:
        assert f"'{source.value}'" in schema_text
    for status in ListingStatus:
        assert f"'{status.value}'" in schema_text
    for precision in LocationPrecision:
        assert f"'{precision.value}'" in schema_text
