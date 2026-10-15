"""Turning listing-alert emails into listings.

Alert layouts differ by site and by email type (BizBuySell alone sends a
digest and a single-listing email), so the work is split:

- Code finds each site's listing links, reads the listing's ID from the
  link, builds a clean link without tracking, and replaces each link in the
  text with a short label such as [L1]. Every other link (ads, unsubscribe,
  tracking) is removed, so no personal tokens reach the model and the model
  never has to copy a long web address.
- A model (Claude Haiku) reads the cleaned text and lists the businesses for
  sale, each with its label, title and the lines describing it. The answer
  can only name labels that exist in that email.
- Code turns each answer into a normal `Listing`, identified by the site's
  listing ID, which then goes through the usual extraction, matching and
  notification steps.
"""

import copy
import json
import re
from dataclasses import dataclass
from importlib import resources
from urllib.parse import parse_qs, urlparse

import anthropic
from pydantic import BaseModel, ConfigDict, ValidationError

from business_finder.config import ExtractionSettings
from business_finder.extraction.extractor import ExtractionError
from business_finder.models import Listing, ListingSource
from business_finder.sources.email_alerts import ALERT_SENDER_DOMAINS, AlertEmail

PROMPTS_PACKAGE = "business_finder.extraction.prompts"
READER_PROMPT_FILE = "read_alert_email.md"
READER_SCHEMA_FILE = "read_alert_email.schema.json"
LINK_LABELS_PLACEHOLDER = "{link_labels}"
# A digest can list many businesses, each copied word for word.
MAX_OUTPUT_TOKENS = 16000

# Links as they appear in alert text: "<https://...>" from HTML emails, or bare in plain-text ones.
LINK_IN_TEXT = re.compile(r"<(https?://[^\s<>]+)>|(https?://[^\s<>]+)")

SOURCE_DISPLAY_NAMES = {
    ListingSource.BIZBUYSELL: "BizBuySell",
    ListingSource.BIZSCOUT: "BizScout",
    ListingSource.BUSINESSES_FOR_SALE: "BusinessesForSale.com",
}


@dataclass(frozen=True)
class ListingLink:
    """A link to one listing: its clean address and the site's ID for it."""

    url: str
    listing_id: str


@dataclass
class CleanedEmailText:
    """Alert text ready for the model: listing links replaced by labels such as [L1],
    every other link removed, and what each label points to."""

    text: str
    links: dict[str, ListingLink]


class EmailBusiness(BaseModel):
    """One business the model found in an alert email."""

    model_config = ConfigDict(extra="forbid")

    link_label: str
    title: str
    details: str


class EmailReading(BaseModel):
    """The model's answer for one alert email: the businesses for sale it found (possibly none).

    It mirrors read_alert_email.schema.json and checks the answer has that
    shape before any listing is built from it; an answer that doesn't fit
    leaves the email unprocessed, to be read again next run.
    """

    model_config = ConfigDict(extra="forbid")

    listings: list[EmailBusiness]


# --- Recognizing listing links ---------------------------------------------------------------


def is_on_domain(url: str, domain: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == domain or host.endswith("." + domain)


def bizbuysell_listing_link(url: str) -> ListingLink | None:
    """BizBuySell listing links carry the listing's ID in `q`, or at the end of the path."""
    if not is_on_domain(url, "bizbuysell.com"):
        return None
    parsed = urlparse(url)
    path = parsed.path.lower()
    listing_id = None
    if path.startswith("/listings/profile"):
        listing_id = parse_qs(parsed.query).get("q", [None])[0]
    elif path.startswith("/business-opportunity/"):
        trailing_number = re.search(r"/(\d{5,})/?$", parsed.path)
        listing_id = trailing_number.group(1) if trailing_number else None
    if not listing_id or not listing_id.isdigit():
        return None
    return ListingLink(url=f"https://www.bizbuysell.com/listings/Profile/?q={listing_id}", listing_id=listing_id)


def sender_site_listing_link(url: str, source: ListingSource) -> ListingLink | None:
    """For sites whose listing links we have not studied yet: any link on the site's own domain.

    The address without its query (tracking) part is used as the ID. The
    model decides which of these labels sit next to a business.
    """
    domain = next((domain for domain, site in ALERT_SENDER_DOMAINS.items() if site == source), None)
    if domain is None or not is_on_domain(url, domain):
        return None
    clean_url = url.split("?")[0].split("#")[0]
    return ListingLink(url=clean_url, listing_id=clean_url)


def listing_link_for(url: str, source: ListingSource) -> ListingLink | None:
    """Decide whether a link in an alert email leads to one of the site's listings.

    Returns the listing's clean address and the site's ID for it, or None for
    any other link (ads, unsubscribe, tracking), which is then removed from
    the text. BizBuySell has its own rule because we know how its listing
    links carry the ID; other sites use a general rule until we have seen
    their alerts.
    """
    if source == ListingSource.BIZBUYSELL:
        return bizbuysell_listing_link(url)
    return sender_site_listing_link(url, source)


def label_listing_links(text: str, source: ListingSource) -> CleanedEmailText:
    """Replace listing links with [L1], [L2]... (one label per listing) and remove every other link."""
    labels_by_listing_id: dict[str, str] = {}
    links: dict[str, ListingLink] = {}

    def replace(match: re.Match) -> str:
        url = match.group(1) or match.group(2)
        link = listing_link_for(url, source)
        if link is None:
            return ""
        if link.listing_id not in labels_by_listing_id:
            label = f"L{len(labels_by_listing_id) + 1}"
            labels_by_listing_id[link.listing_id] = label
            links[label] = link
        return f"[{labels_by_listing_id[link.listing_id]}]"

    return CleanedEmailText(text=LINK_IN_TEXT.sub(replace, text), links=links)


# --- The model's instructions and answer schema ----------------------------------------------------


def load_reader_prompt() -> str:
    return resources.files(PROMPTS_PACKAGE).joinpath(READER_PROMPT_FILE).read_text()


def reading_schema_for(cleaned: CleanedEmailText) -> dict:
    """The answer schema for one email: link labels may only be ones found in it."""
    schema = json.loads(resources.files(PROMPTS_PACKAGE).joinpath(READER_SCHEMA_FILE).read_text())
    schema = copy.deepcopy(schema)
    label_property = schema["properties"]["listings"]["items"]["properties"]["link_label"]
    label_property["enum"] = list(cleaned.links)
    return schema


# --- Building listings ------------------------------------------------------------------------------


def listings_from_reading(reading: EmailReading, cleaned: CleanedEmailText, alert: AlertEmail) -> list[Listing]:
    """Turn the model's answer into listings, one per listing link.

    Businesses naming a label that is not in the email are dropped, and a
    business listed twice is kept once.
    """
    listings = []
    seen_listing_ids: set[str] = set()
    site_name = SOURCE_DISPLAY_NAMES.get(alert.source, alert.source.value)
    for item in reading.listings:
        link = cleaned.links.get(item.link_label)
        if link is None or link.listing_id in seen_listing_ids:
            continue
        seen_listing_ids.add(link.listing_id)
        raw_text = (
            f"Title: {item.title}\n{item.details.strip()}\n\n"
            f'From a {site_name} alert email: "{alert.subject}", received {alert.received_at:%Y-%m-%d}.'
        )
        listings.append(
            Listing(
                source=alert.source,
                source_listing_id=link.listing_id,
                url=link.url,
                title=item.title,
                raw_text=raw_text,
                raw_data={
                    "email_message_id": alert.message_id,
                    "email_subject": alert.subject,
                    "email_received_at": alert.received_at.isoformat(),
                },
            )
        )
    return listings


# --- Reading an email with Claude ----------------------------------------------------------------------


class ClaudeAlertReader:
    """Lists the businesses in an alert email with a Claude model."""

    def __init__(self, settings: ExtractionSettings, client: anthropic.Anthropic | None = None) -> None:
        self.settings = settings
        self.client = client or anthropic.Anthropic()
        self.instructions = load_reader_prompt()

    def read(self, alert: AlertEmail) -> list[Listing]:
        """Return the listings in one alert email.

        An email with no listing links yields nothing without calling the
        model. Raises `ExtractionError` for an unusable answer; the email is
        then left unprocessed and tried again next run.
        """
        cleaned = label_listing_links(alert.text, alert.source)
        if not cleaned.links:
            return []
        response = self.client.messages.create(
            model=self.settings.model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=[{"type": "text", "text": self.instructions, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": f"<email subject=\"{alert.subject}\">\n{cleaned.text}\n</email>"}],
            output_config={
                "effort": self.settings.effort,
                "format": {"type": "json_schema", "schema": reading_schema_for(cleaned)},
            },
        )
        if response.stop_reason == "refusal":
            raise ExtractionError("Claude declined to read this alert email")
        if response.stop_reason == "max_tokens":
            raise ExtractionError("Claude's reading of this alert email was cut off")
        answer_text = next((block.text for block in response.content if block.type == "text"), None)
        if answer_text is None:
            raise ExtractionError("Claude's answer had no text block")
        try:
            reading = EmailReading.model_validate_json(answer_text)
        except ValidationError as error:
            raise ExtractionError(f"Claude's reading did not match the schema: {error}") from error
        return listings_from_reading(reading, cleaned, alert)
