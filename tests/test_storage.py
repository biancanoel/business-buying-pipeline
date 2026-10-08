"""Tests for converting listings and saved searches to and from database rows.

Only the conversion is tested here; the SQL itself is checked by real runs
against the database.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from business_finder.models import (
    Listing,
    ListingFields,
    ListingSource,
    ListingStatus,
    LocationPrecision,
    SavedSearch,
)
from business_finder.storage import (
    listing_from_row,
    listing_to_row,
    saved_search_from_row,
    saved_search_to_row,
)

SAVED_SEARCH_FILE = Path(__file__).parent.parent / "saved_searches" / "lucie.json"


def make_full_listing() -> Listing:
    """A fake listing with every field filled in, so nothing can be silently dropped."""
    return Listing(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        source=ListingSource.BIZBEN,
        source_listing_id="1000001",
        url="https://example.com/listing/1000001",
        title="Established Residential Cleaning Company",
        raw_text="Title: Established Residential Cleaning Company\n\nDescription:\nRecurring clients.",
        raw_data={"entityId": "#POST#1000001", "customAttributes": [{"key": "tw_sales", "value": "$1,250,000"}]},
        fields=ListingFields(
            business_name="Fake Cleaning Co",
            business_type="residential cleaning",
            requires_contractor_license=False,
            asking_price=850_000,
            gross_revenue=1_250_000,
            seller_discretionary_earnings=300_000,
            down_payment=200_000,
            year_established=2012,
            employee_count=14,
            city="Petaluma",
            county="Sonoma",
            state="CA",
            states=["CA"],
            region="North Bay",
            conflict_notes=["gross_revenue: listing details say 1250000, listing text says 1100000"],
            source_snippets={"gross_revenue": "2025 revenue of $1.25M"},
        ),
        location_precision=LocationPrecision.EXACT,
        latitude=38.2324,
        longitude=-122.6367,
        distance_miles=0.0,
        first_seen_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        last_seen_at=datetime(2026, 10, 8, 12, 0, tzinfo=UTC),
        status=ListingStatus.INTERESTED,
    )


def test_listing_survives_a_round_trip_through_a_row():
    listing = make_full_listing()

    assert listing_from_row(listing_to_row(listing)) == listing


def test_listing_row_is_flat_with_plain_values():
    row = listing_to_row(make_full_listing())

    assert "fields" not in row
    assert row["asking_price"] == 850_000
    assert row["source"] == "bizben"
    assert row["status"] == "interested"
    assert row["location_precision"] == "exact"


def test_saved_search_survives_a_round_trip_through_a_row():
    saved_search = SavedSearch.model_validate(json.loads(SAVED_SEARCH_FILE.read_text()))

    assert saved_search_from_row(saved_search_to_row(saved_search)) == saved_search
