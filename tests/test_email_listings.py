"""Tests for turning listing-alert emails into listings: link labels, and building listings from Claude's reading."""

from datetime import UTC, datetime

from faker import Faker

from business_finder.models import ListingSource
from business_finder.sources.email_listings import (
    EmailBusiness,
    EmailReading,
    label_listing_links,
    listings_from_reading,
    reading_schema_for,
)
from business_finder.sources.email_alerts import AlertEmail

fake = Faker()


def bizbuysell_link(listing_id: str, tracking: str) -> str:
    return f"https://www.bizbuysell.com/listings/Profile/?q={listing_id}&J={tracking}&utm_source=alert&utm_medium=email"


def bizbuysell_digest(first_title: str, second_title: str) -> str:
    """Text shaped like a real BizBuySell digest alert (made-up listings)."""
    return f"""Lucie, see what's just been listed for sale on BizBuySell!
Businesses Recently Posted for Sale
<{bizbuysell_link("2564983", "x91a")}>
{first_title} <{bizbuysell_link("2564983", "x91b")}>
Asking Price:
$795,000
Location:
Petaluma, CA
{second_title} <{bizbuysell_link("2564584", "k22c")}>
Asking Price:
$1,100,000
Location:
Windsor, CA
The following franchises <https://www.bizbuysell.com/franchise-for-sale/?utm_source=alert> match your search criteria.
Kiddie Academy <https://www.bizbuysell.com/franchise-for-sale/kiddie-academy/?utm_source=alert>
To unsubscribe from this email, click here <https://www.bizbuysell.com/unsubscribe.aspx?token=secret-token-123>."""


def make_alert(text: str, source: ListingSource = ListingSource.BIZBUYSELL) -> AlertEmail:
    return AlertEmail(
        message_id=f"<{fake.uuid4()}@alerts.example>",
        source=source,
        sender="BizBuySell <alerts@bizbuysell.com>",
        subject="2 New Business Matches: Businesses in California For Sale",
        received_at=datetime(2026, 10, 15, 7, 20, tzinfo=UTC),
        text=text,
    )


# --- Link labels -------------------------------------------------------------------------------


def test_each_bizbuysell_listing_gets_one_label_a_clean_link_and_its_id():
    first_title, second_title = fake.catch_phrase(), fake.catch_phrase()

    cleaned = label_listing_links(bizbuysell_digest(first_title, second_title), ListingSource.BIZBUYSELL)

    assert sorted(link.listing_id for link in cleaned.links.values()) == ["2564584", "2564983"]
    assert {link.url for link in cleaned.links.values()} == {
        "https://www.bizbuysell.com/listings/Profile/?q=2564983",
        "https://www.bizbuysell.com/listings/Profile/?q=2564584",
    }
    first_label = next(label for label, link in cleaned.links.items() if link.listing_id == "2564983")
    assert f"{first_title} [{first_label}]" in cleaned.text


def test_other_links_and_tracking_never_reach_the_text_claude_reads():
    cleaned = label_listing_links(bizbuysell_digest(fake.catch_phrase(), fake.catch_phrase()), ListingSource.BIZBUYSELL)

    assert "http" not in cleaned.text
    assert "secret-token-123" not in cleaned.text
    assert "Kiddie Academy" in cleaned.text
    assert len(cleaned.links) == 2


def test_links_on_other_sites_are_not_labeled_for_sites_without_a_known_layout():
    listing_url = f"https://app.bizscout.com/deals/{fake.random_number(digits=6)}?ref=alert"
    text = f"New deal: Residential Cleaning Company <{listing_url}>\nPowered by <https://www.mailservice.example/track?id=1>"

    cleaned = label_listing_links(text, ListingSource.BIZSCOUT)

    assert [link.url for link in cleaned.links.values()] == [listing_url.split("?")[0]]
    assert "mailservice" not in cleaned.text


def test_the_answer_schema_only_allows_labels_found_in_that_email():
    cleaned = label_listing_links(bizbuysell_digest(fake.catch_phrase(), fake.catch_phrase()), ListingSource.BIZBUYSELL)

    schema = reading_schema_for(cleaned)

    label_property = schema["properties"]["listings"]["items"]["properties"]["link_label"]
    assert sorted(label_property["enum"]) == sorted(cleaned.links)


# --- Building listings from Claude's reading -------------------------------------------------------


def test_each_business_claude_found_becomes_a_listing_with_its_details_and_where_it_came_from():
    title = fake.catch_phrase()
    alert = make_alert(bizbuysell_digest(title, fake.catch_phrase()))
    cleaned = label_listing_links(alert.text, ListingSource.BIZBUYSELL)
    label = next(label for label, link in cleaned.links.items() if link.listing_id == "2564983")
    reading = EmailReading(listings=[EmailBusiness(link_label=label, title=title, details="Asking Price: $795,000\nLocation: Petaluma, CA")])

    listings = listings_from_reading(reading, cleaned, alert)

    assert len(listings) == 1
    listing = listings[0]
    assert listing.source == ListingSource.BIZBUYSELL
    assert listing.source_listing_id == "2564983"
    assert listing.url == "https://www.bizbuysell.com/listings/Profile/?q=2564983"
    assert listing.title == title
    assert "Asking Price: $795,000" in listing.raw_text
    assert alert.subject in listing.raw_text
    assert listing.raw_data["email_message_id"] == alert.message_id


def test_businesses_with_an_unknown_label_are_dropped_and_repeats_kept_once():
    alert = make_alert(bizbuysell_digest(fake.catch_phrase(), fake.catch_phrase()))
    cleaned = label_listing_links(alert.text, ListingSource.BIZBUYSELL)
    real_label = next(iter(cleaned.links))
    reading = EmailReading(
        listings=[
            EmailBusiness(link_label=real_label, title="A business", details="Asking Price: $795,000"),
            EmailBusiness(link_label=real_label, title="The same business again", details="Asking Price: $795,000"),
            EmailBusiness(link_label="L99", title="Not in this email", details="Asking Price: $1"),
        ]
    )

    listings = listings_from_reading(reading, cleaned, alert)

    assert [listing.title for listing in listings] == ["A business"]
