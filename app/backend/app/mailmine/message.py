"""One message, as the mail reader sees it: header fields plus a bounded slice of the body.

Everything the reader keeps of a message is in `Seen`: who it is from, the subject, the day, the start of its text and
whether it is bulk mail. The raw message is read once and dropped (nothing here writes it anywhere). The same function
reads a message from an mbox file and one a mail server hands over, so both paths read the same way.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import UTC, date
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime

from app.agents.security import redact_secrets
from app.textutil import ellipsize, squash
from app.timeutil import owner_zone

#: How much of the body is read (characters of text, after tags are removed).
MAX_BODY_CHARS = 12_000
#: The longest quote `scrub_quote` returns by default.
QUOTE_CHARS = 200
#: How much of one message is read from a file or a server at all (bytes). Past this is attachments.
MAX_MESSAGE_BYTES = 160_000
#: How much of an HTML body is looked at for text (characters, before the tags are taken out).
MAX_HTML_CHARS = 100_000
#: The longest subject or sender name that is read (characters). A mail header can be a hundred thousand characters of
#: one word.
MAX_SUBJECT_CHARS = 300
MAX_NAME_CHARS = 200
#: The most of an attached calendar invite that is read (characters).
MAX_CALENDAR_CHARS = 20_000
#: The most that any pattern below is shown of one text. The quote's patterns are the ones that can cost the most per
#: character, so what they see is held to a few hundred characters whatever the message holds.
MAX_SCRUB_CHARS = 600

#: Headers that mark bulk mail (newsletters, lists, offers).
BULK_HEADERS = ("list-unsubscribe", "list-id", "precedence")
#: Mailbox providers whose addresses belong to many different people: their senders are told apart by the whole
#: address, not by the domain.
SHARED_MAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com", "icloud.com", "me.com",
    "aol.com", "proton.me", "protonmail.com", "fastmail.com", "msn.com",
})
#: A second-level label that sits before a two-letter country code ("example.co.uk" is the company).
SECOND_LEVELS = frozenset({"co", "com", "org", "net", "gov", "ac", "edu"})

# Every pattern here runs on mail from strangers, so each is written to take time in proportion to its text:
# no repeat inside a repeat, no two repeats that can share a character, and a pattern that starts on a run of
# characters begins only at the run's start. `tests/test_mail_reader.py` holds each to the inputs that made the
# earlier forms take minutes (a run of `*` after an account word, a run of `<`, one endless word).
_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\d)(?:\+?\d{1,2}[ .-])?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)")
_LONG_NUMBER = re.compile(r"(?<![\d$])\d{6,}(?!\d)|(?<![\d$])(?:\d{4}[ -]){2,}\d{2,4}(?!\d)")
# A masked account number: runs of at least two of x * • # with one space or dash between runs, then digits. The
# earlier form, `(?:[xX*•#]{2,}[ -]?)+`, split a run of `*` in exponentially many ways before it gave up (80 of them
# took longer than anyone waited). Here a run is taken whole and begins where the run begins.
_MASK = r"[xX*•#]"
_MASKED = re.compile(
    rf"(?<!{_MASK}){_MASK}{{2,}}+(?:[ -]{_MASK}{{2,}}+)*+[ -]?\d{{2,}}"
    r"|\b(?:acct|account|policy|member|ref|reference|confirmation)\b[ .:#-]*(?=[A-Z0-9-]*\d)[A-Z0-9-]{5,}", re.I,
)
# A plate, a policy code, a reference: capitals and digits together.
_CODE = re.compile(r"\b(?=[A-Z0-9-]*\d)(?=[A-Z0-9-]*[A-Z])[A-Z0-9-]{5,}\b")
# A tag cannot hold another tag's start, so a stray `<` costs only the text up to the next one (the earlier form
# looked to the end of the message from every `<`).
_TAG = re.compile(r"<[^<>]*>")
_BLOCK_START = re.compile(r"<(script|style)\b", re.I)
_BLOCK_END = {name: re.compile(rf"</{name}\s*>", re.I) for name in ("script", "style")}


@dataclass(frozen=True)
class Seen:
    sender_key: str
    sender_name: str
    subject: str
    day: date
    #: The start of the message's text, tags removed, at most `MAX_BODY_CHARS` characters.
    text: str
    #: Carries a bulk-mail header (`BULK_HEADERS`).
    bulk: bool
    #: The message's own id (its `Message-ID` header), which says a message was read already; empty when it has none.
    message_id: str = ""
    #: The sender's address, lower case (`sender_key` is the company's, this is the whole of it).
    address: str = ""
    #: The text of an attached calendar invite (a `text/calendar` part or a `.ics` file), at most `MAX_CALENDAR_CHARS`
    #: characters; empty when there is none. The only attachment that is read (docs/PRD.md 14.3).
    calendar: str = ""


# --- the text rules ---------------------------------------------------------------------------------------


def sender_key(address: str) -> str:
    """The sender, the same for every address of one company: its domain (`mail.example.co.uk` is `example.co.uk`);
    an address at a mailbox provider is its whole address."""
    addr = (address or "")[:320].strip().lower()
    domain = addr.rsplit("@", 1)[-1] if "@" in addr else addr
    if not domain:
        return "unknown"
    if domain in SHARED_MAIL_DOMAINS:
        return addr
    labels = [p for p in domain.split(".") if p]
    keep = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in SECOND_LEVELS else 2
    return ".".join(labels[-keep:])


def sender_label(name: str, address: str) -> str:
    """The sender's name as a person would say it: the display name, or the company part of the domain."""
    clean = squash((name or "")[: MAX_NAME_CHARS * 2])[:MAX_NAME_CHARS].strip("\"' ")
    generic = re.fullmatch(r"[\W\d_]+", clean) or re.search(r"no-?reply|do-?not-?reply", clean, re.I)
    if clean and "@" not in clean and not generic:
        return ellipsize(clean, 60)
    key = sender_key(address)
    base = key.split("@")[-1].split(".")
    label = base[-3] if len(base) >= 3 and len(base[-1]) == 2 and base[-2] in SECOND_LEVELS else base[0]
    return label.replace("-", " ").title()


def scrub_quote(text: str, limit: int = QUOTE_CHARS) -> str:
    """A quote safe to keep as a source: addresses, links, phone and account numbers and anything that looks like a
    secret are replaced, then it is cut to `limit` characters."""
    out = _URL.sub("", (text or "")[:MAX_SCRUB_CHARS])
    out = _EMAIL.sub("[email]", out)
    out = _PHONE.sub("[number]", out)
    out = _MASKED.sub("[number]", out)
    out = _LONG_NUMBER.sub("[number]", out)
    out = _CODE.sub("[code]", out)
    out = redact_secrets(out)
    return ellipsize(squash(out), limit)


def _strip_markup(text: str) -> str:
    """HTML as plain text: script and style blocks removed whole (an unclosed one takes the rest, which is code),
    every other tag replaced by a space. One pass over the text, however many `<` it holds."""
    out: list[str] = []
    at = 0
    while block := _BLOCK_START.search(text, at):
        out.append(text[at : block.start()])
        close = _BLOCK_END[block.group(1).lower()].search(text, block.end())
        if close is None:
            at = len(text)
            break
        at = close.end()
    out.append(text[at:])
    return _TAG.sub(" ", "".join(out))


def _body_text(msg) -> str:
    text = ""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is not None:
            text = part.get_content()[:MAX_HTML_CHARS]
            if part.get_content_subtype() == "html":
                text = html.unescape(_strip_markup(text))
    except Exception:  # an odd charset or a cut-off part: read what can be read
        try:
            raw = msg.get_payload(decode=True)
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(msg.get_payload() or "")
        except Exception:
            text = ""
    return squash(text)[:MAX_BODY_CHARS]


def _calendar_text(msg) -> str:
    """The text of the first calendar invite attached to the message, or "". Only a `text/calendar` part, or a file
    named `.ics`, is opened; no other attachment is ever read."""
    try:
        for part in msg.walk():
            if part.is_multipart():
                continue
            name = str(part.get_filename() or "").lower()
            if part.get_content_type() != "text/calendar" and not name.endswith(".ics"):
                continue
            raw = part.get_payload(decode=True)
            text = raw.decode(part.get_content_charset() or "utf-8", "replace") if isinstance(raw, bytes) else ""
            if "BEGIN:VCALENDAR" in text.upper():
                return text[:MAX_CALENDAR_CHARS]
    except Exception:  # a part cut off by the size limit, an odd charset: no invite
        return ""
    return ""


def _day(msg) -> date | None:
    """The owner's calendar day of the message's `Date` (`JOBSEARCH_TIMEZONE`), not the day on the sender's clock: a
    message sent at 3 am on the 7th in India is still the evening of the 6th in New York. A header that says its zone is
    unknown (`-0000`) is read as UTC."""
    try:
        when = parsedate_to_datetime(str(msg["date"]))
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    when = when if when.tzinfo is not None else when.replace(tzinfo=UTC)
    day = when.astimezone(owner_zone()).date()
    return day if 1995 <= day.year <= date.today().year + 1 else None


def from_bytes(raw: bytes) -> Seen | None:
    """Read one message (its header block and what follows) into `Seen`. None when it has no date or sender."""
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw[:MAX_MESSAGE_BYTES])
        return from_message(msg)
    except Exception:  # one bad message never stops a read
        return None


def from_message(msg) -> Seen | None:
    day = _day(msg)
    name, address = parseaddr(str(msg["from"] or ""))
    if day is None or not address:
        return None
    return Seen(
        sender_key=sender_key(address), sender_name=sender_label(name, address),
        subject=squash(str(msg["subject"] or "")[: MAX_SUBJECT_CHARS * 3])[:MAX_SUBJECT_CHARS], day=day,
        text=_body_text(msg), bulk=any(msg[h] is not None for h in BULK_HEADERS),
        message_id=squash(str(msg["message-id"] or ""))[:300], address=address.strip().lower()[:320],
        calendar=_calendar_text(msg),
    )
