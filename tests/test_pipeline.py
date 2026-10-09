"""Tests for the pipeline's decisions: what is new, what Claude should read, and how its reading is applied."""

from datetime import UTC, datetime

import pytest

from business_finder.models import Listing, ListingFields, ListingSource
from business_finder.pipeline import (
    ListingsNotSavedError,
    listings_to_extract,
    split_new_and_known,
    stop_if_not_saved,
    with_extraction,
)

READ_AT = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def make_listing(source_listing_id: str, asking_price: int | None = None, extracted_at: datetime | None = None) -> Listing:
    return Listing(
        source=ListingSource.BIZBEN,
        source_listing_id=source_listing_id,
        url=f"https://example.com/{source_listing_id}",
        raw_text=f"Fake listing {source_listing_id}",
        fields=ListingFields(asking_price=asking_price),
        extracted_at=extracted_at,
    )


def test_new_listings_are_separated_from_ones_already_stored():
    fetched = [make_listing("1"), make_listing("2"), make_listing("3")]

    new_listings, known_ids_seen = split_new_and_known(fetched, known_ids={"2", "9"})

    assert [listing.source_listing_id for listing in new_listings] == ["1", "3"]
    assert known_ids_seen == {"2"}


def test_claude_reads_unread_listings_within_the_price_limit_including_ones_skipped_before():
    unread_within_limit = make_listing("within", asking_price=900_000)
    unread_without_price = make_listing("no-price")
    unread_over_limit = make_listing("over", asking_price=1_500_000)
    already_read = make_listing("read", asking_price=900_000, extracted_at=READ_AT)

    selected = listings_to_extract(
        [unread_within_limit, unread_without_price, unread_over_limit, already_read], price_ceiling=1_200_000
    )

    assert [listing.source_listing_id for listing in selected] == ["within", "no-price"]


def test_applying_claudes_reading_keeps_site_values_fills_gaps_and_marks_the_listing_read():
    listing = make_listing("1", asking_price=850_000)
    claudes_reading = ListingFields(asking_price=900_000, gross_revenue=1_250_000, requires_contractor_license=False)

    updated = with_extraction(listing, claudes_reading, read_at=READ_AT)

    assert updated.fields.asking_price == 850_000
    assert updated.fields.gross_revenue == 1_250_000
    assert updated.fields.requires_contractor_license is False
    assert updated.extracted_at == READ_AT


def test_run_stops_when_new_listings_are_missing_from_the_database():
    with pytest.raises(ListingsNotSavedError, match="2 of 5"):
        stop_if_not_saved(source=ListingSource.BIZBEN, expected=5, found=3)


def test_run_continues_when_every_new_listing_is_saved():
    stop_if_not_saved(source=ListingSource.BIZBEN, expected=5, found=5)
