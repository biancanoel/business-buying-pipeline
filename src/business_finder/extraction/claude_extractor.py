"""Field extraction with Claude through the Anthropic API.

Uses structured outputs: the request includes the shared JSON schema, so
Claude's answer is always JSON in that shape. The answer is still
validated with Pydantic, because a refused or cut-off answer may not match.

The instructions are identical for every listing, so they are marked for
prompt caching. Within a run, every call after the first reads them from
the cache at a fraction of the normal input price.
"""

import anthropic
from pydantic import ValidationError

from business_finder.config import ExtractionSettings
from business_finder.extraction.extractor import (
    ExtractionError,
    ExtractionResult,
    fields_from_extraction,
    load_output_schema,
    load_system_prompt,
)
from business_finder.models import ListingFields

# Room for the model's thinking plus the JSON answer. Extraction answers
# are a few hundred tokens; the rest is headroom so answers aren't cut off.
MAX_OUTPUT_TOKENS = 8000


class ClaudeExtractor:
    """Extracts listing fields with a Claude model."""

    def __init__(self, settings: ExtractionSettings, client: anthropic.Anthropic | None = None) -> None:
        """Set up the extractor.

        `client` can be replaced in tests with a fake that never calls the
        API. By default the SDK reads ANTHROPIC_API_KEY from the environment.
        """
        self.settings = settings
        self.client = client or anthropic.Anthropic()
        self.system_prompt = load_system_prompt()
        self.output_schema = load_output_schema()

    def extract(self, listing_text: str) -> ListingFields:
        """Ask Claude for the fields stated in one listing's text.

        Raises `ExtractionError` when the answer can't be used (refused,
        cut off, or not matching the schema). API errors such as a bad key
        or an outage are not caught: they affect every listing, so the run
        should stop and report them.
        """
        response = self.client.messages.create(
            model=self.settings.model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": self.system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": f"<listing>\n{listing_text}\n</listing>",
                }
            ],
            output_config={
                "effort": self.settings.effort,
                "format": {"type": "json_schema", "schema": self.output_schema},
            },
        )

        if response.stop_reason == "refusal":
            raise ExtractionError("Claude declined to process this listing")
        if response.stop_reason == "max_tokens":
            raise ExtractionError("Claude's answer was cut off before it finished")

        answer_text = next((block.text for block in response.content if block.type == "text"), None)
        if answer_text is None:
            raise ExtractionError("Claude's answer had no text block")

        try:
            result = ExtractionResult.model_validate_json(answer_text)
        except ValidationError as error:
            raise ExtractionError(f"Claude's answer did not match the schema: {error}") from error

        return fields_from_extraction(result)
