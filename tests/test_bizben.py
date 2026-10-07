"""Tests for the BizBen adapter. All pages are built from fake data; nothing touches the network."""

import json

import pytest

from business_finder.models import ListingSource
from business_finder.sources.bizben import (
    BizBenAdapter,
    BizBenPageFormatError,
    listing_from_post,
    parse_dollar_amount,
    parse_results_page,
    results_page_url,
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


def make_results_page_html(posts: list[dict], page_count: int = 1) -> str:
    """Build a page that embeds its data the way BizBen's Next.js pages do.

    The data is split across two chunks, as on the real site, so the test
    also checks that chunks are joined before parsing.
    """
    results_object = {
        "results": posts,
        "itemCount": len(posts),
        "pageCount": page_count,
        "nextPageToken": None,
        "page": 0,
    }
    payload = '5:{"categoryPageResult":' + json.dumps(results_object) + "}"
    halfway = len(payload) // 2
    scripts = "".join(
        f"<script>self.__next_f.push({json.dumps([1, part])})</script>"
        for part in (payload[:halfway], payload[halfway:])
    )
    return f"<html><head></head><body>{scripts}</body></html>"


class FakeHttpClient:
    """Returns prepared pages by URL and records which URLs were requested."""

    def __init__(self, pages_by_url: dict[str, str]) -> None:
        self.pages_by_url = pages_by_url
        self.requested_urls: list[str] = []

    def get_text(self, url: str) -> str:
        self.requested_urls.append(url)
        return self.pages_by_url[url]


# --- Reading a results page ------------------------------------------------


def test_parse_results_page_reads_posts_and_page_count():
    html = make_results_page_html([make_post(), make_post(entityId="#POST#1000002")], page_count=3)

    results_page = parse_results_page(html)

    assert len(results_page.posts) == 2
    assert results_page.page_count == 3
    assert results_page.total_listing_count == 2


def test_parse_results_page_fails_loudly_when_layout_changes():
    with pytest.raises(BizBenPageFormatError):
        parse_results_page("<html><body>No data here</body></html>")


def test_results_page_url_only_adds_page_number_after_the_first_page():
    assert results_page_url("california-state-ca", 1) == (
        "https://www.bizben.com/business-for-sale/california-state-ca"
    )
    assert results_page_url("california-state-ca", 2) == (
        "https://www.bizben.com/business-for-sale/california-state-ca?page=2"
    )


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


def test_listing_keeps_the_original_post_for_reparsing():
    post = make_post()

    listing = listing_from_post(post)

    assert listing.raw_data == post


def test_raw_text_has_labeled_facts_and_plain_text_description():
    listing = listing_from_post(make_post())

    assert "Asking price: 850000" in listing.raw_text
    assert "Location: Petaluma, Sonoma County, CA" in listing.raw_text
    assert "Recurring clients & trained crews.\nOwner retiring." in listing.raw_text
    assert "<p>" not in listing.raw_text


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        (450000, 450_000),
        ("1450000", 1_450_000),
        ("633,484", 633_484),
        ("$75,000", 75_000),
        (0, None),
        ("0", None),
        ("Call/Email", None),
        ("All Cash", None),
        (None, None),
        (True, None),
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


def test_county_only_visibility_hides_the_city():
    listing = listing_from_post(make_post(locationVisibility="showCountyState"))

    assert listing.fields.city is None
    assert listing.fields.county == "Sonoma"


def test_state_only_visibility_hides_city_and_county():
    listing = listing_from_post(make_post(locationVisibility="showState"))

    assert listing.fields.city is None
    assert listing.fields.county is None
    assert listing.fields.state == "CA"


def test_single_entry_states_list_gives_that_state():
    post = make_post(states=["Oregon"])
    del post["state"]

    listing = listing_from_post(post)

    assert listing.fields.state == "OR"


def test_multi_state_listing_has_no_single_state():
    post = make_post(states=["California", "Oregon"])
    del post["state"]

    listing = listing_from_post(post)

    assert listing.fields.state is None


def test_transworld_listing_on_bizben_keeps_its_id():
    post = make_post(
        entityId="#POST#*0000fake0000hash*91044",
        urlPath="fake-therapy-practice-tw:91044",
    )

    listing = listing_from_post(post)

    assert listing.source_listing_id == "*0000fake0000hash*91044"
    assert listing.url.endswith("/fake-therapy-practice-tw:91044")


def test_county_name_is_normalized():
    listing = listing_from_post(make_post(county="Sonoma County"))

    assert listing.fields.county == "Sonoma"


def test_business_category_is_used_when_types_are_missing():
    post = make_post(businessCategory=["Lawn/Landscaping"])
    del post["businessTypes"]

    listing = listing_from_post(post)

    assert listing.fields.business_type == "Lawn/Landscaping"


def test_listing_id_falls_back_to_url_number():
    post = make_post(urlPath="some-business-7654321")
    del post["entityId"]

    listing = listing_from_post(post)

    assert listing.source_listing_id == "7654321"


# --- The adapter -----------------------------------------------------------


def test_adapter_reads_every_page_of_results():
    first_page_url = results_page_url("california-state-ca", 1)
    second_page_url = results_page_url("california-state-ca", 2)
    http_client = FakeHttpClient(
        {
            first_page_url: make_results_page_html([make_post(entityId="#POST#1")], page_count=2),
            second_page_url: make_results_page_html([make_post(entityId="#POST#2")], page_count=2),
        }
    )
    adapter = BizBenAdapter(http_client, target_states=["CA"])

    listings = list(adapter.fetch_listings())

    assert [listing.source_listing_id for listing in listings] == ["1", "2"]
    assert http_client.requested_urls == [first_page_url, second_page_url]


def test_adapter_skips_listings_repeated_across_pages():
    first_page_url = results_page_url("california-state-ca", 1)
    second_page_url = results_page_url("california-state-ca", 2)
    http_client = FakeHttpClient(
        {
            first_page_url: make_results_page_html([make_post(entityId="#POST#1")], page_count=2),
            second_page_url: make_results_page_html(
                [make_post(entityId="#POST#1"), make_post(entityId="#POST#2")], page_count=2
            ),
        }
    )
    adapter = BizBenAdapter(http_client, target_states=["CA"])

    listings = list(adapter.fetch_listings())

    assert [listing.source_listing_id for listing in listings] == ["1", "2"]


def test_adapter_skips_inactive_posts():
    page_url = results_page_url("california-state-ca", 1)
    http_client = FakeHttpClient(
        {
            page_url: make_results_page_html(
                [make_post(entityId="#POST#1"), make_post(entityId="#POST#2", status="sold")]
            )
        }
    )
    adapter = BizBenAdapter(http_client, target_states=["CA"])

    listings = list(adapter.fetch_listings())

    assert [listing.source_listing_id for listing in listings] == ["1"]


def test_adapter_fetches_each_target_state():
    california_url = results_page_url("california-state-ca", 1)
    oregon_url = results_page_url("oregon-or", 1)
    nevada_url = results_page_url("nevada-nv", 1)
    http_client = FakeHttpClient(
        {
            california_url: make_results_page_html([make_post(entityId="#POST#1")]),
            oregon_url: make_results_page_html([make_post(entityId="#POST#2", state="Oregon")]),
            nevada_url: make_results_page_html([make_post(entityId="#POST#3", state="Nevada")]),
        }
    )
    adapter = BizBenAdapter(http_client, target_states=["CA", "OR", "NV"])

    listings = list(adapter.fetch_listings())

    assert [listing.fields.state for listing in listings] == ["CA", "OR", "NV"]
    assert http_client.requested_urls == [california_url, oregon_url, nevada_url]


def test_adapter_skips_listings_repeated_across_states():
    multi_state_post = make_post(entityId="#POST#9", states=["California", "Oregon"])
    del multi_state_post["state"]
    http_client = FakeHttpClient(
        {
            results_page_url("california-state-ca", 1): make_results_page_html([multi_state_post]),
            results_page_url("oregon-or", 1): make_results_page_html([multi_state_post]),
        }
    )
    adapter = BizBenAdapter(http_client, target_states=["CA", "OR"])

    listings = list(adapter.fetch_listings())

    assert [listing.source_listing_id for listing in listings] == ["9"]


def test_adapter_ignores_states_without_a_bizben_page():
    http_client = FakeHttpClient({})
    adapter = BizBenAdapter(http_client, target_states=["WY"])

    assert list(adapter.fetch_listings()) == []
    assert http_client.requested_urls == []
