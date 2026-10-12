"""Tests for the weekly Slack message: what each match shows and what a quiet week says."""

import pytest
from faker import Faker

from business_finder.models import Listing, ListingFields, ListingSource, LocationPrecision, Match, SavedSearch
from business_finder.notifiers.slack import MatchDetails, price_against_target, weekly_message

fake = Faker()

SEARCH = SavedSearch(
    name="Test search: Petaluma area",
    business_types=["physical therapy clinic", "residential cleaning"],
    target_city="Petaluma",
    target_state="CA",
    asking_price_max=1_200_000,
    asking_price_target=1_000_000,
)


def make_details(
    is_confirmed: bool = True,
    missing_facts: list[str] | None = None,
    miles_away: float | None = 63.4,
    precision: LocationPrecision = LocationPrecision.EXACT,
    **field_overrides,
) -> MatchDetails:
    fields = {
        "city": fake.city(),
        "county": fake.last_name(),
        "state": "CA",
        "asking_price": 1_100_000,
        "gross_revenue": 1_011_283,
        "seller_discretionary_earnings": 263_694,
        "year_established": 2006,
    }
    fields.update(field_overrides)
    listing = Listing(
        source=ListingSource.BIZBEN,
        source_listing_id=str(fake.random_number(digits=7)),
        url=fake.url(),
        title=fake.catch_phrase(),
        raw_text="Listing text",
        fields=ListingFields(**fields),
    )
    match = Match(
        saved_search_id=SEARCH.id,
        listing_id=listing.id,
        match_summary="Not shown in Slack.",
        is_confirmed=is_confirmed,
        missing_facts=missing_facts or [],
    )
    return MatchDetails(
        match=match, listing=listing, matched_type="physical therapy clinic", miles_away=miles_away, precision=precision
    )


def message_text(payload: dict) -> str:
    """All the text a person would read in the message."""
    return "\n".join(block["text"]["text"] for block in payload["blocks"] if "text" in block)


@pytest.mark.parametrize(
    ("asking_price", "expected"),
    [
        (1_100_000, "$100K over the $1M target, within the $1.2M max"),
        (850_000, "$150K under the $1M target"),
        (1_000_000, "at the $1M target"),
    ],
)
def test_asking_price_is_shown_against_the_target(asking_price, expected):
    assert price_against_target(asking_price, SEARCH) == expected


def test_a_confirmed_match_shows_the_agreed_facts():
    details = make_details()
    listing = details.listing

    text = message_text(weekly_message(SEARCH, [details], target_city="Petaluma"))

    assert "Confirmed match" in text
    assert "Physical therapy clinic" in text
    assert f"<{listing.url}|{listing.title}>" in text
    assert listing.fields.city in text
    assert "63 miles from Petaluma" in text
    assert "$1,100,000" in text
    assert "$100K over the $1M target, within the $1.2M max" in text
    assert "Revenue $1,011,283" in text
    assert "SDE $263,694" in text
    assert "Established 2006" in text
    assert str(details.match.listing_id) in text


def test_the_why_line_is_not_shown():
    text = message_text(weekly_message(SEARCH, [make_details()], target_city="Petaluma"))

    assert "Why" not in text
    assert "Not shown in Slack." not in text


def test_a_possible_match_says_so_and_names_what_to_ask_the_broker():
    details = make_details(
        is_confirmed=False,
        missing_facts=["gross_revenue", "seller_discretionary_earnings"],
        gross_revenue=None,
        seller_discretionary_earnings=None,
    )

    text = message_text(weekly_message(SEARCH, [details], target_city="Petaluma"))

    assert "Possible match" in text
    assert "Not stated: revenue, SDE. Ask the broker." in text


@pytest.mark.parametrize(
    ("precision", "miles_away", "expected"),
    [
        (LocationPrecision.APPROXIMATE, 120.6, "about 121 miles from Petaluma"),
        (LocationPrecision.UNKNOWN, None, "distance unknown"),
    ],
)
def test_vague_locations_are_labeled_as_such(precision, miles_away, expected):
    text = message_text(weekly_message(SEARCH, [make_details(precision=precision, miles_away=miles_away)], target_city="Petaluma"))

    assert expected in text


def test_a_week_with_matches_has_one_entry_each_and_counts_them():
    matches = [make_details(), make_details(is_confirmed=False, missing_facts=["seller_discretionary_earnings"])]

    payload = weekly_message(SEARCH, matches, target_city="Petaluma")

    text = message_text(payload)
    assert text.count("Confirmed match") == 1
    assert text.count("Possible match") == 1
    assert "1 confirmed and 1 possible" in payload["text"]


def test_a_quiet_week_posts_a_no_new_matches_note():
    payload = weekly_message(SEARCH, [], target_city="Petaluma")

    assert "No new matches this week" in message_text(payload)
    assert SEARCH.name in payload["text"]


def test_a_very_busy_week_is_cut_to_fit_slacks_limits():
    payload = weekly_message(SEARCH, [make_details() for _ in range(30)], target_city="Petaluma")

    assert len(payload["blocks"]) <= 50
    assert "and 10 more" in message_text(payload)


def test_confirmed_matches_are_listed_before_possible_ones():
    matches = [
        make_details(is_confirmed=False, missing_facts=["seller_discretionary_earnings"]),
        make_details(),
        make_details(is_confirmed=False, missing_facts=["gross_revenue"]),
    ]

    text = message_text(weekly_message(SEARCH, matches, target_city="Petaluma"))

    assert text.index("Confirmed match") < text.index("Possible match")
