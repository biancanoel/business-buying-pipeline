"""Tests for the polite HTTP client. Requests go to a fake transport, never the network."""

import httpx
import pytest

from business_finder.sources.html_text import html_to_text
from business_finder.sources.polite_http import (
    DEFAULT_USER_AGENT,
    BlockedByBotCheckError,
    PoliteHttpClient,
    user_agent_from_environment,
)


class FakeClock:
    """A clock the test moves forward by hand, with a sleep that advances it."""

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(handler, clock: FakeClock, seconds_between_requests: float = 10.0) -> PoliteHttpClient:
    """Build a client whose requests are answered by `handler`."""
    return PoliteHttpClient(
        seconds_between_requests=seconds_between_requests,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=clock.sleep,
        clock=clock.time,
    )


def answer_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text="<html>listing</html>")


def test_first_request_does_not_wait():
    clock = FakeClock()
    client = make_client(answer_ok, clock)

    assert client.get_text("https://example.com/a") == "<html>listing</html>"
    assert clock.sleeps == []


def test_waits_between_requests():
    clock = FakeClock()
    client = make_client(answer_ok, clock)

    client.get_text("https://example.com/a")
    clock.now += 3
    client.get_text("https://example.com/b")

    assert clock.sleeps == [7]


def test_does_not_wait_when_enough_time_has_passed():
    clock = FakeClock()
    client = make_client(answer_ok, clock)

    client.get_text("https://example.com/a")
    clock.now += 15
    client.get_text("https://example.com/b")

    assert clock.sleeps == []


def test_bot_check_page_raises_instead_of_returning_it():
    def answer_with_bot_check(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="<html><title>Just a moment...</title></html>")

    client = make_client(answer_with_bot_check, FakeClock())

    with pytest.raises(BlockedByBotCheckError):
        client.get_text("https://example.com/a")


def test_error_status_raises():
    def answer_not_found(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="missing")

    client = make_client(answer_not_found, FakeClock())

    with pytest.raises(httpx.HTTPStatusError):
        client.get_text("https://example.com/a")


def test_user_agent_comes_from_environment(monkeypatch):
    monkeypatch.setenv("BUSINESS_FINDER_USER_AGENT", "TestAgent/1.0")

    assert user_agent_from_environment() == "TestAgent/1.0"


def test_user_agent_has_a_default_without_personal_details(monkeypatch):
    monkeypatch.delenv("BUSINESS_FINDER_USER_AGENT", raising=False)

    assert user_agent_from_environment() == DEFAULT_USER_AGENT
    assert "@" not in DEFAULT_USER_AGENT


def test_html_to_text_keeps_paragraphs_and_decodes_entities():
    html = "<p>Great  location &amp; staff.</p><ul><li>Recurring revenue</li><li>Owner retiring</li></ul>"

    assert html_to_text(html) == "Great location & staff.\nRecurring revenue\nOwner retiring"
