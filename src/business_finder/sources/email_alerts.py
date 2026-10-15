"""Reading listing-alert emails from the dedicated alerts inbox.

Sites that block automated reading (BizBuySell, BizScout, BusinessesForSale.com)
send listing alerts by email instead. This module does the part
that is the same for every site:

- reads the inbox over IMAP, read-only, without marking anything as read;
- recognizes which listing site sent each email, by the sender's domain,
  and ignores everything else (welcome emails, Google's own messages);
- turns each email into plain text, keeping link addresses, which lead to
  each listing;
- reports which emails have not been processed yet, by Message-ID.

Turning an alert's text into listings is site-specific and comes later.

The inbox credentials come from ALERTS_EMAIL_ADDRESS and
ALERTS_EMAIL_APP_PASSWORD (a Gmail app password, spaces allowed).
"""

import email
import email.policy
import hashlib
import imaplib
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr, parsedate_to_datetime

from bs4 import BeautifulSoup

from business_finder.models import ListingSource
from business_finder.sources.html_text import html_to_text

IMAP_SERVER = "imap.gmail.com"
IMAP_PORT = 993
# How far back each run looks. Processed emails are remembered, so a long
# window only costs a little reading; it lets a missed week catch up.
DEFAULT_LOOKBACK_DAYS = 30

# Listing sites by the domain their alert emails come from.
ALERT_SENDER_DOMAINS = {
    "bizbuysell.com": ListingSource.BIZBUYSELL,
    "bizscout.com": ListingSource.BIZSCOUT,
    "businessesforsale.com": ListingSource.BUSINESSES_FOR_SALE,
}


class AlertInboxError(Exception):
    """Raised when the alerts inbox cannot be read. Never carries a credential."""


@dataclass
class AlertEmail:
    """One email from the alerts inbox, as text."""

    message_id: str
    source: ListingSource | None
    sender: str
    subject: str
    received_at: datetime
    text: str


# --- Recognizing the sender ---------------------------------------------------------------


def source_for_sender(from_header: str) -> ListingSource | None:
    """Return the listing site that sent an email, or None for anyone else.

    The sender's domain must be the site's domain or a subdomain of it, so
    look-alikes such as "bizbuysell.com.example-offers.net" or
    "notbizbuysell.com" are not recognized.
    """
    _, address = parseaddr(from_header)
    domain = address.rpartition("@")[2].lower().strip(">").strip()
    for site_domain, source in ALERT_SENDER_DOMAINS.items():
        if domain == site_domain or domain.endswith("." + site_domain):
            return source
    return None


# --- Turning an email into text -------------------------------------------------------------


def html_to_text_with_links(html: str) -> str:
    """Convert HTML to text, writing each link as "text <address>" so listing links survive."""
    soup = BeautifulSoup(html, "html.parser")
    for invisible in soup.select("script, style, head"):
        invisible.decompose()
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        label = link.get_text(" ", strip=True)
        if href.startswith(("http://", "https://")) and href not in label:
            link.replace_with(f"{label} <{href}>" if label else f"<{href}>")
    return html_to_text(str(soup))


def body_text(message: email.message.EmailMessage) -> str:
    """The email's plain-text version if it has one, otherwise its HTML as text."""
    plain = message.get_body(preferencelist=("plain",))
    if plain is not None:
        return plain.get_content()
    html = message.get_body(preferencelist=("html",))
    if html is not None:
        return html_to_text_with_links(html.get_content())
    return ""


def stable_id_for(message: email.message.EmailMessage) -> str:
    """The email's Message-ID, or, if it has none, a fingerprint of its sender, date and subject."""
    message_id = (message.get("Message-ID") or "").strip()
    if message_id:
        return message_id
    fingerprint = "|".join(str(message.get(header, "")) for header in ("From", "Date", "Subject"))
    return "no-message-id:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]


def received_time(message: email.message.EmailMessage) -> datetime:
    date_header = message.get("Date")
    if not date_header:
        return datetime.now(UTC)
    sent = parsedate_to_datetime(str(date_header))
    return sent if sent.tzinfo else sent.replace(tzinfo=UTC)


def parse_email(raw: bytes) -> AlertEmail:
    """Turn one raw email, as an inbox delivers it, into an `AlertEmail`."""
    message = email.message_from_bytes(raw, policy=email.policy.default)
    sender = str(message.get("From", ""))
    return AlertEmail(
        message_id=stable_id_for(message),
        source=source_for_sender(sender),
        sender=sender,
        subject=str(message.get("Subject", "")),
        received_at=received_time(message),
        text=body_text(message),
    )


def new_alert_emails(emails: list[AlertEmail], processed_message_ids: set[str]) -> list[AlertEmail]:
    """Keep emails from listing sites that have not been processed yet."""
    return [
        alert
        for alert in emails
        if alert.source is not None and alert.message_id not in processed_message_ids
    ]


# --- Reading the inbox ------------------------------------------------------------------------


class AlertInbox:
    """Reads the alerts inbox over IMAP, read-only."""

    def __init__(self, address: str, app_password: str) -> None:
        self.address = address.strip()
        # Gmail shows app passwords in groups of four; the spaces are not part of it.
        self.app_password = app_password.replace(" ", "")

    @classmethod
    def from_environment(cls) -> "AlertInbox":
        address = os.environ.get("ALERTS_EMAIL_ADDRESS", "")
        app_password = os.environ.get("ALERTS_EMAIL_APP_PASSWORD", "")
        if not address or not app_password:
            raise AlertInboxError("ALERTS_EMAIL_ADDRESS and ALERTS_EMAIL_APP_PASSWORD must both be set")
        return cls(address, app_password)

    def recent_emails(self, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> list[AlertEmail]:
        """Return every email in the inbox from the last `lookback_days` days.

        Emails are fetched with BODY.PEEK, which leaves them unread, and the
        inbox is opened read-only. Errors are reported without the server's
        own message, which could echo the login.
        """
        since = (datetime.now(UTC) - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
        try:
            with imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT, timeout=60) as mailbox:
                mailbox.login(self.address, self.app_password)
                mailbox.select("INBOX", readonly=True)
                _, found = mailbox.search(None, "SINCE", since)
                emails = []
                for number in found[0].split():
                    _, parts = mailbox.fetch(number, "(BODY.PEEK[])")
                    raw = next(part[1] for part in parts if isinstance(part, tuple))
                    emails.append(parse_email(raw))
                return emails
        except imaplib.IMAP4.error:
            raise AlertInboxError("Gmail refused the login or a request; check the alerts address and app password") from None
        except OSError:
            raise AlertInboxError("Could not reach Gmail's IMAP server") from None
