"""Source adapters: one module per listing site or feed.

Every adapter turns its site's data into the same `Listing` shape, so the
rest of the pipeline never needs to know where a listing came from.
"""

from collections.abc import Iterator
from typing import Protocol

from business_finder.models import Listing, ListingSource


class SourceAdapter(Protocol):
    """What the pipeline needs from every source adapter."""

    source: ListingSource

    def fetch_listings(self) -> Iterator[Listing]:
        """Yield every current listing this source has for the target states.

        Adapters collect broadly (all listings in the target states) rather
        than per saved search; matching happens later against stored listings.
        """
        ...
