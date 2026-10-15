"""Tests for reading listing-alert emails: who sent them, their text, and which are new."""

from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

import pytest
from faker import Faker

from business_finder.models import ListingSource
from business_finder.sources.email_alerts import new_alert_emails, parse_email, source_for_sender

fake = Faker()


def make_email(
    sender: str = "BizBuySell <alerts@bizbuysell.com>",
    subject: str | None = None,
    plain: str | None = None,
    html: str | None = None,
    message_id: str | None = "generate",
    sent_at: datetime | None = None,
) -> bytes:
    """A real email, built with the standard library, as an inbox would deliver it."""
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "biancabusinessfinder@example.com"
    message["Subject"] = subject or f"New listings: {fake.catch_phrase()}"
    message["Date"] = format_datetime(sent_at or datetime(2026, 10, 14, 15, 30, tzinfo=UTC))
    if message_id == "generate":
        message["Message-ID"] = make_msgid(domain="bizbuysell.com")
    elif message_id is not None:
        message["Message-ID"] = message_id
    if plain is not None:
        message.set_content(plain)
    if html is not None:
        if plain is None:
            message.set_content(html, subtype="html")
        else:
            message.add_alternative(html, subtype="html")
    return message.as_bytes()


# --- Who sent it ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sender", "expected"),
    [
        ("BizBuySell <alerts@bizbuysell.com>", ListingSource.BIZBUYSELL),
        ("BizBuySell Alerts <noreply@email.bizbuysell.com>", ListingSource.BIZBUYSELL),
        ("BizScout <hello@bizscout.com>", ListingSource.BIZSCOUT),
        ("BusinessesForSale.com <alerts@businessesforsale.com>", ListingSource.BUSINESSES_FOR_SALE),
        ("Sunbelt <listings@sunbeltnetwork.com>", None),
        ("Google <no-reply@accounts.google.com>", None),
        ("Deals <alerts@bizbuysell.com.example-offers.net>", None),
        ("Not BizBuySell <alerts@notbizbuysell.com>", None),
    ],
    ids=[
        "BizBuySell",
        "BizBuySell subdomain",
        "BizScout",
        "BusinessesForSale.com",
        "Sunbelt (no alerts set up, so ignored)",
        "Google's own email",
        "look-alike domain",
        "domain that only ends with the name",
    ],
)
def test_alert_senders_are_recognized_by_their_domain(sender, expected):
    assert source_for_sender(sender) == expected


# --- Its text ----------------------------------------------------------------------------------


def test_the_plain_text_version_is_used_when_there_is_one():
    plain = fake.paragraph(nb_sentences=4)

    alert = parse_email(make_email(plain=plain, html=f"<p>{fake.paragraph()}</p>"))

    assert alert.text.strip() == plain


def test_html_only_emails_become_text_that_keeps_listing_links():
    listing_url = f"https://www.bizbuysell.com/business-opportunity/{fake.slug()}/2471583/"
    html = (
        "<html><body><h1>3 new listings match your search</h1>"
        f'<p><a href="{listing_url}">Established Residential Cleaning Company</a></p>'
        "<p>Asking Price: $850,000 &middot; Cash Flow: $310,000</p></body></html>"
    )

    alert = parse_email(make_email(html=html))

    assert "Established Residential Cleaning Company" in alert.text
    assert listing_url in alert.text
    assert "Asking Price: $850,000" in alert.text
    assert "<p>" not in alert.text


def test_an_email_keeps_its_source_subject_and_time():
    sent_at = datetime(2026, 10, 14, 15, 30, tzinfo=UTC)

    alert = parse_email(make_email(sender="BizScout <hello@bizscout.com>", subject="Your weekly deals", plain="Deals.", sent_at=sent_at))

    assert alert.source == ListingSource.BIZSCOUT
    assert alert.subject == "Your weekly deals"
    assert alert.received_at == sent_at


def test_an_email_without_a_message_id_still_gets_a_stable_id():
    raw = make_email(plain="Deals.", message_id=None)

    assert parse_email(raw).message_id == parse_email(raw).message_id
    assert parse_email(raw).message_id


# --- Which are new ----------------------------------------------------------------------------


def test_only_unprocessed_emails_from_listing_sites_are_new():
    processed = parse_email(make_email(plain="Old alert."))
    unprocessed = parse_email(make_email(plain="New alert."))
    welcome = parse_email(make_email(sender="Google <no-reply@accounts.google.com>", plain="Welcome."))

    new = new_alert_emails([processed, unprocessed, welcome], processed_message_ids={processed.message_id})

    assert new == [unprocessed]
