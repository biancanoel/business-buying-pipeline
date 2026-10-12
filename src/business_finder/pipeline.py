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

5. Match every stored listing against each saved search: number and
   location checks first, then a model's call on business type for the
   listings those checks keep (judged once per listing and search).

Notifications come later.

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
from business_finder.extraction.business_type_judge import BusinessTypeJudge
from business_finder.geography import Gazetteer, listings_not_out_of_reach, locate, search_areas_for, within_search_area
from business_finder.matching import (
    business_criteria_fingerprint,
    check_criteria,
    decide_match,
    listings_needing_judgment,
)
from business_finder.models import BusinessTypeJudgment, Listing, ListingFields, ListingSource, SavedSearch
from business_finder.sources import SourceAdapter
from business_finder.storage import Storage

# How often to report progress while Claude reads listings.
PROGRESS_EVERY_N_LISTINGS = 25


class ListingsNotSavedError(Exception):
    """Raised when new listings are not in the database after being saved.

    The run stops straight away, before any time or money is spent having
    Claude read listings that would not be kept.
    """


@dataclass
class RunSummary:
    """What one run did, for the person running it."""

    fetched: int = 0
    new: int = 0
    seen_again: int = 0
    read_by_claude: int = 0
    skipped_over_price_limit: int = 0
    skipped_out_of_reach: int = 0
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


def stop_if_not_saved(source: ListingSource, expected: int, found: int) -> None:
    """Stop the run if fewer new listings are in the database than were saved."""
    if found < expected:
        missing = expected - found
        raise ListingsNotSavedError(
            f"{missing} of {expected} new {source.value} listings were not in the database after saving; "
            "stopping before Claude reads anything."
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
    gazetteer: Gazetteer | None = None,
    approximate_margin_miles: float = 50,
) -> RunSummary:
    """Run steps 1 to 4 once. `report` receives progress messages.

    With a `gazetteer`, listings clearly out of reach of every saved search
    are not sent to Claude; they stay unread and are caught up if a search's
    area widens.
    """
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
        new_ids = [listing.source_listing_id for listing in new_listings]
        found = storage.count_saved_from_fresh_connection(source.source, new_ids)
        stop_if_not_saved(source.source, expected=len(new_ids), found=found)
        summary.fetched += len(fetched)
        summary.new += inserted
        summary.seen_again += len(known_ids_seen)
        report(f"  {len(fetched)} fetched: {inserted} new, {len(known_ids_seen)} already stored")

    price_ceiling = extraction_price_ceiling(saved_searches)
    awaiting = storage.listings_awaiting_extraction()
    within_price = listings_to_extract(awaiting, price_ceiling)
    to_read = within_price
    if gazetteer is not None:
        areas = search_areas_for(saved_searches, gazetteer)
        to_read = listings_not_out_of_reach(within_price, gazetteer, areas, approximate_margin_miles)
    summary.skipped_over_price_limit = len(awaiting) - len(within_price)
    summary.skipped_out_of_reach = len(within_price) - len(to_read)
    report(
        f"Claude is reading {len(to_read)} listings "
        f"(skipped: {summary.skipped_over_price_limit} over the price limit, {summary.skipped_out_of_reach} out of reach)…"
    )

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


# --- Step 5: matching ------------------------------------------------------------------


@dataclass
class MatchSummary:
    """What one matching pass did for one saved search."""

    search_name: str
    not_ruled_out: int = 0
    judged: int = 0
    confirmed_matches: int = 0
    possible_matches: int = 0
    failed_judgments: list[str] = field(default_factory=list)


def match_listings(
    storage: Storage,
    judge: BusinessTypeJudge,
    saved_searches: list[SavedSearch],
    gazetteer: Gazetteer,
    approximate_margin_miles: float,
    report: Callable[[str], None] = lambda message: None,
) -> list[MatchSummary]:
    """Match every stored listing against each saved search and store the matches."""
    current_year = datetime.now(UTC).year
    listings = storage.load_listings()
    summaries = []

    for saved_search in saved_searches:
        summary = MatchSummary(search_name=saved_search.name)
        area = search_areas_for([saved_search], gazetteer)[0]

        checks = {}
        for listing in listings:
            within_reach = within_search_area(locate(listing.fields, gazetteer), area, approximate_margin_miles)
            check = check_criteria(listing.fields, saved_search, within_reach, current_year)
            if not check.ruled_out:
                checks[listing.id] = (listing, check)
        summary.not_ruled_out = len(checks)

        judgments = storage.load_judgments(saved_search.id)
        candidates = [listing for listing, _ in checks.values()]
        needing = listings_needing_judgment(candidates, judgments, saved_search, judge.version)
        report(f"{saved_search.name}: {len(checks)} listings pass the number checks; judging business type for {len(needing)}…")
        fingerprint = business_criteria_fingerprint(saved_search, judge.version)
        for listing in needing:
            try:
                answer = judge.judge(listing, saved_search)
            except ExtractionError as error:
                summary.failed_judgments.append(f"{listing.url}: {error}")
                continue
            judgment = BusinessTypeJudgment(
                saved_search_id=saved_search.id,
                listing_id=listing.id,
                fits=answer.fits,
                matched_type=answer.matched_type,
                reason=answer.reason,
                criteria_fingerprint=fingerprint,
            )
            storage.save_judgment(judgment)
            judgments[listing.id] = judgment
            summary.judged += 1

        still_matching_listing_ids = set()
        for listing, check in checks.values():
            match = decide_match(listing, check, judgments.get(listing.id), saved_search)
            if match is None:
                continue
            storage.save_match(match)
            still_matching_listing_ids.add(listing.id)
            if match.is_confirmed:
                summary.confirmed_matches += 1
            else:
                summary.possible_matches += 1

        storage.remove_unsent_matches_except(saved_search.id, still_matching_listing_ids)
        report(f"  {summary.confirmed_matches} confirmed matches, {summary.possible_matches} possible matches")
        summaries.append(summary)
    return summaries
