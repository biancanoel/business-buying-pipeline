"""Database access for listings and saved searches (Postgres on Supabase).

Two layers, kept apart on purpose:

- Conversion between the Pydantic models and flat database rows. This is
  where the rules live (which columns exist, how enums and JSON are stored),
  and it is plain Python with no database needed.
- A thin `Storage` class that sends SQL through psycopg. It holds no rules of
  its own; it only moves rows in and out.

The table definitions are in db/schema.sql.
"""

import os
from collections.abc import Iterable
from datetime import datetime

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from business_finder.models import (
    Listing,
    ListingFields,
    ListingSource,
    ListingStatus,
    LocationPrecision,
    SavedSearch,
)

DATABASE_URL_ENVIRONMENT_VARIABLE = "DATABASE_URL"

# Columns stored as Postgres jsonb. psycopg needs these wrapped explicitly;
# everything else (text, numbers, arrays, timestamps, uuids) adapts on its own.
JSON_COLUMNS = {"raw_data", "source_snippets"}


# --- Converting between models and rows --------------------------------------


def listing_to_row(listing: Listing) -> dict:
    """Flatten a listing into one row of the `listings` table.

    Enums are stored as their plain string values, which is what the
    table's CHECK constraints expect.
    """
    row = listing.model_dump(exclude={"fields"})
    row.update(listing.fields.model_dump())
    row["source"] = listing.source.value
    row["status"] = listing.status.value
    row["location_precision"] = listing.location_precision.value
    return row


def listing_from_row(row: dict) -> Listing:
    """Rebuild a listing from a row of the `listings` table.

    Columns that are not part of the model are ignored.
    """
    listing_values = {name: row[name] for name in Listing.model_fields if name != "fields"}
    listing_values["source"] = ListingSource(row["source"])
    listing_values["status"] = ListingStatus(row["status"])
    listing_values["location_precision"] = LocationPrecision(row["location_precision"])
    field_values = {name: row[name] for name in ListingFields.model_fields}
    return Listing(**listing_values, fields=ListingFields(**field_values))


def saved_search_to_row(saved_search: SavedSearch) -> dict:
    """Turn a saved search into one row of the `saved_searches` table."""
    return saved_search.model_dump()


def saved_search_from_row(row: dict) -> SavedSearch:
    """Rebuild a saved search from a row of the `saved_searches` table."""
    return SavedSearch(**{name: row[name] for name in SavedSearch.model_fields})


def values_for_sql(row: dict) -> dict:
    """Mark the two dictionary columns (raw_data and source_snippets) so the database stores them as JSON.

    Other values convert on their own. An empty raw_data stays a database null.
    """
    return {
        column: Jsonb(value) if column in JSON_COLUMNS and value is not None else value
        for column, value in row.items()
    }


# --- Talking to the database ---------------------------------------------------


class Storage:
    """Reads and writes listings and saved searches.

    Each method commits its own work, so a crash part-way through a weekly
    run keeps everything saved up to that point.
    """

    def __init__(self, connection: psycopg.Connection) -> None:
        self.connection = connection

    @classmethod
    def connect(cls, database_url: str | None = None) -> "Storage":
        """Open a connection using DATABASE_URL from the environment."""
        database_url = database_url or os.environ.get(DATABASE_URL_ENVIRONMENT_VARIABLE)
        if not database_url:
            raise RuntimeError(f"{DATABASE_URL_ENVIRONMENT_VARIABLE} is not set")
        return cls(psycopg.connect(database_url, row_factory=dict_row))

    def close(self) -> None:
        self.connection.close()

    # Listings

    def known_source_listing_ids(self, source: ListingSource) -> set[str]:
        """Return the site IDs of every listing already stored for a source.

        The pipeline uses this to skip extraction for listings it has seen.
        """
        rows = self.connection.execute(
            "select source_listing_id from listings where source = %s", (source.value,)
        ).fetchall()
        return {row["source_listing_id"] for row in rows}

    def insert_new_listings(self, listings: Iterable[Listing]) -> int:
        """Store listings that are not stored yet; return how many were added.

        A listing already stored (same source and site ID) is left untouched.
        """
        inserted_count = 0
        with self.connection.transaction():
            for listing in listings:
                row = values_for_sql(listing_to_row(listing))
                statement = sql.SQL(
                    "insert into listings ({columns}) values ({values}) "
                    "on conflict (source, source_listing_id) do nothing"
                ).format(
                    columns=sql.SQL(", ").join(sql.Identifier(column) for column in row),
                    values=sql.SQL(", ").join(sql.Placeholder(column) for column in row),
                )
                cursor = self.connection.execute(statement, row)
                inserted_count += cursor.rowcount
        return inserted_count

    def mark_listings_seen(self, source: ListingSource, source_listing_ids: Iterable[str], seen_at: datetime) -> None:
        """Record that these stored listings were still on the site at `seen_at`."""
        with self.connection.transaction():
            self.connection.execute(
                "update listings set last_seen_at = %s where source = %s and source_listing_id = any(%s)",
                (seen_at, source.value, list(source_listing_ids)),
            )

    def load_listings(self) -> list[Listing]:
        """Return every stored listing."""
        rows = self.connection.execute("select * from listings").fetchall()
        return [listing_from_row(row) for row in rows]

    # Saved searches

    def save_saved_search(self, saved_search: SavedSearch) -> None:
        """Insert a saved search, or replace the stored one with the same id."""
        row = saved_search_to_row(saved_search)
        updatable_columns = [column for column in row if column not in {"id", "created_at"}]
        statement = sql.SQL(
            "insert into saved_searches ({columns}) values ({values}) "
            "on conflict (id) do update set {updates}"
        ).format(
            columns=sql.SQL(", ").join(sql.Identifier(column) for column in row),
            values=sql.SQL(", ").join(sql.Placeholder(column) for column in row),
            updates=sql.SQL(", ").join(
                sql.SQL("{column} = excluded.{column}").format(column=sql.Identifier(column))
                for column in updatable_columns
            ),
        )
        with self.connection.transaction():
            self.connection.execute(statement, row)

    def load_saved_searches(self) -> list[SavedSearch]:
        """Return every stored saved search."""
        rows = self.connection.execute("select * from saved_searches order by created_at").fetchall()
        return [saved_search_from_row(row) for row in rows]
