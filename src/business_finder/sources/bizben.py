"""BizBen (bizben.com) source adapter.

BizBen is mostly California listings, but it also has state pages for
Oregon and Nevada (and others). Transworld and some Sunbelt brokers post
their listings here too: Transworld's have URL paths containing `tw:`.

Its state and county pages are
built with Next.js, which embeds the page's data in the HTML as a series
of `self.__next_f.push([1, "..."])` script chunks. Inside that data is a
`categoryPageResult` object holding the page's listings as structured JSON
(asking price, cash flow, revenue, city, county and so on), plus the total
page count.

Reading that JSON is much sturdier than parsing the visible cards, whose
CSS class names are generated and change whenever the site is redeployed.

robots.txt (checked October 2026) allows these pages but disallows URLs
with `?offset=` or `?orderby=`, so pagination uses `?page=N` only.
"""

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

from business_finder.models import Listing, ListingFields, ListingSource
from business_finder.sources.html_text import html_to_text
from business_finder.sources.polite_http import PoliteHttpClient

BIZBEN_BASE_URL = "https://www.bizben.com"

# The path of each state's results page, by two-letter state code.
# Target states missing from this map are skipped.
STATE_PAGE_SLUGS = {
    "CA": "california-state-ca",
    "OR": "oregon-or",
    "NV": "nevada-nv",
}

STATE_CODES_BY_NAME = {"california": "CA", "oregon": "OR", "nevada": "NV"}

NEXT_DATA_CHUNK_PATTERN = re.compile(r"self\.__next_f\.push\((\[.*?\])\)</script>", re.DOTALL)
RESULTS_OBJECT_KEY = '"categoryPageResult":'

# How each listing's `locationVisibility` setting limits what we may use.
# Sellers choose how much of their location to show; we respect that even
# though the hidden parts are sometimes present in the page data.
VISIBILITY_SHOWS_CITY = {"showCityCountyState"}
VISIBILITY_SHOWS_COUNTY = {"showCityCountyState", "showCountyState"}


class BizBenPageFormatError(Exception):
    """Raised when a BizBen page no longer has the data layout we expect.

    This usually means the site changed, and the adapter needs updating.
    """


@dataclass
class BizBenResultsPage:
    """The listings and paging details found on one BizBen results page."""

    posts: list[dict]
    page_count: int
    total_listing_count: int


# --- Reading a results page ------------------------------------------------


def extract_next_data_payload(html: str) -> str:
    """Join the text of every Next.js data chunk embedded in the page.

    Each chunk is a JSON array like `[1, "text"]`. Only string payloads
    carry page data; other chunk kinds are skipped.
    """
    payload_parts = []
    for chunk_source in NEXT_DATA_CHUNK_PATTERN.findall(html):
        chunk = json.loads(chunk_source)
        if len(chunk) > 1 and isinstance(chunk[1], str):
            payload_parts.append(chunk[1])
    return "".join(payload_parts)


def parse_results_page(html: str) -> BizBenResultsPage:
    """Read the listings and page count from a BizBen state or county page."""
    payload = extract_next_data_payload(html)
    key_position = payload.find(RESULTS_OBJECT_KEY)
    if key_position == -1:
        raise BizBenPageFormatError("No categoryPageResult found in the page data")

    object_start = key_position + len(RESULTS_OBJECT_KEY)
    try:
        results_object, _ = json.JSONDecoder().raw_decode(payload, object_start)
    except json.JSONDecodeError as error:
        raise BizBenPageFormatError(f"categoryPageResult is not valid JSON: {error}") from error

    try:
        return BizBenResultsPage(
            posts=results_object["results"],
            page_count=results_object["pageCount"],
            total_listing_count=results_object["itemCount"],
        )
    except KeyError as error:
        raise BizBenPageFormatError(f"categoryPageResult is missing {error}") from error


def results_page_url(state_page_slug: str, page_number: int) -> str:
    """Return the URL of one page of results. Page numbers start at 1."""
    url = f"{BIZBEN_BASE_URL}/business-for-sale/{state_page_slug}"
    if page_number > 1:
        url += f"?page={page_number}"
    return url


# --- Turning one BizBen post into a Listing --------------------------------


def parse_dollar_amount(value: object) -> int | None:
    """Turn a BizBen money value into whole dollars, or None if not stated.

    BizBen mixes numbers (450000), digit strings ("1450000"), formatted
    strings ("633,484") and placeholders ("Call/Email", "All Cash"). It also
    uses 0 where the seller left a field blank, so 0 is treated as "not
    stated": no business for sale has zero revenue or a zero asking price.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        amount = int(value)
    elif isinstance(value, str):
        digits_only = value.replace("$", "").replace(",", "").strip()
        if not digits_only.isdigit():
            return None
        amount = int(digits_only)
    else:
        return None
    return amount if amount > 0 else None


def parse_whole_number(value: object) -> int | None:
    """Turn a number or digit string into an int, or None if it isn't one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def parse_year_established(value: object) -> int | None:
    """Return the year a business was established, if it is plausible."""
    year = parse_whole_number(value)
    if year is None:
        return None
    if year < 1800 or year > datetime.now(UTC).year:
        return None
    return year


def listing_id_from_post(post: dict) -> str:
    """Return BizBen's own ID for a post.

    `entityId` ("#POST#2869829") is present on every post we have seen;
    `postID` is only on older ones. The URL path ends with the same number
    and is the last resort.
    """
    entity_id = post.get("entityId", "")
    if entity_id.startswith("#POST#"):
        return entity_id.removeprefix("#POST#")
    if post.get("postID"):
        return str(post["postID"])
    trailing_number = re.search(r"(\d+)$", post.get("urlPath", ""))
    if trailing_number:
        return trailing_number.group(1)
    raise BizBenPageFormatError(f"Post has no usable ID: {post.get('title')!r}")


def as_list(value: object) -> list[str]:
    """Return a list of strings, whether BizBen gave a list, one string or nothing."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def normalize_county(county: str | None) -> str | None:
    """Return a county name without the word "County" ("Sonoma County" -> "Sonoma")."""
    if not county:
        return None
    return county.strip().removesuffix(" County").strip() or None


def normalize_state(state: str | None) -> str | None:
    """Return a two-letter state code ("California" -> "CA")."""
    if not state:
        return None
    state = state.strip()
    if len(state) == 2:
        return state.upper()
    return STATE_CODES_BY_NAME.get(state.lower(), state)


def state_from_post(post: dict) -> str | None:
    """Return the post's state as a two-letter code, if it names exactly one.

    Most posts have a `state` string. Some have a `states` list instead,
    for businesses offered in several states or relocatable anywhere. A
    business listed in several states has no single location, so it gets
    no state and the geography step tags it as unknown.
    """
    if post.get("state"):
        return normalize_state(post["state"])
    states = as_list(post.get("states"))
    if len(states) == 1:
        return normalize_state(states[0])
    return None


def fields_from_post(post: dict) -> ListingFields:
    """Read the structured fields BizBen already provides.

    Only facts BizBen states directly are filled in. Anything that needs
    judgment (such as whether the work needs a contractor's license) is
    left for the Claude extractor.
    """
    visibility = post.get("locationVisibility")
    business_types = as_list(post.get("businessTypes")) or as_list(post.get("businessCategory"))

    asking_price = None
    if not post.get("hideAskingPrice"):
        asking_price = parse_dollar_amount(post.get("askingPrice"))

    return ListingFields(
        business_type=", ".join(business_types) or None,
        asking_price=asking_price,
        gross_revenue=(
            parse_dollar_amount(post.get("revenue"))
            or parse_dollar_amount(post.get("revenueInt"))
        ),
        seller_discretionary_earnings=(
            parse_dollar_amount(post.get("cashFlow"))
            or parse_dollar_amount(post.get("adjustedNet"))
        ),
        down_payment=parse_dollar_amount(post.get("down")),
        year_established=parse_year_established(post.get("establishedYear")),
        employee_count=parse_whole_number(post.get("employees")),
        city=post.get("city") if visibility in VISIBILITY_SHOWS_CITY else None,
        county=normalize_county(post.get("county")) if visibility in VISIBILITY_SHOWS_COUNTY else None,
        state=state_from_post(post),
    )


def raw_text_from_post(post: dict, fields: ListingFields) -> str:
    """Build a readable text version of a post for storage and for Claude.

    The labeled facts come first, then the seller's description, so the
    extractor and a human reader see everything BizBen showed.
    """
    location_parts = [fields.city, f"{fields.county} County" if fields.county else None, fields.state]
    labeled_facts = [
        ("Title", post.get("title")),
        ("Business types", fields.business_type),
        ("Location", ", ".join(part for part in location_parts if part)),
        ("Asking price", post.get("askingPrice")),
        ("Gross revenue", post.get("revenue")),
        ("Cash flow", post.get("cashFlow")),
        ("Adjusted net", post.get("adjustedNet")),
        ("Down payment", post.get("down")),
        ("Established", post.get("establishedYear")),
        ("Employees", post.get("employees")),
        ("Reason for sale", post.get("saleReason")),
    ]

    lines = []
    for label, value in labeled_facts:
        if value not in (None, "", 0):
            lines.append(f"{label}: {value}")

    description = html_to_text(post.get("description") or "")
    if description:
        lines.append("")
        lines.append("Description:")
        lines.append(description)
    return "\n".join(lines)


def listing_from_post(post: dict) -> Listing:
    """Turn one BizBen post into the pipeline's `Listing` shape."""
    fields = fields_from_post(post)
    return Listing(
        source=ListingSource.BIZBEN,
        source_listing_id=listing_id_from_post(post),
        url=f"{BIZBEN_BASE_URL}/business-for-sale/{post['urlPath']}",
        title=post.get("title"),
        raw_text=raw_text_from_post(post, fields),
        raw_data=post,
        fields=fields,
    )


def is_active_post(post: dict) -> bool:
    """Return True for posts that are currently for sale."""
    return post.get("status", "active") == "active"


# --- The adapter -----------------------------------------------------------


class BizBenAdapter:
    """Collects every active BizBen listing in the target states."""

    def __init__(self, http_client: PoliteHttpClient, target_states: list[str]) -> None:
        """Set up the adapter.

        `target_states` are two-letter codes. States without a known
        BizBen page are ignored.
        """
        self.http_client = http_client
        self.state_page_slugs = [
            STATE_PAGE_SLUGS[state] for state in target_states if state in STATE_PAGE_SLUGS
        ]

    def fetch_listings(self) -> Iterator[Listing]:
        """Yield every active listing, one results page at a time.

        A listing can appear more than once in a run: on several state
        pages (multi-state businesses), or on two pages of the same state
        when new posts push others down while we are paging. IDs already
        yielded in this run are skipped.
        """
        seen_listing_ids: set[str] = set()
        for state_page_slug in self.state_page_slugs:
            yield from self.fetch_state_listings(state_page_slug, seen_listing_ids)

    def fetch_state_listings(self, state_page_slug: str, seen_listing_ids: set[str]) -> Iterator[Listing]:
        """Yield every active, not-yet-seen listing on every results page for one state.

        The first page says how many pages there are.
        """
        page_number = 1
        page_count = 1

        while page_number <= page_count:
            html = self.http_client.get_text(results_page_url(state_page_slug, page_number))
            results_page = parse_results_page(html)
            page_count = results_page.page_count

            for post in results_page.posts:
                if not is_active_post(post):
                    continue
                listing = listing_from_post(post)
                if listing.source_listing_id in seen_listing_ids:
                    continue
                seen_listing_ids.add(listing.source_listing_id)
                yield listing

            page_number += 1
