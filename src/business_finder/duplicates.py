"""Recognizing the same business listed on more than one source.

Brokers post the same business on their own site, on marketplaces such as
BizBen, and in alert emails, so the pipeline can store one business several
times. All copies are kept (one source sometimes has details another lacks);
this module only decides which listings are the same business, so
notifications show it once.

The decision is made from the listings' facts, so it works the same for any
source, website or email, whatever shape its titles take:

1. Hard conflicts keep two listings apart, always: different states,
   counties or cities, or asking prices more than 10% apart.
2. Some facts settle it on their own: a shared listing or reference number,
   or the same exact, non-round revenue or SDE (such as $1,247,316).
3. Otherwise supporting facts are added up: similar titles, the same asking
   price, the same city, county or region, the same founding year, the same
   round revenue or SDE. Soft conflicts each take a point away: founding
   years more than a year apart, or revenue or SDE more than 10% apart.
   Copies of one business often disagree on these (years counted
   differently, figures from different years, a misread number), so they
   weigh against a match without ruling it out. Three points make the
   same business.

The cases these rules must handle are in tests/test_duplicates.py; add
pairs there when a new source brings new patterns.
"""

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from business_finder.models import Listing, Match

# Supporting points needed to call two listings the same business.
POINTS_FOR_SAME_BUSINESS = 3
# Facts further apart than this (as a share of the larger) are different businesses.
FIGURE_TOLERANCE = 0.10
FOUNDING_YEAR_TOLERANCE = 1
# Title similarity thresholds (0 to 1).
NEARLY_IDENTICAL_TITLES = 0.85
SIMILAR_TITLES = 0.6
# Titles need this many meaningful words to count as evidence at all, so
# generic titles like "Restaurant for Sale" never make a match.
MINIMUM_TITLE_WORDS = 3
# A nearly identical title this specific is enough by itself.
SPECIFIC_TITLE_WORDS = 5

FILLER_WORDS = {"a", "an", "and", "at", "for", "in", "of", "on", "the", "to", "with", "sale", "sell", "selling"}
# "Internal ID: MN2026-7", "Listing #48213", "Ref MN2026-7". The label must be a
# whole word (not "refrigeration") and the number must contain a digit (not "GROOM").
REFERENCE_NUMBER = re.compile(
    r"\b(?:internal\s+id|listing\s+(?:number|no|id)|listing\s*#|ref(?:erence)?(?:\s+(?:number|no))?)\b\.?\s*[:#]?\s*([A-Za-z0-9-]*\d[A-Za-z0-9-]*)",
    re.IGNORECASE,
)
MINIMUM_REFERENCE_LENGTH = 4


# --- Comparing facts ---------------------------------------------------------------------


def title_words(title: str | None) -> list[str]:
    """The meaningful words of a title, lowercase, without filler words or punctuation."""
    return [word for word in re.findall(r"\w+", (title or "").casefold()) if word not in FILLER_WORDS]


def title_similarity(first: Listing, second: Listing) -> float:
    """How alike two titles are, from 0 to 1, by letters and by shared words.

    Letter-by-letter matching catches typos and shortened titles; shared
    words catch rewritten ones ("Absentee-Owned Residential Cleaning Company"
    and "Residential Cleaning Company - Absentee Owner"). The higher score is
    used, and both orders are scored, so the result is the same either way.
    """
    first_words, second_words = title_words(first.title), title_words(second.title)
    if not first_words or not second_words:
        return 0.0
    first_text, second_text = " ".join(first_words), " ".join(second_words)
    by_letters = max(
        SequenceMatcher(None, first_text, second_text).ratio(),
        SequenceMatcher(None, second_text, first_text).ratio(),
    )
    shared = len(set(first_words) & set(second_words))
    by_words = 2 * shared / (len(set(first_words)) + len(set(second_words)))
    return max(by_letters, by_words)


def text_differs(first: str | None, second: str | None) -> bool:
    """True when both are stated and they are not the same place name."""
    if not first or not second:
        return False
    return first.strip().casefold() != second.strip().casefold()


def figures_differ(first: int | None, second: int | None) -> bool:
    """True when both amounts are stated and more than 10% apart."""
    if first is None or second is None:
        return False
    return abs(first - second) > FIGURE_TOLERANCE * max(first, second)


def facts_conflict(first: Listing, second: Listing) -> bool:
    """True when the listings state facts that cannot both be true of one business.

    Only place and asking price count here; see `soft_conflicts` for facts
    that copies of one business often disagree on.
    """
    a, b = first.fields, second.fields
    if text_differs(a.state, b.state) or text_differs(a.county, b.county) or text_differs(a.city, b.city):
        return True
    return figures_differ(a.asking_price, b.asking_price)


def soft_conflicts(first: Listing, second: Listing) -> int:
    """Count disagreements that weigh against a match without ruling it out."""
    a, b = first.fields, second.fields
    count = 0
    if a.year_established is not None and b.year_established is not None:
        if abs(a.year_established - b.year_established) > FOUNDING_YEAR_TOLERANCE:
            count += 1
    if figures_differ(a.gross_revenue, b.gross_revenue):
        count += 1
    if figures_differ(a.seller_discretionary_earnings, b.seller_discretionary_earnings):
        count += 1
    return count


def reference_numbers(listing: Listing) -> set[str]:
    """Listing or reference numbers mentioned in the listing, e.g. "Internal ID: MN2026-7"."""
    numbers = REFERENCE_NUMBER.findall(listing.raw_text or "")
    return {number.upper() for number in numbers if len(number) >= MINIMUM_REFERENCE_LENGTH}


def is_distinctive(amount: int | None) -> bool:
    """A non-round amount ($1,247,316) is practically unique to one business."""
    return amount is not None and amount % 1000 != 0


def settles_it(first: Listing, second: Listing) -> bool:
    """Facts that identify one business on their own."""
    if first.url == second.url or reference_numbers(first) & reference_numbers(second):
        return True
    for amount_a, amount_b in [
        (first.fields.gross_revenue, second.fields.gross_revenue),
        (first.fields.seller_discretionary_earnings, second.fields.seller_discretionary_earnings),
    ]:
        if is_distinctive(amount_a) and amount_a == amount_b:
            return True
    return False


def same_stated(first, second) -> bool:
    """True when both are stated and equal (place names compared without case)."""
    if first is None or second is None:
        return False
    if isinstance(first, str):
        return first.strip().casefold() == str(second).strip().casefold()
    return first == second


def supporting_points(first: Listing, second: Listing) -> int:
    """Add up the facts that agree."""
    points = 0
    shortest_title = min(len(set(title_words(first.title))), len(set(title_words(second.title))))
    if shortest_title >= MINIMUM_TITLE_WORDS:
        similarity = title_similarity(first, second)
        if similarity >= NEARLY_IDENTICAL_TITLES:
            points += POINTS_FOR_SAME_BUSINESS if shortest_title >= SPECIFIC_TITLE_WORDS else 2
        elif similarity >= SIMILAR_TITLES:
            points += 1

    a, b = first.fields, second.fields
    if same_stated(a.asking_price, b.asking_price):
        points += 1
    if same_stated(a.city, b.city) or same_stated(a.county, b.county) or same_stated(a.region, b.region):
        points += 1
    if same_stated(a.year_established, b.year_established):
        points += 1
    if same_stated(a.gross_revenue, b.gross_revenue):
        points += 1
    if same_stated(a.seller_discretionary_earnings, b.seller_discretionary_earnings):
        points += 1
    return points


def looks_like_same_business(first: Listing, second: Listing) -> bool:
    """Decide whether two listings from different sources describe the same business."""
    if first.source == second.source:
        return False
    if facts_conflict(first, second):
        return False
    if settles_it(first, second):
        return True
    return supporting_points(first, second) - soft_conflicts(first, second) >= POINTS_FOR_SAME_BUSINESS


def group_duplicates(listings: list[Listing]) -> list[list[Listing]]:
    """Group listings that are the same business; every other listing is a group of one.

    Groups keep the order the listings came in, ordered by each group's first listing.
    """
    groups: list[list[Listing]] = []
    for listing in listings:
        matching_group = next(
            (group for group in groups if any(looks_like_same_business(listing, member) for member in group)),
            None,
        )
        if matching_group is None:
            groups.append([listing])
        else:
            matching_group.append(listing)
    return groups


# --- Deciding what to announce --------------------------------------------------------------


@dataclass
class AnnouncementEntry:
    """One business to announce, with the same business's listings on other sources."""

    match: Match
    listing: Listing
    also_listed: list[Listing] = field(default_factory=list)


@dataclass
class AnnouncementPlan:
    """What to post this week, and which matches to mark as sent without posting."""

    entries: list[AnnouncementEntry]
    held_back: list[Match]


def plan_announcements(
    unsent: list[tuple[Match, Listing]],
    already_sent_listings: list[Listing],
) -> AnnouncementPlan:
    """Decide which of a search's new matches to announce.

    - A match whose business was already announced from another source is
      held back: it is marked as sent but not posted again.
    - Matches that are the same business become one entry, led by a
      confirmed match if there is one, with the other copies linked.
    """
    held_back = []
    to_announce: list[tuple[Match, Listing]] = []
    for match, listing in unsent:
        if any(looks_like_same_business(listing, sent) for sent in already_sent_listings):
            held_back.append(match)
        else:
            to_announce.append((match, listing))

    match_for_listing = {listing.id: match for match, listing in to_announce}
    entries = []
    for group in group_duplicates([listing for _, listing in to_announce]):
        confirmed = [listing for listing in group if match_for_listing[listing.id].is_confirmed]
        leader = confirmed[0] if confirmed else group[0]
        others = [listing for listing in group if listing is not leader]
        entries.append(AnnouncementEntry(match=match_for_listing[leader.id], listing=leader, also_listed=others))
    return AnnouncementPlan(entries=entries, held_back=held_back)
