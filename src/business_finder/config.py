"""Settings that change between environments, read from environment variables.

Secrets (such as ANTHROPIC_API_KEY) are not read here: the Anthropic SDK
reads its own key from the environment, so it never passes through our code.
"""

import os
from dataclasses import dataclass

DEFAULT_EXTRACTION_PROVIDER = "anthropic"
DEFAULT_EXTRACTION_MODEL = "claude-haiku-5-5"
DEFAULT_EXTRACTION_EFFORT = "low"


@dataclass(frozen=True)
class ExtractionSettings:
    """Which model backend extracts listing fields, and how hard it thinks.

    `effort` is low by default because extraction is transcription, not
    reasoning, and runs on thousands of listings.
    """

    provider: str = DEFAULT_EXTRACTION_PROVIDER
    model: str = DEFAULT_EXTRACTION_MODEL
    effort: str = DEFAULT_EXTRACTION_EFFORT

    @classmethod
    def from_environment(cls) -> "ExtractionSettings":
        """Read settings from BUSINESS_FINDER_EXTRACTION_* variables, with defaults."""
        return cls(
            provider=os.environ.get("BUSINESS_FINDER_EXTRACTION_PROVIDER", DEFAULT_EXTRACTION_PROVIDER),
            model=os.environ.get("BUSINESS_FINDER_EXTRACTION_MODEL", DEFAULT_EXTRACTION_MODEL),
            effort=os.environ.get("BUSINESS_FINDER_EXTRACTION_EFFORT", DEFAULT_EXTRACTION_EFFORT),
        )
