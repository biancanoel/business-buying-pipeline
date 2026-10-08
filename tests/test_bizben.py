"""Tests for the BizBen adapter. All pages are built from fake data; nothing touches the network."""

import json

import pytest

from business_finder.models import ListingSource
from business_finder.sources.bizben import (
    BizBenPageFormatError,
    listing_from_post,
    parse_dollar_amount,
    parse_results_page,
    parse_year_established,
)


def make_post(**overrides) -> dict:
    """A fake BizBen post shaped like the real ones, with every common field."""
    post = {
        "entityId": "#POST#1000001",
        "urlPath": "fake-cleaning-company-for-sale-1000001",
        "title": "Established Residential Cleaning Company",
        "status": "active",
        "locationVisibility": "showCityCountyState",
        "city": "Petaluma",
        "county": "Sonoma",
        "state": "California",
        "businessTypes": ["House Cleaning, Maid Service"],
        "businessCategory": "Service",
        "askingPrice": 850000,
        "revenue": "1250000",
        "revenueInt": 0,
        "cashFlow": 300000,
        "adjustedNet": "300,000",
        "down": "Call/Email",
        "establishedYear": "2012",
        "employees": "14",
        "description": "<p>Recurring clients &amp; trained crews.</p><p>Owner retiring.</p>",
    }
    post.update(overrides)
    return post


# --- Reading a results page ------------------------------------------------


def test_parse_results_page_fails_loudly_when_layout_changes():
    with pytest.raises(BizBenPageFormatError):
        parse_results_page("<html><body>No data here</body></html>")


# --- Turning a post into a Listing -----------------------------------------


def test_listing_from_post_maps_structured_fields():
    listing = listing_from_post(make_post())

    assert listing.source == ListingSource.BIZBEN
    assert listing.source_listing_id == "1000001"
    assert listing.url == "https://www.bizben.com/business-for-sale/fake-cleaning-company-for-sale-1000001"
    assert listing.fields.business_type == "House Cleaning, Maid Service"
    assert listing.fields.asking_price == 850_000
    assert listing.fields.gross_revenue == 1_250_000
    assert listing.fields.seller_discretionary_earnings == 300_000
    assert listing.fields.year_established == 2012
    assert listing.fields.employee_count == 14
    assert (listing.fields.city, listing.fields.county, listing.fields.state) == ("Petaluma", "Sonoma", "CA")


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        (450000, 450_000),
        ("633,484", 633_484),
        (0, None),
        ("Call/Email", None),
        (None, None),
    ],
)
def test_parse_dollar_amount(raw_value, expected):
    assert parse_dollar_amount(raw_value) == expected


def test_zero_placeholders_become_unknown_not_zero():
    listing = listing_from_post(make_post(revenue="0", revenueInt=0, cashFlow=0, adjustedNet="Call/Email"))

    assert listing.fields.gross_revenue is None
    assert listing.fields.seller_discretionary_earnings is None


def test_falls_back_to_adjusted_net_when_cash_flow_is_blank():
    listing = listing_from_post(make_post(cashFlow=0, adjustedNet="186,626"))

    assert listing.fields.seller_discretionary_earnings == 186_626


def test_hidden_asking_price_is_not_used():
    listing = listing_from_post(make_post(hideAskingPrice=True))

    assert listing.fields.asking_price is None


def test_implausible_year_established_is_dropped():
    listing = listing_from_post(make_post(establishedYear="3020"))

    assert listing.fields.year_established is None


@pytest.mark.parametrize(
    ("raw_value", "expected_year"),
    [("2009", 2009), ("5", 2021), ("0", None), ("Call", None)],
)
def test_parse_year_established_reads_years_and_counts(raw_value, expected_year):
    assert parse_year_established(raw_value, current_year=2026) == expected_year


def test_county_only_visibility_hides_the_city():
    listing = listing_from_post(make_post(locationVisibility="showCountyState"))

    assert listing.fields.city is None
    assert listing.fields.county == "Sonoma"


def test_state_only_visibility_hides_city_and_county():
    listing = listing_from_post(make_post(locationVisibility="showState"))

    assert listing.fields.city is None
    assert listing.fields.county is None
    assert listing.fields.state == "CA"


def test_county_name_is_normalized():
    listing = listing_from_post(make_post(county="Sonoma County"))

    assert listing.fields.county == "Sonoma"


def test_business_category_is_used_when_types_are_missing():
    post = make_post(businessCategory=["Lawn/Landscaping"])
    del post["businessTypes"]

    listing = listing_from_post(post)

    assert listing.fields.business_type == "Lawn/Landscaping"


# --- Text rows and references ------------------------------------------------


def test_page_without_a_results_object_is_rejected():
    payload = '5:{"categoryPageResult":"$7"}'
    html = f"<script>self.__next_f.push({json.dumps([1, payload])})</script>"

    with pytest.raises(BizBenPageFormatError, match="not a results page"):
        parse_results_page(html)


# --- Several states ----------------------------------------------------------


def test_multi_state_listing_keeps_every_state():
    post = make_post(states=["California", "Oregon"])
    del post["state"]

    listing = listing_from_post(post)

    assert listing.fields.state is None
    assert listing.fields.states == ["CA", "OR"]


