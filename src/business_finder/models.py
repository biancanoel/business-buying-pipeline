"""Pydantic models shared by every part of the pipeline.

These models are the contract between the source adapters, the extractor,
storage, matching and the notifiers. Every adapter returns a `Listing`, no
matter which site it came from, so the rest of the pipeline never needs to
know where a listing was found.

Money is stored as whole US dollars (int). Listing sites never show cents for
prices, revenue or SDE, and integers avoid floating-point rounding surprises.
"""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, NonNegativeInt, PositiveInt, model_validator


def utc_now() -> datetime:
    """Return the current time in UTC.

    Used as a default factory so every timestamp is timezone-aware and
    comparable, whichever machine (laptop or GitHub Actions) runs the pipeline.
    """
    return datetime.now(UTC)


class ListingSource(StrEnum):
    """The listing sites and email feeds the pipeline ingests from."""

    TRANSWORLD = "transworld"
    BIZBEN = "bizben"
    SUNBELT = "sunbelt"
    BUSINESSES_FOR_SALE = "businessesforsale"
    BIZBUYSELL = "bizbuysell"
    BIZSCOUT = "bizscout"
    SMB_DEAL_HUNTER = "smb_deal_hunter"
    MURPHY_BUSINESS = "murphy_business"
    BUSINESSMART = "businessmart"


class LocationPrecision(StrEnum):
    """How precisely a listing's location is known.

    Sites report location at different levels (city, county, state or a
    vague region). Listings with vague locations are kept and tagged rather
    than dropped, so the buyer can decide whether to look closer.
    """

    EXACT = "exact"
    APPROXIMATE = "approximate"
    UNKNOWN = "unknown"


class ListingStatus(StrEnum):
    """Where the buyer is with a listing."""

    NEW = "new"
    INTERESTED = "interested"
    PASSED = "passed"
    CONTACTED = "contacted"


class ListingFields(BaseModel):
    """Structured facts about a listing, parsed from the site or by Claude.

    Every field is optional on purpose. A missing value means "the listing
    did not say", never "zero". The extractor must leave a field as None
    rather than guess.
    """

    business_name: str | None = None
    business_type: str | None = Field(
        default=None,
        description="The kind of business in plain words, e.g. 'residential cleaning'.",
    )
    requires_contractor_license: bool | None = Field(
        default=None,
        description=(
            "Whether the work needs a contractor's license (for example a "
            "California CSLB license). Used to tell yard care apart from "
            "licensed landscaping."
        ),
    )

    asking_price: NonNegativeInt | None = None
    gross_revenue: NonNegativeInt | None = None
    seller_discretionary_earnings: NonNegativeInt | None = Field(
        default=None,
        description="SDE, sometimes labeled 'cash flow' or 'discretionary earnings'.",
    )
    down_payment: NonNegativeInt | None = None

    year_established: int | None = Field(default=None, ge=1800, le=2100)
    employee_count: NonNegativeInt | None = None

    city: str | None = None
    county: str | None = None
    state: str | None = None

    source_snippets: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Optional map from field name to the text the value was taken "
            "from, so extracted values can be spot-checked."
        ),
    )


class Listing(BaseModel):
    """One business-for-sale listing from one source.

    `id` is our own stable identifier. It never changes, so Slack messages
    and future features (such as a "deep dive" button) can refer to it.
    `source` plus `source_listing_id` identify the listing on its site and
    are what deduplication checks.
    """

    id: UUID = Field(default_factory=uuid4)
    source: ListingSource
    source_listing_id: str = Field(
        min_length=1,
        description="The site's own ID for the listing, or its URL if the site has none.",
    )
    url: str
    title: str | None = None
    raw_text: str = Field(description="The listing text as fetched, kept for re-extraction.")
    raw_data: dict | None = Field(
        default=None,
        description=(
            "The site's own structured record, when it has one. Kept so "
            "listings can be re-parsed later without fetching them again."
        ),
    )
    fields: ListingFields = Field(default_factory=ListingFields)

    # Filled in by the geography step, not by the source adapters.
    location_precision: LocationPrecision = LocationPrecision.UNKNOWN
    latitude: float | None = None
    longitude: float | None = None
    distance_miles: float | None = None

    first_seen_at: datetime = Field(default_factory=utc_now)
    last_seen_at: datetime = Field(default_factory=utc_now)
    status: ListingStatus = ListingStatus.NEW


class SavedSearch(BaseModel):
    """A buyer's search criteria, stored as data rather than code.

    There is only one saved search today, but ingestion is kept separate
    from matching so many saved searches (and later many users) can be
    matched against the same stored listings.
    """

    id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1)

    business_types: list[str] = Field(
        min_length=1,
        description="The kinds of business the buyer wants, in plain words.",
    )
    business_type_guidance: str | None = Field(
        default=None,
        description=(
            "Extra rules for deciding whether a listing's business type fits, "
            "such as which licenses rule a business out."
        ),
    )

    minimum_revenue: NonNegativeInt | None = None
    minimum_seller_discretionary_earnings: NonNegativeInt | None = None
    asking_price_min: NonNegativeInt | None = None
    asking_price_max: NonNegativeInt | None = None
    asking_price_target: NonNegativeInt | None = Field(
        default=None,
        description="The price the buyer is aiming for; notifications show each listing relative to it.",
    )
    minimum_years_in_business: NonNegativeInt | None = None

    target_city: str
    target_state: str
    max_distance_miles: PositiveInt | None = Field(
        default=None,
        description="How far from the target city a business may be. None means no distance limit yet.",
    )

    notify_slack_channel: str | None = Field(
        default=None,
        description="Channel name for display only; the webhook URL is a secret in the environment.",
    )
    notify_email_address: str | None = None

    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def check_asking_price_range(self) -> "SavedSearch":
        """Reject price settings that contradict each other.

        A range where the minimum is above the maximum, or a target outside
        the range, would silently match nothing, so fail loudly instead.
        """
        price_min = self.asking_price_min
        price_max = self.asking_price_max
        target = self.asking_price_target

        if price_min is not None and price_max is not None and price_min > price_max:
            raise ValueError("asking_price_min must not be greater than asking_price_max")
        if target is not None and price_max is not None and target > price_max:
            raise ValueError("asking_price_target must not be greater than asking_price_max")
        if target is not None and price_min is not None and target < price_min:
            raise ValueError("asking_price_target must not be less than asking_price_min")
        return self


class Match(BaseModel):
    """A stored listing that meets a saved search's criteria.

    A listing matches a given saved search at most once. `notified_at`
    stays None until the match has been sent, so a failed Slack post is
    retried on the next run instead of being lost.
    """

    id: UUID = Field(default_factory=uuid4)
    saved_search_id: UUID
    listing_id: UUID
    match_summary: str = Field(description="One line explaining why the listing matched.")
    matched_at: datetime = Field(default_factory=utc_now)
    notified_at: datetime | None = None
