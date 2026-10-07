"""Reading structured fields out of listing text with a language model."""

from business_finder.config import ExtractionSettings
from business_finder.extraction.extractor import Extractor


def make_extractor(settings: ExtractionSettings) -> Extractor:
    """Build the extractor the settings ask for.

    Only Anthropic is supported today; an Ollama extractor can be added
    here later without changing the rest of the pipeline.
    """
    if settings.provider == "anthropic":
        from business_finder.extraction.claude_extractor import ClaudeExtractor

        return ClaudeExtractor(settings)
    raise ValueError(f"Unknown extraction provider: {settings.provider!r}")
