"""Broker websites built on a shared WordPress listing template.

Santa Rosa Business & Commercial (Sonoma County) and The Liberty Group of
Nevada (Reno) use the same listing software. Each has one "businesses for
sale" page of cards (title, industry, location, internal ID, price, and on
Santa Rosa gross revenue), and a page per listing with a summary box
(asking price, SDE), a facts grid and the business description.

The two sites differ only in CSS class names and which facts sit on the card
versus the listing page, so one adapter serves both, with a `BrokerSite`
setting per site. Pages are read with Beautiful Soup by class name; tooltip
text and the hidden "print version" copy of the summary are removed first.

Both sites' robots.txt and terms of use allow reading these pages (checked
October 2026). Only new listings' own pages are fetched; listings already
stored are recognized from their card alone.
"""

import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from business_finder.models import Listing, ListingFields, ListingSource
from business_finder.sources.html_text import html_to_text
from business_finder.sources.polite_http import PoliteHttpClient
from business_finder.sources.values import parse_dollar_amount, parse_whole_number, parse_year_established

# Label and value classes used by either site, on cards and listing pages.
CARD_LABEL_SELECTOR = ".description-name, .descriptionName"
CARD_VALUE_SELECTOR = ".description-value, .descriptionValue"
PAGE_LABEL_SELECTOR = ".descriptionName, .description-name-l"
PAGE_VALUE_SELECTOR = ".descriptionValue, .description-value-l"
PRICE_SELECTOR = ".price-description-value, .priceDescriptionValue"
DESCRIPTION_SELECTOR = ".description-name"
# The status badge in a card's image corner: ACTIVE, NEW, SOLD...
STATUS_BADGE_SELECTOR = ".image-top-left div, .topLeft div"
SOLD_STATUSES = {"sold"}
# Removed before reading: help tooltips inside values, and the print copy of the page.
NOISE_SELECTOR = ".su-tooltip, .print-version"

STATE_CODES = {"ca": "CA", "california": "CA", "nv": "NV", "nevada": "NV", "or": "OR", "oregon": "OR"}
TRAILING_STATE = re.compile(r"[,\s]+(CA|California|NV|Nevada|OR|Oregon)\.?$", re.IGNORECASE)
REGION_START = re.compile(r"^(northern|southern|central|western|eastern|north|south|east|west)\b", re.IGNORECASE)
NO_SINGLE_STATE_REGIONS = re.compile(r"united states|west coast", re.IGNORECASE)


@dataclass(frozen=True)
class BrokerSite:
    """One broker website on the shared template."""

    source: ListingSource
    base_url: str
    listings_path: str
    home_state: str  # assumed when a listing's location names no state
    card_selector: str


SANTA_ROSA_BUSINESS_AND_COMMERCIAL = BrokerSite(
    source=ListingSource.SANTA_ROSA_BUSINESS_AND_COMMERCIAL,
    base_url="https://santarosabusinessandcommercial.com",
    listings_path="/businesses-for-sale/",
    home_state="CA",
    card_selector=".listing-box",
)

LIBERTY_GROUP_OF_NEVADA = BrokerSite(
    source=ListingSource.LIBERTY_GROUP_OF_NEVADA,
    base_url="https://www.thelibertygroupofnevada.com",
    listings_path="/businesses-for-sale/",
    home_state="NV",
    card_selector=".listingBox",
)


@dataclass
class ListingCard:
    """What a listing's card on the "businesses for sale" page shows."""

    url: str
    title: str
    internal_id: str
    facts: dict[str, str]
    price: str | None


@dataclass
class ListingPage:
    """What a listing's own page adds: labeled facts and the description."""

    facts: dict[str, str]
    description: str


# --- Reading pages -----------------------------------------------------------------------


def clean_label(text: str) -> str:
    return " ".join(text.split()).rstrip(":").strip()


def clean_value(element) -> str:
    return " ".join(element.get_text(" ", strip=True).split())


def labeled_facts(labels: list, values: list) -> dict[str, str]:
    """Pair labels with values in page order; the first value for a label wins."""
    facts: dict[str, str] = {}
    for label, value in zip(labels, values):
        facts.setdefault(clean_label(label.get_text(" ")), clean_value(value))
    return facts


def parse_listing_cards(html: str, site: BrokerSite) -> list[ListingCard]:
    """Read every listing card on a site's "businesses for sale" page, except sold ones."""
    soup = BeautifulSoup(html, "html.parser")
    cards = []
    for card in soup.select(site.card_selector):
        if is_sold(card):
            continue
        links = [link for link in card.select('a[href*="/listing/"]') if link.get_text(strip=True)]
        if not links:
            continue
        title_link = links[0]
        facts = labeled_facts(card.select(CARD_LABEL_SELECTOR), card.select(CARD_VALUE_SELECTOR))
        price_element = card.select_one(PRICE_SELECTOR)
        cards.append(
            ListingCard(
                url=urljoin(site.base_url, title_link["href"]),
                title=" ".join(title_link.get_text(" ", strip=True).split()),
                internal_id=facts.get("Internal ID", ""),
                facts=facts,
                price=clean_value(price_element) if price_element else None,
            )
        )
    return cards


def is_sold(card) -> bool:
    """Sold listings stay on these sites' pages with a SOLD badge; they are skipped."""
    badges = {badge.get_text(strip=True).lower() for badge in card.select(STATUS_BADGE_SELECTOR)}
    return bool(badges & SOLD_STATUSES)


def parse_listing_page(html: str) -> ListingPage:
    """Read a listing's own page: summary box, facts grid and description."""
    soup = BeautifulSoup(html, "html.parser")
    for noise in soup.select(NOISE_SELECTOR):
        noise.decompose()

    facts = labeled_facts(soup.select(".twcSummaryBoxName"), soup.select(".twcSummaryBoxValue"))
    for label, value in labeled_facts(soup.select(PAGE_LABEL_SELECTOR), soup.select(PAGE_VALUE_SELECTOR)).items():
        facts.setdefault(label, value)

    descriptions = [html_to_text(str(element)) for element in soup.select(DESCRIPTION_SELECTOR)]
    description = max(descriptions, key=len, default="")
    description = description.removeprefix("Business Description").strip()
    return ListingPage(facts=facts, description=description)


# --- Turning a card and page into a Listing --------------------------------------------


def parse_location(text: str, home_state: str) -> dict[str, str | None]:
    """Read a location as these sites write it into city, county, state and region.

    Handles "Reno, NV", "Washoe County NV", "Nevada County, CA", "Tahoe City,
    Placer County CA", "Sonoma County" (the site's home state assumed),
    plain states, and regions such as "Northern Nevada" or "North Bay".
    """
    location = {"city": None, "county": None, "state": None, "region": None}
    text = " ".join(text.split()).strip(" ,")
    if not text:
        return location

    if text.lower() in STATE_CODES:
        location["state"] = STATE_CODES[text.lower()]
        return location

    named_state = None
    trailing = TRAILING_STATE.search(text)
    if trailing:
        named_state = STATE_CODES[trailing.group(1).lower()]
        text = text[: trailing.start()].strip(" ,")

    if REGION_START.match(text) or NO_SINGLE_STATE_REGIONS.search(text):
        location["region"] = " ".join(filter(None, [text, trailing.group(1) if trailing else None]))
        if not NO_SINGLE_STATE_REGIONS.search(text):
            words = re.findall(r"[A-Za-z]+", location["region"])
            states_in_text = [STATE_CODES[word.lower()] for word in words if word.lower() in STATE_CODES]
            location["state"] = named_state or (states_in_text[0] if states_in_text else home_state)
        return location

    for part in (part.strip() for part in text.split(",")):
        if not part:
            continue
        if part.endswith(" County"):
            location["county"] = part.removesuffix(" County").strip()
        elif location["city"] is None:
            location["city"] = part
    location["state"] = named_state or home_state
    return location


def listing_id_for(card: ListingCard) -> str:
    """The site's internal ID, or the last part of the listing's web address when there is none."""
    if card.internal_id:
        return card.internal_id
    return urlparse(card.url).path.rstrip("/").split("/")[-1]


def raw_text_for(card: ListingCard, facts: dict[str, str], description: str) -> str:
    lines = [f"Title: {card.title}"]
    lines += [f"{label}: {value}" for label, value in facts.items() if value]
    if description:
        lines += ["", "Description:", description]
    return "\n".join(lines)


def listing_from_pages(card: ListingCard, page: ListingPage, site: BrokerSite) -> Listing:
    """Combine a listing's card and its own page into the pipeline's `Listing` shape."""
    facts = {**card.facts, **page.facts}
    if card.price and "Asking Price" not in facts:
        facts["Asking Price"] = card.price
    location = parse_location(facts.get("Location", ""), site.home_state)

    fields = ListingFields(
        business_type=facts.get("Industry") or None,
        asking_price=parse_dollar_amount(facts.get("Asking Price")),
        gross_revenue=parse_dollar_amount(facts.get("Gross Revenue")),
        seller_discretionary_earnings=parse_dollar_amount(facts.get("SDE") or facts.get("Cash Flow")),
        year_established=parse_year_established(facts.get("Year Established")),
        employee_count=parse_whole_number(facts.get("Number of Employees") or facts.get("Employees")),
        city=location["city"],
        county=location["county"],
        state=location["state"],
        states=[location["state"]] if location["state"] else [],
        region=location["region"],
    )
    return Listing(
        source=site.source,
        source_listing_id=listing_id_for(card),
        url=card.url,
        title=card.title,
        raw_text=raw_text_for(card, facts, page.description),
        raw_data={"card": asdict(card), "page_facts": page.facts},
        fields=fields,
    )


# --- The adapter -----------------------------------------------------------------------------


class BrokerSiteAdapter:
    """Collects every listing from one broker site on the shared template."""

    def __init__(self, http_client: PoliteHttpClient, site: BrokerSite) -> None:
        self.http_client = http_client
        self.site = site
        self.source = site.source

    def fetch_listings(self, known_ids: set[str] = frozenset()) -> Iterator[Listing]:
        """Yield every listing on the site.

        Listings already stored (`known_ids`) are built from their card
        alone, just so the pipeline can mark them as seen; only new
        listings' own pages are fetched.
        """
        cards_html = self.http_client.get_text(urljoin(self.site.base_url, self.site.listings_path))
        seen_ids: set[str] = set()
        for card in parse_listing_cards(cards_html, self.site):
            listing_id = listing_id_for(card)
            if listing_id in seen_ids:
                continue
            seen_ids.add(listing_id)
            if listing_id in known_ids:
                page = ListingPage(facts={}, description="")
            else:
                page = parse_listing_page(self.http_client.get_text(card.url))
            yield listing_from_pages(card, page, self.site)
