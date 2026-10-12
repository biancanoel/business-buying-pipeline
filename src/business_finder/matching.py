"""Checking stored listings against a saved search's criteria.

Each numeric or location criterion comes out as passes, fails or not stated:

- Any criterion that fails rules the listing out.
- If every criterion passes, the listing is a confirmed match (subject to
  the business-type check).
- If none fails but some facts are not stated, the listing is a possible
  match, labeled with what is missing. Brokers often hold back financials
  until an NDA, so these are worth a look rather than dropped.

Listings whose figures are impossible (SDE larger than revenue) are ruled
out: a listing that can't state its own numbers isn't worth pursuing.

Business type is not checked here: wording varies too much for plain code,
so a model judges it separately, only for listings these checks keep.
"""

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from business_finder.models import BusinessTypeJudgment, Listing, ListingFields, Match, SavedSearch


class CriterionOutcome(StrEnum):
    PASSES = "passes"
    FAILS = "fails"
    NOT_STATED = "not_stated"


@dataclass
class CriteriaCheck:
    """How one listing fares against one saved search's numeric and location criteria."""

    outcomes: dict[str, CriterionOutcome]

    @property
    def ruled_out(self) -> bool:
        return CriterionOutcome.FAILS in self.outcomes.values()

    @property
    def missing_facts(self) -> list[str]:
        return [name for name, outcome in self.outcomes.items() if outcome == CriterionOutcome.NOT_STATED]

    @property
    def is_confirmed(self) -> bool:
        return not self.ruled_out and not self.missing_facts


def minimum_outcome(value: int | None, minimum: int | None) -> CriterionOutcome:
    """Check a value against a minimum the search requires.

    A search with no minimum for this criterion accepts anything, stated or not.
    """
    if minimum is None:
        return CriterionOutcome.PASSES
    if value is None:
        return CriterionOutcome.NOT_STATED
    return CriterionOutcome.PASSES if value >= minimum else CriterionOutcome.FAILS


def asking_price_outcome(asking_price: int | None, saved_search: SavedSearch) -> CriterionOutcome:
    """Check the asking price against the search's price range (either end may be open)."""
    price_min = saved_search.asking_price_min
    price_max = saved_search.asking_price_max
    if price_min is None and price_max is None:
        return CriterionOutcome.PASSES
    if asking_price is None:
        return CriterionOutcome.NOT_STATED
    above_min = price_min is None or asking_price >= price_min
    below_max = price_max is None or asking_price <= price_max
    return CriterionOutcome.PASSES if above_min and below_max else CriterionOutcome.FAILS


def years_in_business(year_established: int | None, current_year: int) -> int | None:
    if year_established is None:
        return None
    return current_year - year_established


def figures_consistent_outcome(fields: ListingFields) -> CriterionOutcome:
    """Fail listings whose SDE is larger than their revenue, which is impossible.

    Passes when either figure is missing: the other checks already handle
    missing figures.
    """
    revenue = fields.gross_revenue
    sde = fields.seller_discretionary_earnings
    if revenue is None or sde is None:
        return CriterionOutcome.PASSES
    return CriterionOutcome.PASSES if sde <= revenue else CriterionOutcome.FAILS


def distance_outcome(within_reach: bool | None, saved_search: SavedSearch) -> CriterionOutcome:
    """`within_reach` comes from geography: True, False, or None when the location is unknown."""
    if saved_search.max_distance_miles is None:
        return CriterionOutcome.PASSES
    if within_reach is None:
        return CriterionOutcome.NOT_STATED
    return CriterionOutcome.PASSES if within_reach else CriterionOutcome.FAILS


def check_criteria(
    fields: ListingFields,
    saved_search: SavedSearch,
    within_reach: bool | None,
    current_year: int,
) -> CriteriaCheck:
    """Check a listing's stated facts against every numeric and location criterion."""
    return CriteriaCheck(
        outcomes={
            "asking_price": asking_price_outcome(fields.asking_price, saved_search),
            "gross_revenue": minimum_outcome(fields.gross_revenue, saved_search.minimum_revenue),
            "seller_discretionary_earnings": minimum_outcome(
                fields.seller_discretionary_earnings, saved_search.minimum_seller_discretionary_earnings
            ),
            "years_in_business": minimum_outcome(
                years_in_business(fields.year_established, current_year), saved_search.minimum_years_in_business
            ),
            "distance": distance_outcome(within_reach, saved_search),
            "figures_consistent": figures_consistent_outcome(fields),
        }
    )


# --- Business type --------------------------------------------------------------------


def business_criteria_fingerprint(saved_search: SavedSearch, judge_version: str = "") -> str:
    """Return a short fingerprint of everything business-type judgments depend on.

    That is the search's business types and their guidance, plus the
    version of the judge (its instructions and answer schema), so improving
    the judge re-judges past listings too. Changing a revenue or distance
    limit does not require judging business types again.
    """
    criteria = {
        "business_types": saved_search.business_types,
        "guidance": saved_search.business_type_guidance,
        "judge_version": judge_version,
    }
    return hashlib.sha256(json.dumps(criteria, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def listings_needing_judgment(
    listings: list[Listing],
    judgments: dict[UUID, BusinessTypeJudgment],
    saved_search: SavedSearch,
    judge_version: str = "",
) -> list[Listing]:
    """Choose listings to send for a business-type judgment.

    A listing needs one if it has never been judged for this search, or was
    judged under business types, rules or a judge version that have since changed.
    """
    fingerprint = business_criteria_fingerprint(saved_search, judge_version)
    needing = []
    for listing in listings:
        judgment = judgments.get(listing.id)
        if judgment is None or judgment.criteria_fingerprint != fingerprint:
            needing.append(listing)
    return needing


def decide_match(
    listing: Listing,
    check: CriteriaCheck,
    judgment: BusinessTypeJudgment | None,
    saved_search: SavedSearch,
) -> Match | None:
    """Return the match to record, or None.

    A listing matches when no criterion rules it out and its business type
    was judged to fit. It is a confirmed match if every fact was stated,
    otherwise a possible match listing what is missing.
    """
    if check.ruled_out or judgment is None or not judgment.fits:
        return None
    return Match(
        saved_search_id=saved_search.id,
        listing_id=listing.id,
        match_summary=judgment.reason,
        is_confirmed=check.is_confirmed,
        missing_facts=check.missing_facts,
    )
