"""An HTTP client that is nice and polite to the sites it fetches from :)

Listing sites are other people's servers. This client waits between
requests, identifies itself with a clear user agent, and fails loudly on
errors instead of retrying aggressively. It never tries to get past bot
checks: if a site blocks automated access, the adapter for that site
should be dropped or replaced with an email-alert feed.
"""

import os
import time
from collections.abc import Callable

import httpx

USER_AGENT_ENVIRONMENT_VARIABLE = "BUSINESS_FINDER_USER_AGENT"
DEFAULT_USER_AGENT = "BusinessFinder/0.1 (personal business search; low-frequency)"
DEFAULT_SECONDS_BETWEEN_REQUESTS = 10.0
DEFAULT_TIMEOUT_SECONDS = 30.0


class BlockedByBotCheckError(Exception):
    """Raised when a site answers with a bot-check page instead of content.

    This is a signal to stop fetching from that site, not to try harder.
    """


def user_agent_from_environment() -> str:
    """Return the user agent to send, from the environment if set.

    The user agent is configurable so the owner can choose whether to add
    contact details. It is never filled in automatically with personal data.
    """
    return os.environ.get(USER_AGENT_ENVIRONMENT_VARIABLE, DEFAULT_USER_AGENT)


def looks_like_bot_check(response: httpx.Response) -> bool:
    """Return True if the response is a Cloudflare-style bot-check page."""
    if response.headers.get("cf-mitigated") == "challenge":
        return True
    return "<title>Just a moment...</title>" in response.text


class PoliteHttpClient:
    """Fetches pages one at a time with a fixed pause between requests."""

    def __init__(
        self,
        seconds_between_requests: float = DEFAULT_SECONDS_BETWEEN_REQUESTS,
        user_agent: str | None = None,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Set up the client.

        `http_client`, `sleep` and `clock` can be replaced in tests so they
        run instantly and never touch the network.
        """
        self.seconds_between_requests = seconds_between_requests
        self.http_client = http_client or httpx.Client(
            headers={"User-Agent": user_agent or user_agent_from_environment()},
            timeout=DEFAULT_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        self.sleep = sleep
        self.clock = clock
        self.time_of_last_request: float | None = None

    def get_text(self, url: str) -> str:
        """Fetch a URL and return the response body as text.

        Waits first if the previous request was too recent. Raises
        `BlockedByBotCheckError` for bot-check pages and
        `httpx.HTTPStatusError` for any other error status.
        """
        self.wait_until_next_request_is_allowed()
        response = self.http_client.get(url)
        self.time_of_last_request = self.clock()

        if looks_like_bot_check(response):
            raise BlockedByBotCheckError(f"Bot check returned for {url}")
        response.raise_for_status()
        return response.text

    def wait_until_next_request_is_allowed(self) -> None:
        """Sleep for whatever is left of the pause since the last request."""
        if self.time_of_last_request is None:
            return
        seconds_since_last_request = self.clock() - self.time_of_last_request
        seconds_to_wait = self.seconds_between_requests - seconds_since_last_request
        if seconds_to_wait > 0:
            self.sleep(seconds_to_wait)

    def close(self) -> None:
        """Close the underlying connection pool."""
        self.http_client.close()
