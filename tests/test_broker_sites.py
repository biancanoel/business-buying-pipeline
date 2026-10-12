"""Tests for reading broker websites built on the shared listing template.

The pages here are small made-up copies of each site's layout (no real
listing data), so the tests cover both the Santa Rosa and Liberty variants.
"""

import pytest
from faker import Faker

from business_finder.models import ListingSource
from business_finder.sources.broker_sites import (
    LIBERTY_GROUP_OF_NEVADA,
    SANTA_ROSA_BUSINESS_AND_COMMERCIAL,
    listing_from_pages,
    parse_listing_cards,
    parse_listing_page,
    parse_location,
)

fake = Faker()


# --- Made-up pages in each site's layout ---------------------------------------------


def santa_rosa_card(url: str, title: str, industry: str, location: str, internal_id: str, revenue: str, price: str) -> str:
    return f"""
    <div class="listing-box"><div class="row"><div class="col-md-8">
      <div class="listing-title"><a class="hover-underline-animation" href="{url}">{title}</a></div>
      <div><span class="description-name">Industry:</span> <span class="description-value">{industry}</span></div>
      <div><span class="description-name">Location:</span> <span class="description-value">{location}</span></div>
      <div><span class="description-name">Internal ID:</span> <span class="description-value">{internal_id}</span></div>
      <div><span class="description-name">Gross Revenue:</span> <span class="description-value">{revenue}</span></div>
      <div class="asking-price"><span class="price-description-name">Price</span> <span class="price-description-value">{price}</span></div>
      <div class="more-info-button"><a href="{url}">More Info</a></div>
    </div></div></div>"""


def liberty_card(url: str, title: str, industry: str, location: str, internal_id: str, price: str) -> str:
    return f"""
    <div class="col-sm-4"><div class="listingBox"><div class="listingContent">
      <div class="listingTitle"><a href="{url}"><h2>{title}</h2></a></div>
      <div class="listingIndustry"><span class="descriptionName">Industry:</span> <span class="descriptionValue">{industry}</span></div>
      <div class="listingLocation"><span class="descriptionName">Location:</span> <span class="descriptionValue">{location}</span></div>
      <div class="internalID"><span class="descriptionName">Internal ID:</span> <span class="descriptionValue">{internal_id}</span></div>
      <div class="listingPrice"><span class="priceDescriptionValue">{price}</span></div>
      <div class="footerRight"><a href="{url}">&#62;</a></div>
    </div></div></div>"""


def listing_page(asking_price: str, sde: str, grid: list[tuple[str, str]], description: str) -> str:
    """A listing page: summary box with a tooltip, a facts grid, the description, and a print copy of the summary."""
    tooltip = '<span class="su-tooltip"><span class="su-tooltip-content">The total asking price of the business.</span></span>'
    summary = f"""
    <div class="twcSummaryBox"><div class="row">
      <div class="col-sm-3"><span class="twcSummaryBoxName">Asking Price</span></div>
      <div class="col-sm-3"><span class="twcSummaryBoxValue">{asking_price} {tooltip}</span></div>
      <div class="col-sm-3"><span class="twcSummaryBoxName">SDE</span></div>
      <div class="col-sm-3"><span class="twcSummaryBoxValue">{sde}</span></div>
    </div></div>"""
    grid_html = "".join(
        f'<div><span class="descriptionName">{name}</span></div><div><span class="descriptionValue">{value}</span></div>'
        for name, value in grid
    )
    print_copy = summary.replace(asking_price, "$1")
    return f"""<html><body><article>
      {summary}
      <div class="row">{grid_html}</div>
      <div class="messageBoxLeft">Business Description</div>
      <span class="description-name"><p>{description}</p></span>
      <div class="print-version">{print_copy}</div>
    </article></body></html>"""


# --- Listing cards on a site's "businesses for sale" page ----------------------------


def test_santa_rosa_cards_are_read():
    url = "https://santarosabusinessandcommercial.com/listing/fake-cleaning-company/"
    html = "<html><body>" + santa_rosa_card(url, "Fake Cleaning Company", "Home Services", "Sonoma County", "HS2026-1", "$1,250,000", "$900,000") + "</body></html>"

    cards = parse_listing_cards(html, SANTA_ROSA_BUSINESS_AND_COMMERCIAL)

    assert len(cards) == 1
    card = cards[0]
    assert (card.url, card.title, card.internal_id) == (url, "Fake Cleaning Company", "HS2026-1")
    assert card.facts == {"Industry": "Home Services", "Location": "Sonoma County", "Internal ID": "HS2026-1", "Gross Revenue": "$1,250,000"}
    assert card.price == "$900,000"


def test_liberty_cards_are_read():
    titles = [fake.catch_phrase() for _ in range(3)]
    urls = [f"https://www.thelibertygroupofnevada.com/listing/{fake.slug()}/" for _ in titles]
    html = "<html><body>" + "".join(
        liberty_card(url, title, "Cleaning Companies", "Reno, NV", f"ID{number}", "$360,000")
        for number, (url, title) in enumerate(zip(urls, titles))
    ) + "</body></html>"

    cards = parse_listing_cards(html, LIBERTY_GROUP_OF_NEVADA)

    assert [card.title for card in cards] == titles
    assert [card.url for card in cards] == urls
    assert [card.internal_id for card in cards] == ["ID0", "ID1", "ID2"]
    assert all(card.facts["Location"] == "Reno, NV" for card in cards)


# --- A listing's own page ----------------------------------------------------------------


def test_listing_page_reads_summary_grid_and_description_without_tooltips_or_print_copy():
    description = fake.paragraph(nb_sentences=6)
    html = listing_page("$225,000", "N/A", [("Gross Revenue", "$365,893"), ("Year Established", "1996")], description)

    page = parse_listing_page(html)

    assert page.facts["Asking Price"] == "$225,000"
    assert page.facts["SDE"] == "N/A"
    assert page.facts["Gross Revenue"] == "$365,893"
    assert page.facts["Year Established"] == "1996"
    assert page.description == description
    assert "total asking price" not in page.facts["Asking Price"]


def test_listing_from_pages_combines_card_and_page_into_a_listing():
    url = "https://www.thelibertygroupofnevada.com/listing/fake-janitorial/"
    description = fake.paragraph(nb_sentences=5)
    card = parse_listing_cards(liberty_card(url, "Fake Janitorial Company", "Cleaning Companies", "Washoe County, NV", "JAN1", "$360,000"), LIBERTY_GROUP_OF_NEVADA)[0]
    page = parse_listing_page(
        listing_page("$360,000", "$140,000", [("Gross Revenue", "$1,050,000"), ("Year Established", "2012"), ("Number of Employees", "14")], description)
    )

    listing = listing_from_pages(card, page, LIBERTY_GROUP_OF_NEVADA)

    assert listing.source == ListingSource.LIBERTY_GROUP_OF_NEVADA
    assert listing.source_listing_id == "JAN1"
    assert listing.url == url
    assert listing.title == "Fake Janitorial Company"
    fields = listing.fields
    assert (fields.asking_price, fields.gross_revenue, fields.seller_discretionary_earnings) == (360_000, 1_050_000, 140_000)
    assert (fields.year_established, fields.employee_count) == (2012, 14)
    assert (fields.county, fields.state) == ("Washoe", "NV")
    assert description in listing.raw_text
    assert "Gross Revenue: $1,050,000" in listing.raw_text


def test_sde_shown_as_not_available_is_unknown():
    card = parse_listing_cards(liberty_card("https://www.thelibertygroupofnevada.com/listing/x/", "X", "Retail", "Nevada", "X1", "$225,000"), LIBERTY_GROUP_OF_NEVADA)[0]
    page = parse_listing_page(listing_page("$225,000", "N/A", [], "A business."))

    assert listing_from_pages(card, page, LIBERTY_GROUP_OF_NEVADA).fields.seller_discretionary_earnings is None


def test_a_listing_without_an_internal_id_uses_its_web_address_as_its_id():
    url = "https://santarosabusinessandcommercial.com/listing/fake-yard-care/"
    card = parse_listing_cards(santa_rosa_card(url, "Fake Yard Care", "Landscaping", "Petaluma", "", "$500,000", "$400,000"), SANTA_ROSA_BUSINESS_AND_COMMERCIAL)[0]
    page = parse_listing_page(listing_page("$400,000", "$150,000", [], "Yard care."))

    assert listing_from_pages(card, page, SANTA_ROSA_BUSINESS_AND_COMMERCIAL).source_listing_id == "fake-yard-care"


# --- Location text as these sites write it --------------------------------------------------


@pytest.mark.parametrize(
    ("text", "home_state", "expected"),
    [
        ("Washoe County, NV", "NV", {"county": "Washoe", "state": "NV"}),
        ("Washoe County NV", "NV", {"county": "Washoe", "state": "NV"}),
        ("Reno, NV", "NV", {"city": "Reno", "state": "NV"}),
        ("Reno,NV", "NV", {"city": "Reno", "state": "NV"}),
        ("Reno, Nevada", "NV", {"city": "Reno", "state": "NV"}),
        ("Nevada County, CA", "NV", {"county": "Nevada", "state": "CA"}),
        ("Tahoe City, Placer County CA", "NV", {"city": "Tahoe City", "county": "Placer", "state": "CA"}),
        ("Sonoma County", "CA", {"county": "Sonoma", "state": "CA"}),
        ("Petaluma", "CA", {"city": "Petaluma", "state": "CA"}),
        ("Nevada", "NV", {"state": "NV"}),
        ("Northern Nevada", "NV", {"region": "Northern Nevada", "state": "NV"}),
        ("North Bay", "CA", {"region": "North Bay", "state": "CA"}),
        ("Western United States", "NV", {"region": "Western United States"}),
    ],
)
def test_locations_are_read_as_city_county_state_or_region(text, home_state, expected):
    location = parse_location(text, home_state)

    found = {key: value for key, value in location.items() if value is not None}
    assert found == expected


@pytest.mark.parametrize("status", ["SOLD", "Sold"])
def test_sold_listings_are_skipped(status):
    active = liberty_card("https://www.thelibertygroupofnevada.com/listing/active/", "Active Business", "Retail", "Reno, NV", "A1", "$300,000")
    sold = liberty_card("https://www.thelibertygroupofnevada.com/listing/sold/", "Sold Business", "Retail", "Reno, NV", "S1", "$300,000").replace(
        '<div class="listingBox">', f'<div class="listingBox"><div class="topLeft"><div class="soldButton">{status}</div></div>'
    )

    cards = parse_listing_cards(active + sold, LIBERTY_GROUP_OF_NEVADA)

    assert [card.title for card in cards] == ["Active Business"]
