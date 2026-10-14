"""The weekly Slack message, and posting it through an incoming webhook.

One message per saved search per run lists that week's new matches:
confirmed ones and possible ones (some facts not stated). A quiet week
posts a short "no new matches" note, so silence never means something
broke. Each match shows the listing's stable internal ID, for later
features such as a "deep dive" button.

The webhook URL is a secret (anyone with it can post to the channel), so it
comes from the environment, never from code or the repo.
"""

import os
from dataclasses import dataclass, field

import httpx

from business_finder.models import Listing, ListingSource, LocationPrecision, Match, SavedSearch

WEBHOOK_URL_ENVIRONMENT_VARIABLE = "SLACK_WEBHOOK_URL"

# Slack allows at most 50 blocks per message; each match uses two (a
# divider and its text), plus the header and a possible "more" note.
MAX_MATCHES_PER_MESSAGE = 20

# How missing facts are named to the buyer.
FACT_NAMES = {
    "asking_price": "asking price",
    "gross_revenue": "revenue",
    "seller_discretionary_earnings": "SDE",
    "years_in_business": "years in business",
    "distance": "location",
}


# How each source is named in "Also listed on" links.
SOURCE_NAMES = {
    ListingSource.BIZBEN: "BizBen",
    ListingSource.SANTA_ROSA_BUSINESS_AND_COMMERCIAL: "Santa Rosa Business & Commercial",
    ListingSource.LIBERTY_GROUP_OF_NEVADA: "Liberty Group of Nevada",
}


class SlackPostError(Exception):
    """Raised when Slack does not accept a message."""


@dataclass
class MatchDetails:
    """Everything one match entry in the message shows."""

    match: Match
    listing: Listing
    matched_type: str | None
    miles_away: float | None
    precision: LocationPrecision
    also_listed_at: list[tuple[str, str]] = field(default_factory=list)  # (site name, listing URL)


# --- Formatting --------------------------------------------------------------------------


def dollars(amount: int) -> str:
    """Full amount, e.g. $1,100,000."""
    return f"${amount:,}"


def short_dollars(amount: int) -> str:
    """Rounded amount for comparisons, e.g. $1.2M or $150K."""
    if amount >= 1_000_000:
        number, unit = amount / 1_000_000, "M"
    elif amount >= 1_000:
        number, unit = amount / 1_000, "K"
    else:
        return f"${amount}"
    text = f"{number:.1f}".rstrip("0").rstrip(".")
    return f"${text}{unit}"


def price_against_target(asking_price: int | None, saved_search: SavedSearch) -> str | None:
    """Describe where the asking price sits relative to the search's target price."""
    target = saved_search.asking_price_target
    price_max = saved_search.asking_price_max
    if asking_price is None or target is None:
        return None
    difference = asking_price - target
    if difference == 0:
        return f"at the {short_dollars(target)} target"
    if difference < 0:
        return f"{short_dollars(-difference)} under the {short_dollars(target)} target"
    description = f"{short_dollars(difference)} over the {short_dollars(target)} target"
    if price_max is not None and asking_price <= price_max:
        description += f", within the {short_dollars(price_max)} max"
    return description


def location_line(details: MatchDetails, target_city: str) -> str:
    """Where the business is, and how far from the target city."""
    fields = details.listing.fields
    state_text = fields.state or " and ".join(fields.states) or None
    parts = [fields.city, f"{fields.county} County" if fields.county else None, state_text, fields.region]
    place = ", ".join(part for part in parts if part) or "Location not stated"

    if details.miles_away is None or details.precision == LocationPrecision.UNKNOWN:
        distance = "distance unknown"
    elif details.precision == LocationPrecision.APPROXIMATE:
        distance = f"about {round(details.miles_away)} miles from {target_city}"
    else:
        distance = f"{round(details.miles_away)} miles from {target_city}"
    return f"{place} · {distance}"


def price_line(details: MatchDetails, saved_search: SavedSearch) -> str:
    asking_price = details.listing.fields.asking_price
    if asking_price is None:
        return "Asking price not stated"
    comparison = price_against_target(asking_price, saved_search)
    line = f"Asking *{dollars(asking_price)}*"
    return f"{line} ({comparison})" if comparison else line


def financials_line(details: MatchDetails) -> str | None:
    """Revenue, SDE and founding year, whichever are stated."""
    fields = details.listing.fields
    parts = []
    if fields.gross_revenue is not None:
        parts.append(f"Revenue {dollars(fields.gross_revenue)}")
    if fields.seller_discretionary_earnings is not None:
        parts.append(f"SDE {dollars(fields.seller_discretionary_earnings)}")
    if fields.year_established is not None:
        parts.append(f"Established {fields.year_established}")
    return " · ".join(parts) or None


def match_text(details: MatchDetails, saved_search: SavedSearch, target_city: str) -> str:
    """The text of one match entry."""
    kind = "Confirmed match" if details.match.is_confirmed else "Possible match"
    heading = f"*{kind}*"
    if details.matched_type:
        heading += f" · {details.matched_type[0].upper()}{details.matched_type[1:]}"
    title = details.listing.title or "Untitled listing"

    lines = [
        heading,
        f"*<{details.listing.url}|{title}>*",
        location_line(details, target_city),
        price_line(details, saved_search),
    ]
    financials = financials_line(details)
    if financials:
        lines.append(financials)
    if details.match.missing_facts:
        missing = ", ".join(FACT_NAMES.get(fact, fact) for fact in details.match.missing_facts)
        lines.append(f"_Not stated: {missing}. Ask the broker._")
    if details.also_listed_at:
        links = ", ".join(f"<{url}|{site_name}>" for site_name, url in details.also_listed_at)
        lines.append(f"Also listed on: {links}")
    lines.append(f"ID `{details.match.listing_id}`")
    return "\n".join(lines)


def section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def weekly_message(saved_search: SavedSearch, matches: list[MatchDetails], target_city: str) -> dict:
    """Build the Slack message for one search's new matches this run.

    `text` is the short summary Slack shows in notifications; `blocks` is
    the full message.
    """
    if not matches:
        summary = f"{saved_search.name}: No new matches this week"
        return {"text": summary, "blocks": [section(f"*{saved_search.name}*\nNo new matches this week.")]}

    confirmed = sum(1 for details in matches if details.match.is_confirmed)
    possible = len(matches) - confirmed
    summary = f"{saved_search.name}: {confirmed} confirmed and {possible} possible new matches"
    blocks = [section(f"*{summary}*")]
    # Confirmed matches first; the sort keeps the original order within each group.
    ordered = sorted(matches, key=lambda details: not details.match.is_confirmed)
    for details in ordered[:MAX_MATCHES_PER_MESSAGE]:
        blocks.append({"type": "divider"})
        blocks.append(section(match_text(details, saved_search, target_city)))
    if len(matches) > MAX_MATCHES_PER_MESSAGE:
        remaining = len(matches) - MAX_MATCHES_PER_MESSAGE
        blocks.append(section(f"_…and {remaining} more. They are stored in the database._"))
    return {"text": summary, "blocks": blocks}


# --- Posting ---------------------------------------------------------------------------------


class SlackWebhookNotifier:
    """Posts messages to one Slack channel through an incoming webhook."""

    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url

    @classmethod
    def from_environment(cls) -> "SlackWebhookNotifier":
        webhook_url = os.environ.get(WEBHOOK_URL_ENVIRONMENT_VARIABLE)
        if not webhook_url:
            raise RuntimeError(f"{WEBHOOK_URL_ENVIRONMENT_VARIABLE} is not set")
        return cls(webhook_url)

    def post(self, message: dict) -> None:
        """Send one message; raise `SlackPostError` if Slack rejects it.

        The error never includes the webhook URL, because it is a secret.
        """
        response = httpx.post(self.webhook_url, json=message, timeout=30)
        if response.status_code != 200:
            raise SlackPostError(f"Slack rejected the message ({response.status_code}): {response.text[:200]}")
