"""Has the owner already applied? Go reads the owner's mail for an application's confirmation before it opens a role
(the owner, 2026-10-16; `go_checks.py`).

Read-only: the mail reader (`app.mailmine.imap`, derived from another project's code, see app/PROVENANCE.md) is the only
way in, and it sends
nothing that can change a mailbox. One read covers every role Go checks: the mail of the last `DAYS` days that carries
an application word, then the text of the messages that look like a confirmation ("Thank you for applying", "application
received", or a job system's or job board's confirmation sender ("your application was sent").

A role is matched against each confirmation by its employer (the role's company, and its employer record's name and
aliases) and its title: both, and it is a sure match (`SURE`: the role moves to Applied); the employer with only part of
the title, or none of it, is an unsure one (`UNSURE`: noted on the role's history, nothing moves). The employer is
named by its whole name, so a word of it inside another company's name is not it ("Northfield Group" is not in
"Harbor Northfield"), and an alias of one word counts only when the message names the role beside it (`Names`).

The login is the one saved in the app (`mail/login.py`, under Agent Settings). With none saved, there is no read and
Go says so. The password goes to the mail reader and nowhere else: it is never logged, printed or put in a reply."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.mailmine import imap, message
from app.mailmine.message import Seen
from app.sites import registry

#: How far back the mail is read, in days.
DAYS = 60
#: The most confirmations whose text is read in one check.
MOST_MESSAGES = 400
#: How long one read of the mail may take, in seconds.
READ_SECONDS = 90.0
#: The words the server is asked for (a server matches part of a word, so "appl" forms are found by these).
STEMS = ("applying", "application", "applied")
SURE, UNSURE = "sure", "unsure"
#: What a confirmation says.
CONFIRMS = re.compile(
    r"thanks?(?: you)? for (?:your )?(?:applying|application)|"
    r"(?:application|cv|resume) (?:was |has been |is )?(?:received|submitted|sent|complete)|"
    r"received your (?:application|cv|resume)|your application (?:was|has been|is) (?:sent|received|submitted)|"
    r"we have received your|you applied (?:for|to)", re.I)
#: The job systems that send an application's confirmation from their own domain (the sender's registrable domain):
#: the runbooks that say so (`mail.confirms`).
SYSTEM_SENDERS = registry.mail_sets()[2]


@dataclass(frozen=True)
class Login:
    """What the mail reader opens a mailbox with. The password is left out of its `repr`, so no log can show it."""

    host: str
    port: int
    username: str
    password: str = field(repr=False)


def _fold(text: str | None) -> str:
    """Lowercase words with single spaces and a space at each end, so a phrase is found by `f" {words} " in text`."""
    return " " + " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split()) + " "


def _from_a_system(seen: Seen) -> bool:
    return any(seen.sender_key == d or seen.sender_key.endswith("." + d) for d in SYSTEM_SENDERS)


def might_confirm(seen: Seen) -> bool:
    """From the header alone: a message worth reading the text of (a confirmation's subject, or a job system's mail
    about an application)."""
    if CONFIRMS.search(seen.subject):
        return True
    return _from_a_system(seen) and "appl" in seen.subject.lower()


def confirms(seen: Seen) -> bool:
    """A message that confirms an application: it says so in its subject or in the first 4,000 characters of its text,
    whoever sent it."""
    return bool(CONFIRMS.search(seen.subject) or CONFIRMS.search(seen.text[:4000]))


@dataclass(frozen=True)
class Names:
    """What an employer is called in a message. `keys` are whole names: "Northfield Group" is the name "northfield
    group", not "northfield", which another company's name ("Harbor Northfield") holds as well. `loose` are aliases of
    one word, which another company may go by too ("Quillon"): a message names the employer by one only when it names
    the role beside it."""

    keys: list[str] = field(default_factory=list)
    loose: list[str] = field(default_factory=list)


def employer_names(own: Sequence[str | None], aliases: Sequence[str | None] = ()) -> Names:
    """The names a role's employer goes by in mail: `own` (the role's company and its employer record's name) and the
    record's `aliases`. A name of several words goes by its matching key (`key_of`); one that comes down to a single
    word goes by the whole name instead (`whole_key_of`). An alias that is still one word is loose. The address of a
    job system (an alias that was a board's host) names no employer: every company's mail on that system holds it."""
    from app.jobsearch import ats
    from app.jobsearch.employers.core import key_of, whole_key_of  # employers reads much of the job search

    def keyed(name: str | None) -> str:
        if ats.parse(name) is not None:
            return ""
        key = key_of(name)
        return key if " " in key else whole_key_of(name)

    keys = list(dict.fromkeys(k for k in map(keyed, own) if len(k) >= 2))
    more = list(dict.fromkeys(k for k in map(keyed, aliases) if len(k) >= 2 and k not in keys))
    return Names(keys + [k for k in more if " " in k], [k for k in more if " " not in k])


#: Words of a title that say nothing of the role.
_TITLE_FILLER = frozenset({"a", "an", "and", "the", "of", "for", "in", "at", "to", "with", "or"})


def _holds_the_role(hay: str, title: str) -> bool:
    """Whether a folded message names the role: two words of its title (all of them, when it has one)."""
    words = [w for w in _fold(title).split() if w not in _TITLE_FILLER]
    return bool(words) and sum(f" {w} " in hay for w in words) >= min(2, len(words))


def match(seen: Seen, names: Names, title: str) -> str | None:
    """How a confirmation matches a role: `SURE` (its employer and its whole title), `UNSURE` (its employer, and part
    of the title or none), or None (not this employer). The employer is named by a whole name, in the message or in the
    sender's address; an alias of one word names it only with the role beside it."""
    hay = _fold(f"{seen.subject} {seen.sender_name} {seen.text}")
    sender = seen.sender_key.replace("-", "")
    named = any(f" {k} " in hay for k in names.keys) or any(
        len(k.replace(" ", "")) >= 4 and k.replace(" ", "") in sender for k in names.keys)
    if not named and not (any(f" {k} " in hay for k in names.loose) and _holds_the_role(hay, title)):
        return None
    wanted = _fold(title).strip()
    return SURE if wanted and f" {wanted} " in hay else UNSURE


def best(mails: list[Seen], names: Names, title: str) -> tuple[str | None, Seen | None]:
    """The strongest match among the confirmations, the newest first within it."""
    found: tuple[str | None, Seen | None] = (None, None)
    for seen in sorted(mails, key=lambda s: s.day, reverse=True):
        said = match(seen, names, title)
        if said == SURE:
            return SURE, seen
        if said == UNSURE and found[0] is None:
            found = (UNSURE, seen)
    return found


#: How a mailbox is opened (tests put a fake one in).
open_inbox: Callable[[Login], imap.Inbox] = imap.Inbox


def read_confirmations(who: Login, today: date) -> list[Seen]:
    """The confirmations in the owner's mail of the last `DAYS` days. Blocking (run it in a thread); raises
    `imap.MailReadError` in plain words when the mailbox cannot be read."""
    since = today - timedelta(days=DAYS)
    wanted: dict[str, list[int]] = {}
    count = 0
    out: list[Seen] = []

    def header(raw: bytes, ref: imap.Ref | None) -> None:
        nonlocal count
        seen = message.from_bytes(raw)
        if seen is None or ref is None or seen.day < since or count >= MOST_MESSAGES or not might_confirm(seen):
            return
        wanted.setdefault(ref[0], []).append(ref[1])
        count += 1

    def body(raw: bytes, _ref: imap.Ref | None) -> None:
        seen = message.from_bytes(raw)
        if seen is not None and seen.day >= since and confirms(seen):
            out.append(seen)

    with open_inbox(who) as inbox:
        inbox.read_headers(since, header, stems=STEMS)
        if wanted:
            inbox.read_bodies(wanted, body)
    return out
