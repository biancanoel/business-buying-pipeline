"""Tests for the polite HTTP client. Requests go to a fake transport, never the network."""


from business_finder.sources.html_text import html_to_text
from business_finder.sources.polite_http import (
    DEFAULT_USER_AGENT,
    user_agent_from_environment,
)


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
