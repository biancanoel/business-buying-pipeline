"""The weekly run: fetch listings, store new ones, and have Claude read them.

Steps, in order:
1. Fetch every current listing from each source.
2. Split them into new listings and ones already stored. Stored ones only
   get their "last seen" time updated.
3. Store new listings straight away, before Claude reads them, so a run
   that stops part-way loses nothing it fetched.
4. Claude reads every stored listing it has not read yet that is within
   the price limit. Because "not read yet" is stored, this also catches up
   listings skipped for price when a search's limit widens, listings whose
   reading failed, and listings a stopped run never reached.

Matching and notifications come later.

The decisions are small pure functions (tested); `run_pipeline` only calls
the sources, Claude and the database in order, and is checked by real runs.
"""

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from business_finder.extraction.extractor import (
    ExtractionError,
    Extractor,
    extraction_price_ceiling,
    merge_fields,
    needs_model_extraction,
)
from business_finder.models import Listing, ListingFields, SavedSearch
from business_finder.sources import SourceAdapter
from business_finder.storage import Storage

# How often to report progress while Claude reads listings.
PROGRESS_EVERY_N_LISTINGS = 25


@dataclass
class RunSummary:
    """What one run did, for the person running it."""

    fetched: int = 0
    new: int = 0
    seen_again: int = 0
    read_by_claude: int = 0
    skipped_over_price_limit: int = 0
    failed_readings: list[str] = field(default_factory=list)


# --- Decisions -----------------------------------------------------------------


def split_new_and_known(fetched: Iterable[Listing], known_ids: set[str]) -> tuple[list[Listing], set[str]]:
    """Separate fetched listings into new ones and the IDs of ones already stored."""
    new_listings = []
    known_ids_seen = set()
    for listing in fetched:
        if listing.source_listing_id in known_ids:
            known_ids_seen.add(listing.source_listing_id)
        else:
            new_listings.append(listing)
    return new_listings, known_ids_seen


def listings_to_extract(listings: Iterable[Listing], price_ceiling: int | None) -> list[Listing]:
    """Choose the listings Claude should read: not read yet, and within the price limit."""
    return [
        listing
        for listing in listings
        if listing.extracted_at is None and needs_model_extraction(listing, price_ceiling)
    ]


def with_extraction(listing: Listing, claudes_reading: ListingFields, read_at: datetime) -> Listing:
    """Apply Claude's reading to a listing and mark it as read.

    The site's own fields still win where both have a value (see `merge_fields`).
    """
    return listing.model_copy(
        update={"fields": merge_fields(listing.fields, claudes_reading), "extracted_at": read_at}
    )


# --- Saved searches ----------------------------------------------------------------


def load_saved_search_files(directory: Path) -> list[SavedSearch]:
    """Read every saved search from the JSON files in a directory.

    Until there is a website for editing criteria, these files are where
    saved searches are edited; each run copies them into the database.
    """
    return [
        SavedSearch.model_validate(json.loads(path.read_text()))
        for path in sorted(directory.glob("*.json"))
    ]


# --- The run -------------------------------------------------------------------------


def run_pipeline(
    sources: list[SourceAdapter],
    extractor: Extractor,
    storage: Storage,
    saved_searches: list[SavedSearch],
    report: Callable[[str], None] = lambda message: None,
) -> RunSummary:
    """Run steps 1 to 4 once. `report` receives progress messages."""
    started_at = datetime.now(UTC)
    summary = RunSummary()

    for saved_search in saved_searches:
        storage.save_saved_search(saved_search)

    for source in sources:
        report(f"Fetching {source.source.value} listings…")
        fetched = list(source.fetch_listings())
        new_listings, known_ids_seen = split_new_and_known(fetched, storage.known_source_listing_ids(source.source))
        storage.mark_listings_seen(source.source, known_ids_seen, started_at)
        inserted = storage.insert_new_listings(new_listings)
        summary.fetched += len(fetched)
        summary.new += inserted
        summary.seen_again += len(known_ids_seen)
        report(f"  {len(fetched)} fetched: {inserted} new, {len(known_ids_seen)} already stored")

    price_ceiling = extraction_price_ceiling(saved_searches)
    awaiting = storage.listings_awaiting_extraction()
    to_read = listings_to_extract(awaiting, price_ceiling)
    summary.skipped_over_price_limit = len(awaiting) - len(to_read)
    report(f"Claude is reading {len(to_read)} listings ({summary.skipped_over_price_limit} over the price limit skipped)…")

    for number, listing in enumerate(to_read, start=1):
        try:
            claudes_reading = extractor.extract(listing.raw_text)
        except ExtractionError as error:
            summary.failed_readings.append(f"{listing.url}: {error}")
            continue
        storage.save_extraction(with_extraction(listing, claudes_reading, read_at=datetime.now(UTC)))
        summary.read_by_claude += 1
        if number % PROGRESS_EVERY_N_LISTINGS == 0:
            report(f"  {number} of {len(to_read)} read")

    return summary
