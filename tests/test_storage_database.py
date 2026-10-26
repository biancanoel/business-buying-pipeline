"""Tests that talk to a real Postgres database.

They run only when asked, because they need the database and network:

    BUSINESS_FINDER_DATABASE_TESTS=1 uv run --env-file .env pytest tests/test_storage_database.py

They work in their own schema (`business_finder_test`), created from
db/schema.sql before each test and dropped after, so real data is never touched.

The connection string is read from the environment inside each helper and
never passed as an argument, so a failing test cannot print the password.
"""

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from business_finder.models import BusinessTypeJudgment, Listing, ListingFields, ListingSource, Match, SavedSearch
from business_finder.storage import Storage

pytestmark = pytest.mark.skipif(
    os.environ.get("BUSINESS_FINDER_DATABASE_TESTS") != "1",
    reason="database tests run only with BUSINESS_FINDER_DATABASE_TESTS=1",
)

TEST_SCHEMA = "business_finder_test"
SCHEMA_FILE = Path(__file__).parent.parent / "db" / "schema.sql"


def connect_to_database(**options) -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"], **options)


@pytest.fixture
def test_schema():
    """Create a fresh test schema with the project's tables, and drop it afterwards."""
    with connect_to_database(autocommit=True) as connection:
        connection.execute(f"drop schema if exists {TEST_SCHEMA} cascade")
        connection.execute(f"create schema {TEST_SCHEMA}")
        connection.execute(f"set search_path to {TEST_SCHEMA}")
        connection.execute(SCHEMA_FILE.read_text())
    yield TEST_SCHEMA
    with connect_to_database(autocommit=True) as connection:
        connection.execute(f"drop schema if exists {TEST_SCHEMA} cascade")


def make_listing() -> Listing:
    return Listing(
        source=ListingSource.BIZBEN,
        source_listing_id="1000001",
        url="https://example.com/listing/1000001",
        raw_text="Fake listing",
        fields=ListingFields(asking_price=850_000),
    )


def count_from_a_new_connection(query: str) -> int:
    with connect_to_database() as connection:
        connection.execute(f"set search_path to {TEST_SCHEMA}")
        return connection.execute(query).fetchone()[0]


def test_saved_listings_and_readings_are_visible_from_another_connection(test_schema):
    storage = Storage.connect()
    try:
        storage.connection.execute(f"set search_path to {test_schema}")
        listing = make_listing()

        storage.known_source_listing_ids(ListingSource.BIZBEN)
        storage.insert_new_listings([listing])
        storage.save_extraction(listing.model_copy(update={"extracted_at": datetime.now(UTC)}))

        assert count_from_a_new_connection("select count(*) from listings") == 1
        assert count_from_a_new_connection("select count(*) from listings where extracted_at is not null") == 1
    finally:
        storage.close()


def test_count_from_a_fresh_connection_finds_saved_listings(test_schema):
    storage = Storage.connect(schema=test_schema)
    try:
        storage.known_source_listing_ids(ListingSource.BIZBEN)
        storage.insert_new_listings([make_listing()])

        found = storage.count_saved_from_fresh_connection(ListingSource.BIZBEN, ["1000001", "not-stored"])

        assert found == 1
    finally:
        storage.close()



def store_listing_and_search(storage: Storage) -> tuple[Listing, SavedSearch]:
    """Store one listing and one saved search for the match and judgment tests."""
    listing = make_listing()
    saved_search = SavedSearch(name="Test search", business_types=["residential cleaning"], target_city="Petaluma", target_state="CA")
    storage.insert_new_listings([listing])
    storage.save_saved_search(saved_search)
    return listing, saved_search


def test_judging_a_listing_again_replaces_the_earlier_judgment(test_schema):
    storage = Storage.connect(schema=test_schema)
    try:
        listing, saved_search = store_listing_and_search(storage)
        first = BusinessTypeJudgment(
            saved_search_id=saved_search.id, listing_id=listing.id, fits=False, reason="Unclear.", criteria_fingerprint="old"
        )
        second = first.model_copy(update={"fits": True, "reason": "Maid service.", "criteria_fingerprint": "new"})

        storage.save_judgment(first)
        storage.save_judgment(second)

        assert storage.load_judgments(saved_search.id) == {listing.id: second}
        assert count_from_a_new_connection("select count(*) from business_type_judgments") == 1
    finally:
        storage.close()


def test_saving_a_match_again_updates_it_and_keeps_the_sent_time(test_schema):
    storage = Storage.connect(schema=test_schema)
    try:
        listing, saved_search = store_listing_and_search(storage)
        possible = Match(
            saved_search_id=saved_search.id, listing_id=listing.id, match_summary="Maid service.",
            is_confirmed=False, missing_facts=["seller_discretionary_earnings"],
            notified_at=datetime(2026, 10, 9, 12, 0, tzinfo=UTC),
        )
        confirmed = possible.model_copy(update={"is_confirmed": True, "missing_facts": [], "notified_at": None})

        storage.save_match(possible)
        storage.save_match(confirmed)

        stored = storage.load_matches(saved_search.id)
        assert len(stored) == 1
        assert stored[0].is_confirmed
        assert stored[0].missing_facts == []
        assert stored[0].notified_at == possible.notified_at
    finally:
        storage.close()



def test_the_matched_business_type_is_stored_with_the_judgment(test_schema):
    storage = Storage.connect(schema=test_schema)
    try:
        listing, saved_search = store_listing_and_search(storage)
        judgment = BusinessTypeJudgment(
            saved_search_id=saved_search.id, listing_id=listing.id, fits=True, reason="Maid service.",
            matched_type="residential cleaning", criteria_fingerprint="current",
        )

        storage.save_judgment(judgment)

        assert storage.load_judgments(saved_search.id)[listing.id].matched_type == "residential cleaning"
    finally:
        storage.close()


def test_unsent_matches_that_no_longer_qualify_are_removed_and_sent_ones_kept(test_schema):
    storage = Storage.connect(schema=test_schema)
    try:
        listing, saved_search = store_listing_and_search(storage)
        sent_listing = make_listing().model_copy(update={"id": uuid4(), "source_listing_id": "1000002"})
        storage.insert_new_listings([sent_listing])
        unsent = Match(saved_search_id=saved_search.id, listing_id=listing.id, match_summary="Massage center.")
        sent = Match(
            saved_search_id=saved_search.id, listing_id=sent_listing.id, match_summary="Maid service.",
            notified_at=datetime(2026, 10, 9, 12, 0, tzinfo=UTC),
        )
        storage.save_match(unsent)
        storage.save_match(sent)

        storage.remove_unsent_matches_except(saved_search.id, still_matching_listing_ids=set())

        assert [match.listing_id for match in storage.load_matches(saved_search.id)] == [sent_listing.id]
    finally:
        storage.close()


def test_matches_marked_as_sent_are_no_longer_listed_as_unsent(test_schema):
    storage = Storage.connect(schema=test_schema)
    try:
        listing, saved_search = store_listing_and_search(storage)
        storage.save_match(Match(saved_search_id=saved_search.id, listing_id=listing.id, match_summary="Maid service."))
        unsent_before = storage.load_unsent_matches(saved_search.id)

        storage.mark_matches_sent(saved_search.id, [listing.id], sent_at=datetime(2026, 10, 12, 9, 0, tzinfo=UTC))

        assert [match.listing_id for match in unsent_before] == [listing.id]
        assert storage.load_unsent_matches(saved_search.id) == []
        assert storage.load_matches(saved_search.id)[0].notified_at == datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
    finally:
        storage.close()


def test_processed_emails_are_remembered(test_schema):
    storage = Storage.connect(schema=test_schema)
    try:
        storage.mark_email_processed(
            message_id="<alert-1@bizbuysell.com>",
            source=ListingSource.BIZBUYSELL,
            received_at=datetime(2026, 10, 14, 15, 30, tzinfo=UTC),
            body_text="New listings match your search.",
            listing_count=3,
        )

        assert storage.processed_email_ids() == {"<alert-1@bizbuysell.com>"}
    finally:
        storage.close()


def test_count_listings_counts_every_stored_listing(test_schema):
    storage = Storage.connect(schema=test_schema)
    try:
        empty_count = storage.count_listings()
        storage.insert_new_listings([make_listing(), make_listing().model_copy(update={"id": uuid4(), "source_listing_id": "1000002"})])

        assert empty_count == 0
        assert storage.count_listings() == 2
    finally:
        storage.close()
