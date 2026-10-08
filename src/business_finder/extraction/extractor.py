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

# Facts where the site's value and the model's reading of the text should
# agree; a difference is recorded as a conflict for the buyer to check.
FIELDS_CHECKED_FOR_CONFLICTS = {
    "asking_price",
    "gross_revenue",
    "seller_discretionary_earnings",
    "down_payment",
    "year_established",
    "employee_count",
    "city",
    "county",
    "state",
}


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
    region: str | None
    source_snippets: list[SourceSnippet]
    conflict_notes: list[str]


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


def single_state_code_or_none(state: str | None) -> str | None:
    """Keep the state only if it is one two-letter code.

    The schema can't force the format, and a model sometimes answers
    "CA, OR" for a business in several states. A listing in several states
    has no single state, so anything but one code becomes None.
    """
    if state is None:
        return None
    code = state.strip().upper()
    if len(code) == 2 and code.isalpha():
        return code
    return None


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
        state=single_state_code_or_none(result.state),
        region=result.region,
        conflict_notes=list(result.conflict_notes),
        source_snippets=snippets,
    )


def normalized_for_comparison(field_name: str, value: object) -> object:
    """Put a value in a form where equal facts compare equal ("Sonoma County" == "sonoma")."""
    if isinstance(value, str):
        text = value.strip().casefold()
        if field_name == "county":
            text = text.removesuffix(" county").strip()
        return text
    return value


def disagreement_note(field_name: str, site_value: object, model_value: object) -> str | None:
    """Describe a disagreement between the site's value and the model's, if there is one.

    Founding years may differ by one, because "in business N years" only
    gives the year approximately.
    """
    if site_value is None or model_value is None:
        return None
    if field_name == "year_established" and abs(site_value - model_value) <= 1:
        return None
    if normalized_for_comparison(field_name, site_value) == normalized_for_comparison(field_name, model_value):
        return None
    return f"{field_name}: listing details say {site_value!r}, listing text says {model_value!r}"


def merge_fields(from_site: ListingFields, from_model: ListingFields) -> ListingFields:
    """Combine the site's structured fields with a model's extracted ones.

    The site's own fields win: they come from the listing form the seller
    filled in, so they are more reliable than reading prose. The model
    only fills fields the site left empty. Where both have a value and
    they disagree, the site's value is kept and the disagreement is added
    to `conflict_notes`, alongside any conflicts the model reported.

    One exception: `business_type` comes from the model when it has one.
    Site categories are broad labels ("Service", "Lawn/Landscaping"), while
    the model describes what the business actually does, which is what
    matching needs.
    """
    merged_values = {}
    conflict_notes = list(from_model.conflict_notes)
    for field_name in ListingFields.model_fields:
        if field_name in {"source_snippets", "conflict_notes", "states"}:
            continue
        site_value = getattr(from_site, field_name)
        model_value = getattr(from_model, field_name)
        if field_name == "business_type":
            merged_values[field_name] = model_value or site_value
            continue
        if field_name in FIELDS_CHECKED_FOR_CONFLICTS:
            note = disagreement_note(field_name, site_value, model_value)
            if note:
                conflict_notes.append(note)
        merged_values[field_name] = site_value if site_value is not None else model_value

    merged_values["states"] = from_site.states or from_model.states
    merged_values["conflict_notes"] = conflict_notes
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
