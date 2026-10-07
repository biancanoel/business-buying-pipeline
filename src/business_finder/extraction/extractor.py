"""The provider-neutral side of extraction.

An `Extractor` reads a listing's text and returns `ListingFields`. The
model-specific part (which API to call) lives in each implementation, such
as `ClaudeExtractor`. Everything here is shared: the prompt and schema
files, the shape of a model's answer, turning that answer into
`ListingFields`, merging it with what the site already gave us, and
deciding which listings are worth sending to a model at all.

The model transcribes; code calculates. For example, the model reports
"in business for 20 years" as `years_in_business=20`, and code here turns
that into a founding year.
"""

import json
from datetime import UTC, datetime
from importlib import resources
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from business_finder.models import Listing, ListingFields, SavedSearch

PROMPTS_PACKAGE = "business_finder.extraction.prompts"
SYSTEM_PROMPT_FILE = "extract_listing_fields.md"
SCHEMA_FILE = "listing_fields.schema.json"

EARLIEST_PLAUSIBLE_YEAR = 1800


class ExtractionError(Exception):
    """Raised when a model's answer for one listing can't be used.

    The pipeline should keep the listing with its plain-code fields and
    move on, rather than stop the whole run.
    """


class Extractor(Protocol):
    """Anything that can read listing text and return structured fields."""

    def extract(self, listing_text: str) -> ListingFields:
        """Return the fields stated in the listing text, None where not stated."""
        ...


class SourceSnippet(BaseModel):
    """The listing text a model took one field's value from."""

    model_config = ConfigDict(extra="forbid")

    field: str
    quote: str


class ExtractionResult(BaseModel):
    """A model's answer, exactly as the schema file describes it.

    This mirrors `prompts/listing_fields.schema.json` (a test keeps the two
    in sync). It differs from `ListingFields` on purpose: it has
    `years_in_business`, which code converts to a year, and its snippets
    are a list because the schema format can't describe a free-form map.
    """

    model_config = ConfigDict(extra="forbid")

    business_name: str | None
    business_type: str | None
    requires_contractor_license: bool | None
    asking_price: int | None
    gross_revenue: int | None
    seller_discretionary_earnings: int | None
    down_payment: int | None
    year_established: int | None
    years_in_business: int | None
    employee_count: int | None
    city: str | None
    county: str | None
    state: str | None
    source_snippets: list[SourceSnippet]


# --- Prompt and schema files ------------------------------------------------


def load_system_prompt() -> str:
    """Return the extraction instructions shared by every model backend."""
    return resources.files(PROMPTS_PACKAGE).joinpath(SYSTEM_PROMPT_FILE).read_text()


def load_output_schema() -> dict:
    """Return the JSON schema every model backend must answer in."""
    return json.loads(resources.files(PROMPTS_PACKAGE).joinpath(SCHEMA_FILE).read_text())


# --- Turning a model's answer into ListingFields ---------------------------


def positive_or_none(value: int | None) -> int | None:
    """Treat zero and negative amounts as "not stated"."""
    if value is None or value <= 0:
        return None
    return value


def plausible_year_or_none(year: int | None, current_year: int) -> int | None:
    """Drop years that can't be a founding year."""
    if year is None:
        return None
    if year < EARLIEST_PLAUSIBLE_YEAR or year > current_year:
        return None
    return year


def year_from_years_in_business(years_in_business: int | None, current_year: int) -> int | None:
    """Turn "in business for N years" into an approximate founding year.

    The result can be a year off, depending on when in the year the
    listing was written, which is close enough for a minimum-age filter.
    """
    if years_in_business is None or years_in_business < 0:
        return None
    return plausible_year_or_none(current_year - years_in_business, current_year)


def fields_from_extraction(result: ExtractionResult, current_year: int | None = None) -> ListingFields:
    """Convert a validated model answer into `ListingFields`.

    Implausible values (zero prices, future founding years) become None
    rather than failing the whole listing.
    """
    if current_year is None:
        current_year = datetime.now(UTC).year

    year_established = plausible_year_or_none(result.year_established, current_year)
    if year_established is None:
        year_established = year_from_years_in_business(result.years_in_business, current_year)

    employee_count = result.employee_count
    if employee_count is not None and employee_count < 0:
        employee_count = None

    snippets = {snippet.field: snippet.quote for snippet in result.source_snippets}

    return ListingFields(
        business_name=result.business_name,
        business_type=result.business_type,
        requires_contractor_license=result.requires_contractor_license,
        asking_price=positive_or_none(result.asking_price),
        gross_revenue=positive_or_none(result.gross_revenue),
        seller_discretionary_earnings=positive_or_none(result.seller_discretionary_earnings),
        down_payment=positive_or_none(result.down_payment),
        year_established=year_established,
        employee_count=employee_count,
        city=result.city,
        county=result.county,
        state=result.state.upper() if result.state else None,
        source_snippets=snippets,
    )


def merge_fields(from_site: ListingFields, from_model: ListingFields) -> ListingFields:
    """Combine the site's structured fields with a model's extracted ones.

    The site's own fields win: they come from the listing form the seller
    filled in, so they are more reliable than reading prose. The model
    only fills fields the site left empty.

    One exception: `business_type` comes from the model when it has one.
    Site categories are broad labels ("Service", "Lawn/Landscaping"), while
    the model describes what the business actually does, which is what
    matching needs.
    """
    merged_values = {}
    for field_name in ListingFields.model_fields:
        if field_name == "source_snippets":
            continue
        site_value = getattr(from_site, field_name)
        model_value = getattr(from_model, field_name)
        if field_name == "business_type":
            merged_values[field_name] = model_value or site_value
        else:
            merged_values[field_name] = site_value if site_value is not None else model_value

    merged_values["source_snippets"] = {**from_model.source_snippets, **from_site.source_snippets}
    return ListingFields(**merged_values)


# --- Deciding which listings to send to a model ----------------------------


def extraction_price_ceiling(saved_searches: list[SavedSearch]) -> int | None:
    """Return the highest asking price any saved search would accept.

    None means at least one search has no price limit, so no listing can
    be ruled out by price.
    """
    ceilings = []
    for saved_search in saved_searches:
        if saved_search.asking_price_max is None:
            return None
        ceilings.append(saved_search.asking_price_max)
    return max(ceilings) if ceilings else None


def needs_model_extraction(listing: Listing, price_ceiling: int | None) -> bool:
    """Decide whether a listing is worth the cost of a model call.

    Listings priced above every saved search's maximum are skipped; they
    are still stored with their plain-code fields. A listing with no stated
    price is always sent, because its price may be in the description.
    """
    asking_price = listing.fields.asking_price
    if price_ceiling is None or asking_price is None:
        return True
    return asking_price <= price_ceiling
