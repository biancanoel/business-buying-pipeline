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

import psycopg
import pytest

from business_finder.models import Listing, ListingFields, ListingSource
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
