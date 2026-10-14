"""Tests for recognizing the same business listed on two different sites."""

from uuid import uuid4

import pytest
from faker import Faker
from hypothesis import given
from hypothesis import strategies as st

from business_finder.duplicates import group_duplicates, looks_like_same_business, plan_announcements
from business_finder.models import Listing, ListingFields, ListingSource, Match

fake = Faker()


def make_listing(title: str, source: ListingSource = ListingSource.BIZBEN, asking_price: int | None = None) -> Listing:
    return Listing(
        source=source,
        source_listing_id=str(fake.random_number(digits=8)),
        url=fake.url(),
        title=title,
        raw_text="Listing text",
        fields=ListingFields(asking_price=asking_price),
    )


BROKER = ListingSource.SANTA_ROSA_BUSINESS_AND_COMMERCIAL


def test_duplicates_are_grouped_and_everything_else_stays_on_its_own():
    sonoma_on_broker = make_listing("Restaurant and Bar in Downtown Sonoma for Sale", BROKER, 350_000)
    sonoma_on_bizben = make_listing("Restaurant and Bar in Downtown Sonoma for Sale", ListingSource.BIZBEN, 350_000)
    locksmith = make_listing("Sonoma County Locksmith", ListingSource.BIZBEN, 149_000)

    groups = group_duplicates([sonoma_on_broker, locksmith, sonoma_on_bizben])

    assert groups == [[sonoma_on_broker, sonoma_on_bizben], [locksmith]]


# --- Deciding what to announce ----------------------------------------------------------


def make_match(listing: Listing, is_confirmed: bool = True) -> Match:
    return Match(saved_search_id=uuid4(), listing_id=listing.id, match_summary="Reason.", is_confirmed=is_confirmed)


def test_duplicates_this_week_become_one_entry_led_by_the_confirmed_match():
    possible_on_broker = make_listing("Well-Established Landscaping and Maintenance Business", BROKER, 300_000)
    confirmed_on_bizben = make_listing("Well Established Landscaping & Maintenance Business", ListingSource.BIZBEN, 300_000)
    unrelated = make_listing("Sonoma County Locksmith", ListingSource.BIZBEN, 149_000)
    unsent = [
        (make_match(possible_on_broker, is_confirmed=False), possible_on_broker),
        (make_match(confirmed_on_bizben), confirmed_on_bizben),
        (make_match(unrelated), unrelated),
    ]

    plan = plan_announcements(unsent, already_sent_listings=[])

    assert [entry.listing for entry in plan.entries] == [confirmed_on_bizben, unrelated]
    assert plan.entries[0].also_listed == [possible_on_broker]
    assert plan.held_back == []


def test_a_business_already_announced_from_another_site_is_held_back():
    sent_last_week = make_listing("Restaurant and Bar in Downtown Sonoma for Sale", ListingSource.BIZBEN, 350_000)
    same_business_new_site = make_listing("Restaurant and Bar in Downtown Sonoma for Sale", BROKER, 350_000)
    match = make_match(same_business_new_site)

    plan = plan_announcements([(match, same_business_new_site)], already_sent_listings=[sent_last_week])

    assert plan.entries == []
    assert plan.held_back == [match]


# --- Deciding from facts -----------------------------------------------------------------


def make_listing_with(source: ListingSource, title: str = "Business for sale", raw_text: str = "Listing text", **facts) -> Listing:
    """A listing with the given facts; anything not given is unknown."""
    return Listing(
        source=source,
        source_listing_id=str(fake.random_number(digits=8)),
        url=fake.url(),
        title=title,
        raw_text=raw_text,
        fields=ListingFields(**facts),
    )


ALERT_EMAIL = ListingSource.BIZBUYSELL
SANTA_ROSA = ListingSource.SANTA_ROSA_BUSINESS_AND_COMMERCIAL


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            make_listing_with(SANTA_ROSA, "Cabinet and Countertop Fabricator", raw_text="Internal ID: MN2026-7"),
            make_listing_with(ALERT_EMAIL, "Highly profitable No. Cal. fabricator", raw_text="Ref MN2026-7. Contact the broker."),
        ),
        (
            make_listing_with(SANTA_ROSA, "Profitable Commercial Cleaning Company", gross_revenue=1_247_316, county="Sonoma", state="CA"),
            make_listing_with(ListingSource.BIZBEN, "Janitorial Business - Recurring Contracts", gross_revenue=1_247_316, state="CA"),
        ),
        (
            make_listing_with(ListingSource.BIZBEN, "Established Rain Gutter Installation Company in Washoe County", asking_price=450_000),
            make_listing_with(ALERT_EMAIL, "Established Rain Gutter Installation Company in Washoe Cou…"),
        ),
        (
            make_listing_with(SANTA_ROSA, "Well-Established Landscaping and Maintenance Business", asking_price=300_000),
            make_listing_with(ListingSource.BIZBEN, "Well Established Landscaping & Maintenance Business for Sale", asking_price=300_000),
        ),
        (
            make_listing_with(ListingSource.BIZBEN, "Residential Cleaning Company - Absentee Owner", county="Marin", year_established=2009),
            make_listing_with(ALERT_EMAIL, "Absentee-Owned Residential Cleaning Company", county="Marin", year_established=2009),
        ),
    ],
    ids=[
        "shared listing number, different titles",
        "same exact revenue, different titles",
        "shortened title in an email, no price in the email",
        "similar titles and the same price",
        "rewritten title, same county and founding year, no prices",
    ],
)
def test_the_same_business_is_recognized_across_sources(first, second):
    assert looks_like_same_business(first, second)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            make_listing_with(ListingSource.BIZBEN, "Restaurant for Sale", asking_price=250_000, city="Petaluma", state="CA"),
            make_listing_with(SANTA_ROSA, "Restaurant for Sale", asking_price=250_000, city="Santa Rosa", state="CA"),
        ),
        (
            make_listing_with(ListingSource.BIZBEN, "Wonderful Restaurant in Great Downtown Petaluma Location", asking_price=250_000),
            make_listing_with(ALERT_EMAIL, "Asian Restaurant for sale", asking_price=250_000),
        ),
        (
            make_listing_with(ListingSource.BIZBEN, "Commercial Cleaning Company", gross_revenue=1_247_316, county="Sonoma", state="CA"),
            make_listing_with(ALERT_EMAIL, "Commercial Cleaning Company", gross_revenue=1_247_316, county="Clark", state="NV"),
        ),
        (
            make_listing_with(ListingSource.BIZBEN, "Established Pool Service Route", asking_price=400_000, year_established=1998),
            make_listing_with(ALERT_EMAIL, "Established Pool Service Route", asking_price=400_000, year_established=2015),
        ),
        (
            make_listing_with(ListingSource.BIZBEN, "Profitable Yard Care Company", asking_price=450_000),
            make_listing_with(ALERT_EMAIL, "Profitable Yard Care Company", asking_price=900_000),
        ),
    ],
    ids=[
        "same generic title and price, different cities",
        "same price, unrelated titles",
        "same title and revenue, different state",
        "same title and price, founding years far apart",
        "same title, prices far apart",
    ],
)
def test_different_businesses_are_kept_apart(first, second):
    assert not looks_like_same_business(first, second)


counties = st.sampled_from([None, "Sonoma", "Marin", "Washoe", "Sacramento"])
amounts = st.one_of(st.none(), st.integers(min_value=10_000, max_value=5_000_000))
years = st.one_of(st.none(), st.integers(min_value=1950, max_value=2025))
title_words = st.lists(st.sampled_from(["profitable", "cleaning", "yard", "care", "company", "established", "route", "pool", "clinic", "north", "bay"]), min_size=1, max_size=8)


@given(
    first_title=title_words, second_title=title_words, first_price=amounts, second_price=amounts,
    first_revenue=amounts, second_revenue=amounts, first_county=counties, second_county=counties,
    first_year=years, second_year=years,
)
def test_being_the_same_business_works_the_same_in_both_directions(
    first_title, second_title, first_price, second_price, first_revenue, second_revenue, first_county, second_county, first_year, second_year
):
    first = make_listing_with(SANTA_ROSA, " ".join(first_title), asking_price=first_price, gross_revenue=first_revenue, county=first_county, year_established=first_year)
    second = make_listing_with(ALERT_EMAIL, " ".join(second_title), asking_price=second_price, gross_revenue=second_revenue, county=second_county, year_established=second_year)

    assert looks_like_same_business(first, second) == looks_like_same_business(second, first)


@given(title=title_words, price=amounts, county=counties)
def test_two_listings_from_the_same_source_are_never_treated_as_duplicates(title, price, county):
    first = make_listing_with(ALERT_EMAIL, " ".join(title), asking_price=price, county=county)
    second = make_listing_with(ALERT_EMAIL, " ".join(title), asking_price=price, county=county)

    assert not looks_like_same_business(first, second)


@given(price=amounts, county=counties, year=years)
def test_conflicting_locations_always_keep_listings_apart(price, county, year):
    first = make_listing_with(SANTA_ROSA, "Established Residential Cleaning Company in the North Bay", asking_price=price, county=county, state="CA", year_established=year)
    second = make_listing_with(ALERT_EMAIL, "Established Residential Cleaning Company in the North Bay", asking_price=price, county=county, state="NV", year_established=year)

    assert not looks_like_same_business(first, second)


@pytest.mark.parametrize(
    ("first_text", "second_text"),
    [
        ("Includes refrigeration equipment and a walk-in cooler.", "New refrigeration units installed last year."),
        ("References available on request.", "Excellent references from repeat clients."),
        ("Internal ID: GROOM", "Ref: GROOM"),
    ],
    ids=["ordinary words starting with ref", "the word references", "codes without a digit"],
)
def test_ordinary_words_are_not_mistaken_for_shared_listing_numbers(first_text, second_text):
    first = make_listing_with(SANTA_ROSA, "Pet Grooming Salon", raw_text=first_text)
    second = make_listing_with(ALERT_EMAIL, "Commercial Bakery", raw_text=second_text)

    assert not looks_like_same_business(first, second)


@pytest.mark.parametrize(
    "differing_fact",
    [
        {"year_established": (1996, 1989)},
        {"gross_revenue": (825_000, 540_000)},
        {"seller_discretionary_earnings": (398_000, 150_000)},
    ],
    ids=["founding years counted differently", "revenue from different years", "a misread SDE"],
)
def test_one_soft_disagreement_does_not_hide_a_business_that_otherwise_agrees(differing_fact):
    (name, (first_value, second_value)), = differing_fact.items()
    shared = {"asking_price": 450_000, "county": "Marin", "state": "CA"}
    first = make_listing_with(SANTA_ROSA, "Landscape Design, Construction & Maintenance Company", **shared, **{name: first_value})
    second = make_listing_with(ListingSource.BIZBEN, "Established Landscape Design, Construction & Maintenance Company", **shared, **{name: second_value})

    assert looks_like_same_business(first, second)
