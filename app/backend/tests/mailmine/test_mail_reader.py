"""The copied mail reader (`app/mailmine/`): one message read from bytes, an mbox read as a stream, and the IMAP
connection's read-only check. Every sender is an invented example domain."""

import io
import time
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from app.mailmine import imap
from app.mailmine.mbox import iter_messages
from app.mailmine.message import MAX_BODY_CHARS, from_bytes, scrub_quote, sender_key
from tests.support import pin_clock


def _raw(
    subject: str, body: str, *, address: str = "no-reply@bank.example", html: bool = False, bulk: bool = False,
) -> bytes:
    kind = "text/html" if html else "text/plain"
    head = (
        f"From: Bank <{address}>\nSubject: {subject}\nDate: Mon, 05 Oct 2026 09:30:00 +0000\n"
        f"Message-ID: <1@x.test>\nMIME-Version: 1.0\nContent-Type: {kind}; charset=utf-8\n"
    )
    if bulk:
        head += "List-Unsubscribe: <mailto:u@bank.example>\n"
    return (head + "\n" + body).encode()


# --- reading a message ------------------------------------------------------------------------------------------


def test_a_message_is_read_into_its_sender_subject_day_and_the_start_of_its_text():
    seen = from_bytes(_raw("Your statement   is ready", "Hello.\n\nYour statement is attached."))
    assert seen is not None
    assert (seen.sender_key, seen.sender_name, seen.subject) == ("bank.example", "Bank", "Your statement is ready")
    assert seen.day.isoformat() == "2026-10-05" and seen.text == "Hello. Your statement is attached."
    assert seen.bulk is False


@pytest.mark.parametrize("header, zone, day", [
    ("Sat, 07 Nov 2026 03:00:00 +0530", "America/New_York", "2026-11-06"),
    ("Thu, 05 Nov 2026 23:00:00 -0800", "Pacific/Auckland", "2026-11-06"),
    ("Fri, 06 Nov 2026 20:00:00 +0000", "Pacific/Auckland", "2026-11-07"),
    ("Sat, 07 Nov 2026 03:00:00 -0000", "America/New_York", "2026-11-06"),
], ids=["sent-east-read-west", "sent-west-read-east", "utc-read-at-plus-13", "no-zone-is-utc"])
def test_a_messages_day_is_the_owners_day_not_the_senders(monkeypatch, header, zone, day):
    """Regression case: the day was the calendar day the sender's clock showed, so a message sent at
    3 am in India read as the 7th to an owner in New York for whom it was still the 6th evening. A `-0000` header says
    the sender's zone is unknown; it is read as UTC."""
    pin_clock(monkeypatch, datetime(2026, 11, 7, 12, 0, tzinfo=UTC), zone)
    seen = from_bytes(_raw("Hi", "Hello.").replace(b"Mon, 05 Oct 2026 09:30:00 +0000", header.encode()))
    assert seen is not None and seen.day == date.fromisoformat(day)


def test_a_bulk_header_marks_the_message_bulk():
    seen = from_bytes(_raw("Weekly digest", "Hi.", bulk=True))
    assert seen is not None and seen.bulk is True


def test_html_text_has_its_tags_scripts_and_styles_taken_out():
    body = "<style>p {color: red}</style><p>Hello <b>there</b></p><script>alert(1)</script>"
    seen = from_bytes(_raw("Hi", body, html=True))
    assert seen is not None and seen.text == "Hello there"


def test_a_message_with_no_date_or_no_sender_is_not_read():
    assert from_bytes(b"From: A <a@b.example>\nSubject: no date\n\nbody\n") is None
    assert from_bytes(b"Subject: no sender\nDate: Mon, 05 Oct 2026 09:30:00 +0000\n\nbody\n") is None


def test_one_company_is_one_sender_whatever_the_address():
    assert sender_key("a@mail.example.co.uk") == "example.co.uk"
    assert sender_key("b@news.example.com") == sender_key("c@billing.example.com") == "example.com"
    assert sender_key("friend@gmail.com") == "friend@gmail.com"


def test_a_quote_is_short_and_has_no_secrets_numbers_or_addresses():
    long = (
        "Policy HX-99887766 for jane.doe@example.org. Call 317-555-0123. Card 4111 1111 1111 1111. "
        "Key sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA. See https://pay.example/x?token=abc. " + "words " * 80
    )
    q = scrub_quote(long)
    assert len(q) <= 200
    for leaked in ("jane.doe", "317-555", "4111", "sk-ant", "https", "99887766"):
        assert leaked not in q, leaked


# Messages built to make a pattern take its worst time: each is a whole message a stranger could send. The first forms
# of the quote's patterns took minutes or longer on each of the first four.
HOSTILE = [
    ("a run of stars", _raw("Statement", "*" * 80 + " end")),
    ("a run of x", _raw("Statement", "x" * 60 + " end")),
    ("stars and dashes", _raw("Statement", "*-" * 400)),
    ("an account word then stars", _raw("Statement", "account " + "*" * 400)),
    ("account words in a chain", _raw("Statement", "ref-" * 5000)),
    ("one endless word as the subject", _raw("a" * 120_000, "hello")),
    ("an endless address-like subject", _raw("a.b-c+" * 20_000, "hello")),
    ("a hundred thousand less-than signs", _raw("Statement", "<" * 150_000, html=True)),
    ("unclosed script blocks", _raw("Statement", "<script>" * 18_000, html=True)),
    ("unclosed style blocks", _raw("Statement", "<style " * 18_000, html=True)),
    ("tags that never close", _raw("Statement", "<a " * 50_000, html=True)),
    ("a long run of digits", _raw("Statement", "1" * 150_000)),
    ("digit groups", _raw("Statement", "1234 " * 30_000)),
    ("currency symbols", _raw("Statement", "$1" * 70_000)),
    ("a base64 blob", _raw("Statement", "QUJD" * 40_000)),
    ("capitals and dashes", _raw("Statement", "A-" * 70_000)),
    ("a long line of one character", _raw("S" * 200, "=" * 150_000)),
    ("a private key header repeated", _raw("Statement", "-----BEGIN PRIVATE KEY----- " * 5000)),
    ("a long token", _raw("Statement", "eyJ" + "a" * 100_000)),
    ("masked digits", _raw("Statement", ("xxxx-" * 100) + "1")),
    ("parentheses", _raw("Statement", "(" * 100_000)),
]


@pytest.mark.parametrize("raw", [raw for _, raw in HOSTILE], ids=[name for name, _ in HOSTILE])
def test_a_hostile_message_is_read_fast_and_bounded(raw):
    started = time.perf_counter()
    seen = from_bytes(raw)
    scrub_quote(seen.text if seen else "")
    assert time.perf_counter() - started < 1.0
    assert seen is not None and len(seen.text) <= MAX_BODY_CHARS and len(seen.subject) <= 300


# --- an mbox file -----------------------------------------------------------------------------------------------


def _mbox(*messages: tuple[str, str]) -> bytes:
    parts = []
    for n, (subject, body) in enumerate(messages, start=1):
        head = (
            f"From {n}@xxx Mon Jan 01 00:00:00 +0000 2001\nFrom: A <a@a.example>\nSubject: {subject}\n"
            f"Date: Mon, 05 Oct 2026 09:30:00 +0000\n\n"
        )
        parts.append(head + body.replace("\nFrom ", "\n>From ") + "\n\n")
    return "".join(parts).encode()


def test_a_body_line_that_begins_from_is_not_a_new_message_and_attachments_are_cut():
    raw = _mbox(("One", "line\nFrom here on\nmore " + "x" * 400_000), ("Two", "hi"))
    parts = list(iter_messages(io.BytesIO(raw), limit=50_000))
    assert len(parts) == 2 and len(parts[0]) < 100_000


def test_the_mbox_is_read_as_a_stream_never_all_at_once():
    class OneLineAtATime(io.BytesIO):
        def read(self, *a, **k):  # a whole-file read would be a bug
            raise AssertionError("the file was read whole")

        readall = read

    raw = _mbox(*[(f"Message {i}", "text") for i in range(300)])
    seen = [from_bytes(message) for message in iter_messages(OneLineAtATime(raw))]
    assert len(seen) == 300 and all(s is not None for s in seen)


# --- the IMAP connection stays read-only ------------------------------------------------------------------------


class _Conn:
    """A server with an empty inbox that records each search it is asked."""

    def __init__(self) -> None:
        self.searches: list[str] = []

    def login(self, user, password):
        return "OK", []

    def list(self):
        return "OK", []

    def select(self, name, readonly=False):
        return "OK", [b"0"]

    def uid(self, command, *args):
        self.searches.append(args[-1])
        return "OK", [b""]


def test_the_server_is_asked_from_a_day_before_since_because_it_counts_the_days_its_own_way(monkeypatch):
    """An IMAP `SINCE` is the day the server filed a message, in the server's zone; the owner's day for it can be the
    day after. The reader keeps only what is on the owner's day, so the search is
    one day wider than the day asked for."""
    monkeypatch.setattr(imap, "_SecureIMAP", lambda *args, **kwargs: _Conn())
    inbox = imap.Inbox(SimpleNamespace(host="imap.example.test", port=993, username="me", password="secret"))
    inbox.read_headers(date(2026, 11, 7), lambda raw, ref: None, stems=("interview",))
    assert [q.split(" OR ")[0] for q in inbox.conn.searches] == ["SINCE 06-Nov-2026"]


@pytest.mark.parametrize(
    "name, args",
    [
        ("CAPABILITY", ()), ("LOGIN", ("user", "pw")), ("LIST", ()), ("EXAMINE", ('"INBOX"',)), ("LOGOUT", ()),
        ("UID", ("SEARCH", 'SINCE 01-Jan-2026 OR (SUBJECT "interview") (BODY "offer")')),
        ("UID", ("FETCH", "1,2", "(UID BODY.PEEK[HEADER.FIELDS (DATE FROM)] BODY.PEEK[TEXT]<0.100>)")),
    ],
)
def test_the_commands_the_reader_needs_pass_the_check(name, args):
    imap._check(name, args)


@pytest.mark.parametrize(
    "name, args",
    [
        ("SELECT", ('"INBOX"',)), ("STORE", ("1", "+FLAGS", "\\Seen")), ("COPY", ("1", "Trash")),
        ("EXPUNGE", ()), ("DELETE", ("Old",)), ("APPEND", ("INBOX",)),
        ("UID", ("STORE", "1", "+FLAGS", "\\Deleted")), ("UID", ("COPY", "1", "Trash")),
        ("UID", ("FETCH", "1", "(BODY[TEXT])")), ("UID", ("FETCH", "1", "(RFC822)")),
        ("UID", ("SEARCH", "ALL ; DELETE INBOX")), ("UID", ("SEARCH", "UNKNOWNKEY")),
    ],
)
def test_any_other_command_is_refused_before_it_is_sent(name, args):
    with pytest.raises(imap.ReadOnlyViolation):
        imap._check(name, args)
