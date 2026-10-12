"""A model's yes/no call on whether a listing's business fits a saved search's types.

Business types are written in each site's and seller's own words ("maid
service", "janitorial", "day spa offering facials"), so plain matching
against the buyer's list is not enough. A model makes the call and gives a
one-line reason, which becomes the "why it matched" line in notifications.

Like extraction, the instructions and answer schema are provider-neutral
files; `ClaudeBusinessTypeJudge` is the first implementation.
"""

import copy
import hashlib
import json
from importlib import resources
from typing import Protocol

import anthropic
from pydantic import BaseModel, ConfigDict, ValidationError

from business_finder.config import ExtractionSettings
from business_finder.extraction.extractor import ExtractionError
from business_finder.models import Listing, SavedSearch

PROMPTS_PACKAGE = "business_finder.extraction.prompts"
JUDGE_PROMPT_FILE = "judge_business_type.md"
JUDGE_SCHEMA_FILE = "judge_business_type.schema.json"

# Placeholder in the schema file, replaced with each search's own business types.
BUSINESS_TYPES_PLACEHOLDER = "{business_types}"

# The answer is a sentence, a type and a boolean; the rest is room for thinking.
MAX_OUTPUT_TOKENS = 4000


class JudgeAnswer(BaseModel):
    """The model's answer, exactly as the schema file describes it.

    The reason comes first so the call follows from the reasoning.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str
    matched_type: str | None
    fits: bool


class BusinessTypeJudge(Protocol):
    """Anything that can decide whether a listing fits a search's business types."""

    version: str

    def judge(self, listing: Listing, saved_search: SavedSearch) -> JudgeAnswer: ...


def load_judge_prompt() -> str:
    return resources.files(PROMPTS_PACKAGE).joinpath(JUDGE_PROMPT_FILE).read_text()


def load_judge_schema() -> dict:
    return json.loads(resources.files(PROMPTS_PACKAGE).joinpath(JUDGE_SCHEMA_FILE).read_text())


def judge_schema_for(saved_search: SavedSearch) -> dict:
    """Return the answer schema for one search.

    The matched type may only be one of that search's own business types
    (or null), so the model cannot invent a category.
    """
    schema = copy.deepcopy(load_judge_schema())
    for option in schema["properties"]["matched_type"]["anyOf"]:
        if option.get("enum") == [BUSINESS_TYPES_PLACEHOLDER]:
            option["enum"] = list(saved_search.business_types)
    return schema


def judge_version() -> str:
    """A short fingerprint of the judge's instructions and answer schema.

    Stored with each judgment, so changing either re-judges past listings.
    """
    text = load_judge_prompt() + load_judge_schema_text()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def load_judge_schema_text() -> str:
    return resources.files(PROMPTS_PACKAGE).joinpath(JUDGE_SCHEMA_FILE).read_text()


def check_answer_is_consistent(answer: JudgeAnswer, saved_search: SavedSearch) -> None:
    """Reject answers that contradict themselves or name a type the search does not have.

    A "fits" answer must name one of the search's types, and a "does not
    fit" answer must name none. A rejected answer is retried on a later run.
    """
    if answer.fits and answer.matched_type is None:
        raise ExtractionError("Claude said the business fits but named no business type")
    if not answer.fits and answer.matched_type is not None:
        raise ExtractionError(f"Claude said the business does not fit but named {answer.matched_type!r}")
    if answer.matched_type is not None and answer.matched_type not in saved_search.business_types:
        raise ExtractionError(f"Claude named {answer.matched_type!r}, which is not one of the search's business types")


def buyer_criteria_text(saved_search: SavedSearch) -> str:
    """Describe the search's business types and rules for the instructions."""
    lines = ["Business types:"]
    lines += [f"- {business_type}" for business_type in saved_search.business_types]
    if saved_search.business_type_guidance:
        lines += ["", "Rules:", saved_search.business_type_guidance]
    return "\n".join(lines)


def listing_text_for_judge(listing: Listing) -> str:
    """The listing as sent to the judge, with the earlier reading of its business type."""
    license_answers = {True: "yes", False: "no", None: "not known"}
    return (
        f"Earlier reading - business type: {listing.fields.business_type or 'not known'}; "
        f"work needs a contractor's license: {license_answers[listing.fields.requires_contractor_license]}\n\n"
        f"<listing>\n{listing.raw_text}\n</listing>"
    )


class ClaudeBusinessTypeJudge:
    """Makes business-type calls with a Claude model."""

    def __init__(self, settings: ExtractionSettings, client: anthropic.Anthropic | None = None) -> None:
        self.settings = settings
        self.client = client or anthropic.Anthropic()
        self.prompt_template = load_judge_prompt()
        self.version = judge_version()

    def judge(self, listing: Listing, saved_search: SavedSearch) -> JudgeAnswer:
        """Ask Claude whether the listing's business fits the search's types.

        The instructions, including the buyer's criteria, are the same for
        every listing of a search, so they are marked for caching. Raises
        `ExtractionError` for an unusable or self-contradicting answer; API
        errors are not caught.
        """
        instructions = self.prompt_template.replace("{buyer_criteria}", buyer_criteria_text(saved_search))
        response = self.client.messages.create(
            model=self.settings.model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=[{"type": "text", "text": instructions, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": listing_text_for_judge(listing)}],
            output_config={
                "effort": self.settings.effort,
                "format": {"type": "json_schema", "schema": judge_schema_for(saved_search)},
            },
        )
        if response.stop_reason == "refusal":
            raise ExtractionError("Claude declined to judge this listing")
        if response.stop_reason == "max_tokens":
            raise ExtractionError("Claude's answer was cut off before it finished")
        answer_text = next((block.text for block in response.content if block.type == "text"), None)
        if answer_text is None:
            raise ExtractionError("Claude's answer had no text block")
        try:
            answer = JudgeAnswer.model_validate_json(answer_text)
        except ValidationError as error:
            raise ExtractionError(f"Claude's answer did not match the schema: {error}") from error
        check_answer_is_consistent(answer, saved_search)
        return answer
