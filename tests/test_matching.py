"""Tests for checking a listing's stated facts against a saved search's criteria."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from business_finder.matching import (
    CriterionOutcome,
    business_criteria_fingerprint,
    check_criteria,
    decide_match,
    listings_needing_judgment,
)
from business_finder.models import BusinessTypeJudgment, Listing, ListingFields, ListingSource, SavedSearch

CURRENT_YEAR = 2026

SEARCH = SavedSearch(
    name="Test search",
    business_types=["residential cleaning"],
    target_city="Petaluma",
    target_state="CA",
    minimum_revenue=1_000_000,
    minimum_seller_discretionary_earnings=250_000,
    asking_price_max=1_200_000,
    minimum_years_in_business=5,
    max_distance_miles=250,
)

MEETS_EVERYTHING = ListingFields(
    asking_price=900_000,
    gross_revenue=1_500_000,
    seller_discretionary_earnings=300_000,
    year_established=2010,
)

amounts = st.integers(min_value=1, max_value=20_000_000)


@given(revenue=amounts)
def test_revenue_passes_exactly_when_it_reaches_the_minimum(revenue):
    check = check_criteria(MEETS_EVERYTHING.model_copy(update={"gross_revenue": revenue}), SEARCH, within_reach=True, current_year=CURRENT_YEAR)

    expected = CriterionOutcome.PASSES if revenue >= SEARCH.minimum_revenue else CriterionOutcome.FAILS
    assert check.outcomes["gross_revenue"] == expected


@given(sde=amounts)
def test_sde_passes_exactly_when_it_reaches_the_minimum(sde):
    fields = MEETS_EVERYTHING.model_copy(update={"seller_discretionary_earnings": sde})

    check = check_criteria(fields, SEARCH, within_reach=True, current_year=CURRENT_YEAR)

    expected = CriterionOutcome.PASSES if sde >= SEARCH.minimum_seller_discretionary_earnings else CriterionOutcome.FAILS
    assert check.outcomes["seller_discretionary_earnings"] == expected


@given(price=amounts)
def test_asking_price_passes_exactly_when_it_is_within_the_maximum(price):
    check = check_criteria(MEETS_EVERYTHING.model_copy(update={"asking_price": price}), SEARCH, within_reach=True, current_year=CURRENT_YEAR)

    expected = CriterionOutcome.PASSES if price <= SEARCH.asking_price_max else CriterionOutcome.FAILS
    assert check.outcomes["asking_price"] == expected


@given(year=st.integers(min_value=1800, max_value=CURRENT_YEAR))
def test_business_age_passes_exactly_when_it_has_the_minimum_years(year):
    check = check_criteria(MEETS_EVERYTHING.model_copy(update={"year_established": year}), SEARCH, within_reach=True, current_year=CURRENT_YEAR)

    expected = CriterionOutcome.PASSES if CURRENT_YEAR - year >= SEARCH.minimum_years_in_business else CriterionOutcome.FAILS
    assert check.outcomes["years_in_business"] == expected


@pytest.mark.parametrize(("within_reach", "expected"), [(True, CriterionOutcome.PASSES), (False, CriterionOutcome.FAILS), (None, CriterionOutcome.NOT_STATED)])
def test_distance_follows_whether_the_listing_is_within_reach(within_reach, expected):
    check = check_criteria(MEETS_EVERYTHING, SEARCH, within_reach=within_reach, current_year=CURRENT_YEAR)

    assert check.outcomes["distance"] == expected


def test_a_listing_meeting_every_criterion_is_a_confirmed_match():
    check = check_criteria(MEETS_EVERYTHING, SEARCH, within_reach=True, current_year=CURRENT_YEAR)

    assert not check.ruled_out
    assert check.is_confirmed
    assert check.missing_facts == []


@pytest.mark.parametrize("missing", ["asking_price", "gross_revenue", "seller_discretionary_earnings", "year_established"])
def test_a_missing_fact_makes_a_possible_match_naming_what_is_missing(missing):
    check = check_criteria(MEETS_EVERYTHING.model_copy(update={missing: None}), SEARCH, within_reach=True, current_year=CURRENT_YEAR)

    assert not check.ruled_out
    assert not check.is_confirmed
    assert len(check.missing_facts) == 1


def test_any_failed_criterion_rules_a_listing_out_even_when_other_facts_are_missing():
    fields = ListingFields(asking_price=2_000_000)

    check = check_criteria(fields, SEARCH, within_reach=None, current_year=CURRENT_YEAR)

    assert check.ruled_out


def test_a_search_without_limits_accepts_listings_that_state_nothing():
    open_search = SavedSearch(name="Open search", business_types=["any"], target_city="Petaluma", target_state="CA")

    check = check_criteria(ListingFields(), open_search, within_reach=None, current_year=CURRENT_YEAR)

    assert not check.ruled_out
    assert check.is_confirmed



# --- Business-type judgments and match decisions ------------------------------------


def make_listing(source_listing_id: str, fields: ListingFields = MEETS_EVERYTHING) -> Listing:
    return Listing(
        source=ListingSource.BIZBEN,
        source_listing_id=source_listing_id,
        url=f"https://example.com/{source_listing_id}",
        raw_text="Fake listing",
        fields=fields,
    )


def make_judgment(listing: Listing, fits: bool, fingerprint: str | None = None) -> BusinessTypeJudgment:
    return BusinessTypeJudgment(
        saved_search_id=SEARCH.id,
        listing_id=listing.id,
        fits=fits,
        reason="Residential cleaning company with recurring clients.",
        criteria_fingerprint=fingerprint or business_criteria_fingerprint(SEARCH),
    )


@pytest.mark.parametrize(
    "change",
    [{"business_types": ["residential cleaning", "pool cleaning"]}, {"business_type_guidance": "No franchises."}],
)
def test_fingerprint_changes_when_business_types_or_rules_change(change):
    assert business_criteria_fingerprint(SEARCH.model_copy(update=change)) != business_criteria_fingerprint(SEARCH)


def test_fingerprint_ignores_criteria_that_do_not_affect_business_type():
    changed = SEARCH.model_copy(update={"minimum_revenue": 2_000_000, "max_distance_miles": 100})

    assert business_criteria_fingerprint(changed) == business_criteria_fingerprint(SEARCH)


def test_claude_judges_new_listings_and_ones_judged_under_old_criteria_only():
    never_judged = make_listing("never-judged")
    judged_now = make_listing("judged-now")
    judged_under_old_criteria = make_listing("judged-before")
    judgments = {
        judged_now.id: make_judgment(judged_now, fits=True),
        judged_under_old_criteria.id: make_judgment(judged_under_old_criteria, fits=True, fingerprint="old"),
    }

    needing = listings_needing_judgment([never_judged, judged_now, judged_under_old_criteria], judgments, SEARCH)

    assert [listing.source_listing_id for listing in needing] == ["never-judged", "judged-before"]


def test_a_listing_meeting_the_numbers_with_a_fitting_business_type_is_a_confirmed_match():
    listing = make_listing("fits")
    check = check_criteria(listing.fields, SEARCH, within_reach=True, current_year=CURRENT_YEAR)

    match = decide_match(listing, check, make_judgment(listing, fits=True), SEARCH)

    assert match.is_confirmed
    assert match.missing_facts == []
    assert match.match_summary == "Residential cleaning company with recurring clients."
    assert (match.saved_search_id, match.listing_id) == (SEARCH.id, listing.id)


def test_a_fitting_listing_with_missing_facts_is_a_possible_match_listing_them():
    listing = make_listing("missing-sde", MEETS_EVERYTHING.model_copy(update={"seller_discretionary_earnings": None}))
    check = check_criteria(listing.fields, SEARCH, within_reach=True, current_year=CURRENT_YEAR)

    match = decide_match(listing, check, make_judgment(listing, fits=True), SEARCH)

    assert not match.is_confirmed
    assert match.missing_facts == ["seller_discretionary_earnings"]


@pytest.mark.parametrize(
    ("fields", "fits"),
    [
        (MEETS_EVERYTHING, False),
        (MEETS_EVERYTHING.model_copy(update={"gross_revenue": 400_000}), True),
    ],
    ids=["wrong business type", "fails a number"],
)
def test_no_match_when_the_business_type_does_not_fit_or_a_number_fails(fields, fits):
    listing = make_listing("no-match", fields)
    check = check_criteria(listing.fields, SEARCH, within_reach=True, current_year=CURRENT_YEAR)

    assert decide_match(listing, check, make_judgment(listing, fits=fits), SEARCH) is None


def test_no_match_before_claude_has_judged_the_business_type():
    listing = make_listing("not-judged")
    check = check_criteria(listing.fields, SEARCH, within_reach=True, current_year=CURRENT_YEAR)

    assert decide_match(listing, check, None, SEARCH) is None


@given(revenue=amounts, sde=amounts)
def test_a_listing_whose_sde_exceeds_its_revenue_is_ruled_out(revenue, sde):
    fields = MEETS_EVERYTHING.model_copy(update={"gross_revenue": revenue, "seller_discretionary_earnings": sde})

    check = check_criteria(fields, SEARCH, within_reach=True, current_year=CURRENT_YEAR)

    if sde > revenue:
        assert check.ruled_out


@pytest.mark.parametrize("judge_versions", [("version-a", "version-b")])
def test_fingerprint_changes_when_the_judge_changes(judge_versions):
    first, second = judge_versions

    assert business_criteria_fingerprint(SEARCH, judge_version=first) != business_criteria_fingerprint(SEARCH, judge_version=second)


def test_listings_judged_by_an_older_judge_are_judged_again():
    listing = make_listing("judged-by-old-judge")
    judgments = {listing.id: make_judgment(listing, fits=True, fingerprint=business_criteria_fingerprint(SEARCH, judge_version="old"))}

    needing = listings_needing_judgment([listing], judgments, SEARCH, judge_version="new")

    assert needing == [listing]
