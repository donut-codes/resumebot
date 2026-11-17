"""Read the owner's inbox over IMAP, read-only.

Deterministic code, with the mailbox login opened for one read. No model is in it, and no tool runner. It gives the
caller the same thing a mailbox export does: one raw message each, to read with `message.from_bytes`, and nothing else
leaves it.

**Read-only, enforced twice.** Only these commands are ever sent: `CAPABILITY`, `LOGIN`, `LIST`, `EXAMINE` (never
`SELECT`), `UID SEARCH`, `UID FETCH` of `BODY.PEEK` items only, and `LOGOUT`. The connection refuses anything else
before it is sent (`ReadOnlyViolation`), so nothing here can store a flag, move, copy, delete, expunge or mark a message
seen, whatever a later change tries. `tests/test_mail_reader.py` holds the check to the commands above.

**What is fetched.** For each message in the window: `DATE FROM SUBJECT` and a few headers that say it is bulk mail,
plus the first `BODY_BYTES` of its text (`BODY.PEEK[TEXT]<0.n>`), in batches. The inbox, and the folders the server
marks as all mail or archive (so a provider's all-mail folder is read as well, and a message in both is counted once).

**The login never leaves this module's call:** it is passed to `imaplib` for `LOGIN` and nowhere else. A failure is
said in plain words that hold neither the login nor anything the server answered.
"""

from __future__ import annotations

import imaplib
import re
import ssl
from collections.abc import Callable
from datetime import date, timedelta

#: How much of each message's text is read, in bytes. A notice's markup can be most of its first few thousand bytes,
#: and what the message says comes after it.
BODY_BYTES = 24_000
#: How many messages are asked for at once.
BATCH = 100
TIMEOUT_SECONDS = 60
#: The longest single line a mail server may answer with, in characters (a search's list of message numbers).
MAX_REPLY_LINE = 32_000_000
#: The headers read from every message that matches: enough to say who, what and when, and to tell bulk mail.
HEADER_FIELDS = "DATE FROM SUBJECT LIST-UNSUBSCRIBE LIST-ID PRECEDENCE MESSAGE-ID"
#: What a message's body needs to be read: its type and how it is encoded.
BODY_HEADER_FIELDS = f"{HEADER_FIELDS} CONTENT-TYPE CONTENT-TRANSFER-ENCODING"
#: How the folders are searched: `terms` asks the server for the words in subject or body (any IMAP server);
#: `x_gm_raw` asks in the server's own search language (`X-GM-RAW`; `providers.yaml`: `search: x_gm_raw`).
TERMS, X_GM_RAW = "terms", "x_gm_raw"
#: The bulk-mail tabs a server that files mail into them is asked to leave out (offers, notifications).
LEAVE_OUT_TABS = ("promotions", "social", "forums")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_ALLOWED = frozenset({"CAPABILITY", "LOGIN", "LIST", "EXAMINE", "UID", "LOGOUT", "NOOP"})
_ALLOWED_UID = frozenset({"SEARCH", "FETCH"})
#: A fetch item that would set \Seen, or read without peeking.
_NOT_PEEK = re.compile(r"RFC822(?!\.SIZE|\.HEADER)|(?<!PEEK)\bBODY\[|(?<!PEEK)\bBINARY\[", re.I)
_LIST_LINE = re.compile(r'\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$')
_MESSAGE_ID = re.compile(rb"(?im)^message-id:[ \t]*(<[^>\r\n]+>)")
_UID = re.compile(rb"\bUID (\d+)")
#: What a SEARCH may say outside quotes: these words, parentheses and dates. Anything quoted is only text to look for.
_SEARCH_KEYS = frozenset({"SINCE", "BEFORE", "ON", "OR", "NOT", "ALL", "SUBJECT", "BODY", "TEXT", "X-GM-RAW"})
_SEARCH_TOKEN = re.compile(r'\s*(?:"(?:[^"\\\r\n]|\\[^\r\n])*"|\(|\)|[A-Za-z0-9-]+)')
_SEARCH_DATE = re.compile(r"\d{2}-(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-\d{4}")
#: A word a search may be asked for: letters, digits and a hyphen (the data file holds nothing else).
_SEARCH_WORD = re.compile(r"[A-Za-z0-9-]+")


class MailReadError(Exception):
    """The inbox couldn't be read. The message is plain words, safe to show; it holds no login."""


class ReadOnlyViolation(RuntimeError):
    """Something tried to send a command that isn't read-only. Never sent."""


def _check_search(criteria: str) -> None:
    """A SEARCH asks a question and changes nothing, but what is sent is still held to the forms this module builds:
    only the search words above, parentheses, dates and quoted text (no line break can be in it, so nothing can follow a
    quote's end and become a command of its own)."""
    at = 0
    while at < len(criteria):
        token = _SEARCH_TOKEN.match(criteria, at)
        if token is None:
            raise ReadOnlyViolation("a search may only use the search words this module builds")
        at = token.end()
        word = token.group().strip()
        if word.startswith('"') or word in ("(", ")") or word.upper() in _SEARCH_KEYS or _SEARCH_DATE.fullmatch(word):
            continue
        raise ReadOnlyViolation("a search may only use the search words this module builds")


def _check(name: str, args: tuple) -> None:
    command = name.upper()
    if command not in _ALLOWED:
        raise ReadOnlyViolation(f"{command} is not a read-only command")
    if command == "UID":
        sub = str(args[0]).upper() if args else ""
        if sub not in _ALLOWED_UID:
            raise ReadOnlyViolation(f"UID {sub} is not a read-only command")
        if sub == "SEARCH":
            _check_search(" ".join(str(a) for a in args[1:]))
        if sub == "FETCH":
            items = " ".join(str(a) for a in args[2:])
            if "BODY.PEEK[" not in items.upper() or _NOT_PEEK.search(items.upper().replace("BODY.PEEK[", "PEEK[")):
                raise ReadOnlyViolation("a fetch must peek, so no message is marked seen")


class _Guard:
    """Mixed into the connection classes: refuse anything but the read-only commands before it is sent."""

    def _command(self, name, *args):  # imaplib's own signature
        _check(name, args)
        return super()._command(name, *args)


class _PlainIMAP(_Guard, imaplib.IMAP4):
    pass


class _SecureIMAP(_Guard, imaplib.IMAP4_SSL):
    pass


def _imap_date(day: date) -> str:
    return f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"


#: What a mail server's refusal of a login means, in plain words. The server's own text is never shown (a server
#: can echo the username back), only the sentence for a reason recognised here.
REFUSALS: tuple[tuple[str, str], ...] = (
    ("application-specific password required",
     "It needs an app password, not the account's own password. Make one and put it in Settings > Access."),
    ("web login required", "The mail account wants a sign-in on the web first. Sign in once there, then try again."),
    ("log in via your web browser",
     "The mail account wants a sign-in on the web first. Sign in once there, then try again."),
    ("invalid credentials", "The username or the app password is wrong. Check both in Settings > Access."),
    ("authenticationfailed", "The username or the app password is wrong. Check both in Settings > Access."),
)


def refusal_words(server_text: str) -> str | None:
    """The plain sentence for a mail server's refusal of a login, or None when the reason isn't one known here."""
    text = server_text.lower()
    return next((words for marker, words in REFUSALS if marker in text), None)


def _quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def folders(conn) -> list[str]:
    """The folders worth reading: the inbox, and any the server marks all mail or archive. Never sent, spam, trash
    or drafts."""
    typ, data = conn.list()
    if typ != "OK":
        return ["INBOX"]
    found = ["INBOX"]
    for raw in data or []:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")
        m = _LIST_LINE.search(line)
        if m is None:
            continue
        flags = m.group("flags").lower().split()
        name = m.group("name").strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        if "\\noselect" in flags or name.upper() == "INBOX":
            continue
        if "\\all" in flags or "\\archive" in flags:
            found.append(name)
    return found


# --- what the server is asked for -------------------------------------------------------------------------------


def _term(word: str) -> str | None:
    return word if _SEARCH_WORD.fullmatch(word) else None


def _or_tree(keys: list[str]) -> str:
    """`keys` joined by IMAP's two-operand OR, as a balanced tree (a long chain is as deep as it is long, and servers
    differ in how deep they go)."""
    if len(keys) == 1:
        return keys[0]
    middle = len(keys) // 2
    left, right = _or_tree(keys[:middle]), _or_tree(keys[middle:])
    return f"OR {f'({left})' if left.startswith('OR ') else left} {f'({right})' if right.startswith('OR ') else right}"


def terms_search(since: date, stems: list[str] | tuple[str, ...]) -> str:
    """The SEARCH for any IMAP server: mail from `since` whose subject or body has any of the words (a server
    matches a part of a word, so a stem finds its forms). Words that are not plain letters and digits are left out."""
    keys = [k for w in stems if _term(w) for k in (f'SUBJECT "{w}"', f'BODY "{w}"')]
    if not keys:
        return f"SINCE {_imap_date(since)}"
    return f"SINCE {_imap_date(since)} {_or_tree(keys)}"


def raw_search(since: date, words: list[str] | tuple[str, ...]) -> str:
    """The text of a search in the server's own language (`X-GM-RAW`, answered as the provider's search box would):
    mail from `since` with any of the words, leaving out the promotions, social and forums tabs."""
    terms = " OR ".join(w for w in words if _term(w))
    leave_out = " ".join(f"-category:{tab}" for tab in LEAVE_OUT_TABS)
    return f"after:{since:%Y/%m/%d} ({terms}) {leave_out}" if terms else f"after:{since:%Y/%m/%d} {leave_out}"


def _uids(data) -> list[int]:
    found = data[0].split() if data and data[0] else []
    return [int(n) for n in found if n.isdigit()]


def _messages(data) -> list[tuple[int | None, bytes]]:
    """`(uid, raw message)` for each message in a FETCH reply: the header block, then the text slice if one was asked
    for. A server may put the UID anywhere in a message's reply."""
    out: list[tuple[int | None, bytes]] = []
    head = body = b""
    uid: int | None = None
    started = False
    for item in data or []:
        text = item[0] if isinstance(item, tuple) else item
        if not isinstance(text, bytes):
            continue
        found = _UID.search(text)
        if isinstance(item, tuple) and re.match(rb"\s*\d+ \(", text):
            if started:
                out.append((uid, head + body))
            head = body = b""
            uid = None
            started = True
        if found is not None:
            uid = int(found.group(1))
        if not isinstance(item, tuple):
            continue
        if b"HEADER.FIELDS" in text.upper():
            head = item[1]
        else:
            body = item[1]
    if started:
        out.append((uid, head + body))
    return out


#: One message found by the search: its raw header block (and text slice), and where it is (`(folder, uid)`).
Ref = tuple[str, int]


class Inbox:
    """A read-only connection to a mail server, opened with the login for one read, in two steps:

    1. `read_headers`: one `UID SEARCH` per folder for the mail that carries any of the words asked for (the server
       does the filtering, so nothing else is downloaded), then only the headers of what matched, in batches.
    2. `read_bodies`: a bounded slice of the text of the messages named, one by one.

    `matched`, `headers` and `bodies` count messages for each step. Use it as a context manager."""

    def __init__(
        self, login, *, secure: bool = True, batch: int = BATCH, body_bytes: int = BODY_BYTES,
    ) -> None:
        self.batch, self.body_bytes = batch, body_bytes
        self.matched = self.headers = self.bodies = 0
        self._seen_ids: set[int] = set()
        # A search over years of a big mailbox can answer with one line of every message's number: past imaplib's
        # 1,000,000-character limit, which reads as "the connection dropped". This runs
        # only in the reader's own process, so raising the limit touches nothing else.
        imaplib._MAXLINE = max(imaplib._MAXLINE, MAX_REPLY_LINE)
        try:
            if secure:
                self.conn = _SecureIMAP(
                    login.host, login.port or 993, ssl_context=ssl.create_default_context(), timeout=TIMEOUT_SECONDS,
                )
            else:
                self.conn = _PlainIMAP(login.host, login.port or 143, timeout=TIMEOUT_SECONDS)
        except (OSError, ssl.SSLError, imaplib.IMAP4.error):
            raise MailReadError(
                f"Couldn't reach the inbox at {login.host}. Check the address in Settings > Access."
            ) from None
        try:
            self.conn.login(_quote(login.username), login.password)  # imaplib quotes the password, not the name
        except imaplib.IMAP4.error as exc:
            self.close()
            why = refusal_words(str(exc))
            raise MailReadError(
                "The inbox didn't accept that login. " + (why or "Check the username and the app password in "
                                                          "Settings > Access.")
            ) from None
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> Inbox:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.conn.logout()
        except Exception:  # closing a connection that is already gone
            pass

    def _search(self, since: date, flavour: str, stems, words) -> list[int]:
        """One `UID SEARCH` in the folder that is open. A server that doesn't know that search is asked the
        general way."""
        if flavour == X_GM_RAW:
            try:
                typ, found = self.conn.uid("SEARCH", "X-GM-RAW", _quote(raw_search(since, words)))
            except imaplib.IMAP4.error:
                typ, found = "NO", None
            if typ == "OK":
                return _uids(found)
        typ, found = self.conn.uid("SEARCH", terms_search(since, stems))
        return _uids(found) if typ == "OK" else []

    def read_headers(
        self, since: date, on_message: Callable[[bytes, Ref | None], None],
        on_progress: Callable[[int], None] | None = None, *, flavour: str = TERMS,
        stems: list[str] | tuple[str, ...] = (), words: list[str] | tuple[str, ...] = (),
    ) -> int:
        """Search the folders for `stems` (or, for `x_gm_raw`, `words`) from `since` and hand each match's header
        block to `on_message` with its place. The inbox and the folders marked all mail or archive are searched; with
        `x_gm_raw` the all-mail folder, which holds everything, is searched alone. A message found in two folders is
        handed over once. Returns how many were.

        The server's `SINCE` is the day it filed a message, by its own clock; the day the owner gives a message
        (`message.from_bytes`) can be the day after, so the search starts a day earlier than `since` and the caller
        keeps only the messages whose own day is from `since` on."""
        fetch = f"(UID BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])"
        try:
            names = folders(self.conn)
            if flavour == X_GM_RAW:
                names = names[1:] or names[:1]
            for name in names:
                typ, _ = self.conn.select(_quote(name), readonly=True)  # EXAMINE: nothing can be changed
                if typ != "OK":
                    continue
                ids = self._search(since - timedelta(days=1), flavour, stems, words)
                self.matched += len(ids)
                for i in range(0, len(ids), self.batch):
                    typ, data = self.conn.uid("FETCH", ",".join(str(n) for n in ids[i : i + self.batch]), fetch)
                    if typ != "OK":
                        continue
                    for uid, raw in _messages(data):
                        key = _MESSAGE_ID.search(raw)
                        if key is not None:
                            digest = hash(key.group(1))
                            if digest in self._seen_ids:
                                continue
                            self._seen_ids.add(digest)
                        self.headers += 1
                        on_message(raw, (name, uid) if uid is not None else None)
                    if on_progress is not None:
                        on_progress(self.headers)
        except MailReadError:
            raise
        except (OSError, ssl.SSLError, imaplib.IMAP4.error):
            raise MailReadError("The connection to the inbox dropped before it was all read. Try again.") from None
        return self.headers

    def read_bodies(self, wanted: dict[str, list[int]], on_message: Callable[[bytes, Ref | None], None]) -> int:
        """Fetch the first `body_bytes` of the text of each message in `wanted` (`{folder: [uid]}`), with the
        headers a reader needs, and hand each to `on_message`. Returns how many were."""
        fetch = f"(UID BODY.PEEK[HEADER.FIELDS ({BODY_HEADER_FIELDS})] BODY.PEEK[TEXT]<0.{self.body_bytes}>)"
        try:
            for name, uids in wanted.items():
                if not uids:
                    continue
                typ, _ = self.conn.select(_quote(name), readonly=True)
                if typ != "OK":
                    continue
                for i in range(0, len(uids), self.batch):
                    typ, data = self.conn.uid("FETCH", ",".join(str(n) for n in uids[i : i + self.batch]), fetch)
                    if typ != "OK":
                        continue
                    for uid, raw in _messages(data):
                        self.bodies += 1
                        on_message(raw, (name, uid) if uid is not None else None)
        except (OSError, ssl.SSLError, imaplib.IMAP4.error):
            raise MailReadError("The connection to the inbox dropped before it was all read. Try again.") from None
        return self.bodies


