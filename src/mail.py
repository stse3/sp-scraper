"""Gmail over IMAP (read requests) and SMTP (send replies), using an app password."""
from __future__ import annotations

import html
import imaplib
import logging
import os
import re
import smtplib
from contextlib import contextmanager
from dataclasses import dataclass
from email import message_from_bytes
from email.message import EmailMessage, Message
from email.policy import default as default_policy
from email.utils import parseaddr
from pathlib import Path

log = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
NETWORK_TIMEOUT = 60

# Never reply to these: two automated systems answering each other loop forever.
_AUTOMATED_SENDER = re.compile(r"(no-?reply|do-?not-?reply|mailer-daemon|postmaster|bounces?)", re.IGNORECASE)


@dataclass
class MailConfig:
    address: str
    password: str

    @classmethod
    def from_env(cls) -> "MailConfig":
        address = os.environ.get("GMAIL_ADDRESS", "").strip()
        password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")  # Google shows it in groups of 4
        if not address or not password:
            raise SystemExit("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env (see .env.example)")
        return cls(address, password)


@dataclass
class IncomingEmail:
    uid: str
    sender_name: str
    sender_addr: str
    subject: str
    body: str
    message_id: str
    references: str
    skip_reason: str | None = None  # set for mail the agent must not answer


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def first_name(display_name: str) -> str | None:
    """A first name for the greeting, or None when the display name is missing or an address."""
    name = display_name.strip().strip('"')
    if not name or "@" in name:
        return None
    if "," in name:  # "Last, First"
        name = name.split(",", 1)[1].strip()
    return name.split()[0] if name else None


def _skip_reason(msg: Message, sender_addr: str, own_address: str, allow_self: bool) -> str | None:
    if sender_addr.lower() == own_address.lower() and not allow_self:
        return "sent by the agent's own address"
    if _AUTOMATED_SENDER.search(sender_addr.split("@")[0]):
        return "automated sender"
    if (msg.get("Auto-Submitted") or "no").strip().lower() != "no":
        return "Auto-Submitted header"
    if (msg.get("Precedence") or "").strip().lower() in {"bulk", "junk", "list"}:
        return "bulk mail"
    if msg.get("List-Id") or msg.get("List-Unsubscribe"):
        return "mailing list"
    return None


def _body_text(msg: Message) -> str:
    try:
        part = msg.get_body(preferencelist=("plain",))
        if part is not None:
            return part.get_content()
        part = msg.get_body(preferencelist=("html",))
        if part is not None:
            return html.unescape(re.sub(r"<[^>]+>", " ", part.get_content()))
    except (LookupError, UnicodeError):  # unknown charset etc.
        pass
    return ""


def parse_message(uid: str, raw: bytes, own_address: str, allow_self: bool = False) -> IncomingEmail:
    msg = message_from_bytes(raw, policy=default_policy)
    sender_name, sender_addr = parseaddr(str(msg.get("From", "")))
    return IncomingEmail(
        uid=uid,
        sender_name=sender_name,
        sender_addr=sender_addr,
        subject=str(msg.get("Subject", "")).strip(),
        body=_body_text(msg),
        message_id=str(msg.get("Message-ID", "")).strip(),
        references=" ".join(str(msg.get("References", "")).split()),
        skip_reason=_skip_reason(msg, sender_addr, own_address, allow_self),
    )


def build_reply(cfg: MailConfig, incoming: IncomingEmail, body: str, attachment: Path | None = None) -> EmailMessage:
    subject = incoming.subject or "your request"
    msg = EmailMessage()
    msg["From"] = cfg.address
    msg["To"] = incoming.sender_addr
    msg["Subject"] = subject if re.match(r"(?i)re:", subject) else f"Re: {subject}"
    if incoming.message_id:  # keeps the reply in the sender's thread
        msg["In-Reply-To"] = incoming.message_id
        msg["References"] = f"{incoming.references} {incoming.message_id}".strip()
    msg["Auto-Submitted"] = "auto-replied"
    msg.set_content(body)
    if attachment is not None:
        msg.add_attachment(
            attachment.read_bytes(), maintype="application", subtype="zip", filename=attachment.name
        )
    return msg


# --------------------------------------------------------------------------
# Network
# --------------------------------------------------------------------------

@contextmanager
def _imap(cfg: MailConfig):
    imap = imaplib.IMAP4_SSL(IMAP_HOST, timeout=NETWORK_TIMEOUT)
    try:
        imap.login(cfg.address, cfg.password)
        imap.select("INBOX")
        yield imap
    finally:
        try:
            imap.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def fetch_unseen(cfg: MailConfig, allow_self: bool = False) -> list[IncomingEmail]:
    """All unread mail. BODY.PEEK leaves it unread until `mark_seen` is called."""
    emails = []
    with _imap(cfg) as imap:
        _, data = imap.uid("search", None, "UNSEEN")
        for uid in data[0].split():
            _, parts = imap.uid("fetch", uid, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if raw:
                emails.append(parse_message(uid.decode(), raw, cfg.address, allow_self))
    return emails


def mark_seen(cfg: MailConfig, uid: str) -> None:
    with _imap(cfg) as imap:
        imap.uid("store", uid, "+FLAGS", "\\Seen")


def send(cfg: MailConfig, msg: EmailMessage) -> None:
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=NETWORK_TIMEOUT) as smtp:
        smtp.login(cfg.address, cfg.password)
        smtp.send_message(msg)
