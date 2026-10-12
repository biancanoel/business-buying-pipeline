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
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from business_finder.models import (
    BusinessTypeJudgment,
    Listing,
    ListingFields,
    ListingSource,
    ListingStatus,
    LocationPrecision,
    Match,
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
    run keeps everything saved up to that point. This relies on the
    connection being in autocommit mode: otherwise the first read opens a
    transaction that never ends, every later `transaction()` block only
    nests inside it, and nothing is saved when the connection closes.
    """

    def __init__(self, connection: psycopg.Connection, database_url: str | None = None, schema: str | None = None) -> None:
        self.connection = connection
        self.database_url = database_url
        self.schema = schema

    @classmethod
    def connect(cls, database_url: str | None = None, schema: str | None = None) -> "Storage":
        """Open a connection using DATABASE_URL from the environment.

        `schema` points the connection at a different set of tables; the
        database tests use it to work in their own schema.
        """
        database_url = database_url or os.environ.get(DATABASE_URL_ENVIRONMENT_VARIABLE)
        if not database_url:
            raise RuntimeError(f"{DATABASE_URL_ENVIRONMENT_VARIABLE} is not set")
        storage = cls(psycopg.connect(database_url, row_factory=dict_row, autocommit=True), database_url, schema)
        storage.use_schema(storage.connection)
        return storage

    def use_schema(self, connection: psycopg.Connection) -> None:
        """Point a connection at this storage's schema, if one was given."""
        if self.schema:
            connection.execute(sql.SQL("set search_path to {}").format(sql.Identifier(self.schema)))

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

    def count_saved_from_fresh_connection(self, source: ListingSource, source_listing_ids: Iterable[str]) -> int:
        """Count how many of these listings a brand-new connection can see.

        A separate connection only sees work that was really committed, so
        this catches saves that silently never reached the database.
        """
        with psycopg.connect(self.database_url, autocommit=True) as fresh_connection:
            self.use_schema(fresh_connection)
            row = fresh_connection.execute(
                "select count(*) from listings where source = %s and source_listing_id = any(%s)",
                (source.value, list(source_listing_ids)),
            ).fetchone()
        return row[0]

    def mark_listings_seen(self, source: ListingSource, source_listing_ids: Iterable[str], seen_at: datetime) -> None:
        """Record that these stored listings were still on the site at `seen_at`."""
        with self.connection.transaction():
            self.connection.execute(
                "update listings set last_seen_at = %s where source = %s and source_listing_id = any(%s)",
                (seen_at, source.value, list(source_listing_ids)),
            )

    def listings_awaiting_extraction(self) -> list[Listing]:
        """Return every stored listing Claude has not read yet."""
        rows = self.connection.execute(
            "select * from listings where extracted_at is null order by first_seen_at"
        ).fetchall()
        return [listing_from_row(row) for row in rows]

    def save_extraction(self, listing: Listing) -> None:
        """Store a listing's fields and the time Claude read it."""
        row = listing_to_row(listing)
        columns = list(ListingFields.model_fields) + ["extracted_at"]
        statement = sql.SQL("update listings set {updates} where id = {id}").format(
            updates=sql.SQL(", ").join(
                sql.SQL("{column} = {value}").format(column=sql.Identifier(column), value=sql.Placeholder(column))
                for column in columns
            ),
            id=sql.Placeholder("id"),
        )
        with self.connection.transaction():
            self.connection.execute(statement, values_for_sql({column: row[column] for column in columns + ["id"]}))

    def load_listings(self) -> list[Listing]:
        """Return every stored listing."""
        rows = self.connection.execute("select * from listings").fetchall()
        return [listing_from_row(row) for row in rows]

    def upsert(self, table: str, row: dict, key_columns: list[str], columns_to_update: list[str]) -> None:
        """Insert a row, or update the listed columns of the row with the same key.

        One atomic statement, so there is no gap between checking and writing.
        Columns not listed (such as when a match was first found or sent)
        keep their stored values.
        """
        statement = sql.SQL(
            "insert into {table} ({columns}) values ({values}) on conflict ({keys}) do update set {updates}"
        ).format(
            table=sql.Identifier(table),
            columns=sql.SQL(", ").join(sql.Identifier(column) for column in row),
            values=sql.SQL(", ").join(sql.Placeholder(column) for column in row),
            keys=sql.SQL(", ").join(sql.Identifier(column) for column in key_columns),
            updates=sql.SQL(", ").join(
                sql.SQL("{column} = excluded.{column}").format(column=sql.Identifier(column))
                for column in columns_to_update
            ),
        )
        with self.connection.transaction():
            self.connection.execute(statement, values_for_sql(row))

    # Saved searches

    def save_saved_search(self, saved_search: SavedSearch) -> None:
        """Insert a saved search, or replace the stored one with the same id."""
        row = saved_search_to_row(saved_search)
        columns_to_update = [column for column in row if column not in {"id", "created_at"}]
        self.upsert("saved_searches", row, key_columns=["id"], columns_to_update=columns_to_update)

    def load_saved_searches(self) -> list[SavedSearch]:
        """Return every stored saved search."""
        rows = self.connection.execute("select * from saved_searches order by created_at").fetchall()
        return [saved_search_from_row(row) for row in rows]

    # Business-type judgments

    def save_judgment(self, judgment: BusinessTypeJudgment) -> None:
        """Store a business-type judgment, replacing any earlier one for the same listing and search."""
        self.upsert(
            "business_type_judgments",
            judgment.model_dump(),
            key_columns=["saved_search_id", "listing_id"],
            columns_to_update=["fits", "matched_type", "reason", "criteria_fingerprint", "judged_at"],
        )

    def load_judgments(self, saved_search_id: UUID) -> dict[UUID, BusinessTypeJudgment]:
        """Return a search's judgments, keyed by listing id."""
        rows = self.connection.execute(
            "select * from business_type_judgments where saved_search_id = %s", (saved_search_id,)
        ).fetchall()
        return {row["listing_id"]: BusinessTypeJudgment(**row) for row in rows}

    # Matches

    def save_match(self, match: Match) -> None:
        """Store a match, or update the stored one for the same listing and search.

        When it was first matched and when it was sent are kept, so a match
        is only announced once even if it changes from possible to confirmed.
        """
        self.upsert(
            "matches",
            match.model_dump(),
            key_columns=["saved_search_id", "listing_id"],
            columns_to_update=["match_summary", "is_confirmed", "missing_facts"],
        )

    def load_matches(self, saved_search_id: UUID) -> list[Match]:
        """Return a search's matches, oldest first."""
        rows = self.connection.execute(
            "select * from matches where saved_search_id = %s order by matched_at", (saved_search_id,)
        ).fetchall()
        return [Match(**row) for row in rows]

    def remove_unsent_matches_except(self, saved_search_id: UUID, still_matching_listing_ids: set[UUID]) -> None:
        """Remove a search's matches that no longer qualify, unless they were already sent.

        A listing can stop matching when it is judged again or a search's
        criteria change. Matches already sent are kept as a record of what
        the buyer was shown.
        """
        with self.connection.transaction():
            self.connection.execute(
                "delete from matches where saved_search_id = %s and notified_at is null and not (listing_id = any(%s))",
                (saved_search_id, list(still_matching_listing_ids)),
            )

    def load_unsent_matches(self, saved_search_id: UUID) -> list[Match]:
        """Return a search's matches that have not been sent to the buyer yet, oldest first."""
        rows = self.connection.execute(
            "select * from matches where saved_search_id = %s and notified_at is null order by matched_at",
            (saved_search_id,),
        ).fetchall()
        return [Match(**row) for row in rows]

    def mark_matches_sent(self, saved_search_id: UUID, listing_ids: list[UUID], sent_at: datetime) -> None:
        """Record that these matches were sent, so they are never announced again."""
        with self.connection.transaction():
            self.connection.execute(
                "update matches set notified_at = %s where saved_search_id = %s and listing_id = any(%s)",
                (sent_at, saved_search_id, listing_ids),
            )
