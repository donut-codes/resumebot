"""Whether a role the app meets is one the owner already has (docs/PRD.md 28.8).

A role is stored under its match key (`gates.match_key`): the company and the title, lowercased, with the spaces
folded. That key is exact on purpose, because it is what keeps one row for one role. But one role is written more than
one way: the tracker has "Director, Product" where the employer's own site says "Director - Product", and a board says
"Acme, Inc." where the tracker says "Acme". Set side by side letter for letter, the second writing is a new role, and a
role the owner already applied to comes back as a new role.

So every place that sets a known role aside asks here: the search round's dedupe and the repeats inside one report
(`actions.py`), the nightly filter (`sift.py`) and the browser reader (`browser/reader.py`). A role is one the owner has
when

1. its match key is one a stored role has, or
2. its company and title read the same once folded (`writings`): the company as `employers.key_of` folds it ("Acme,
   Inc." and "Acme and Company" are "Acme", "(via Someone)" and "(Retail)" are left out), the title as
   `employer_posting.title_tokens` folds it (case, spacing, every comma and dash, "&" and "and", "Sr." and "Senior",
   "II" and "2", and a tag that only says where, "(Remote)"); or
3. a link of one names the same posting as a link of the other (`marks`): the same host and the same id, the id being
   what the job system puts at the end of the address or in its job parameter, and the companies fold to the same
   employer (`employers.key_of`), because a tracker row can carry another employer's posting link and a link across
   two employers is not a match. Both must carry an id. A link with no id says nothing, because two roles can share a
   careers page.

One kind of role has no company to hold a link's match to: a job the owner added by pasting its link, until research
reads the posting and names it (`models.is_unnamed`, docs/PRD.md 24.10). It is known by each job id its links carry
whatever the company of what asks (`Known.add`, `unnamed`), so the employer's own listing of that job, or the scout's
report of it, met before the name is known, is that role and not a second one. Once research names it, it is an
ordinary role again. A job board's host is folded to the board's name for these ids (`board_marks`): the same
the board job on `uk.board.example` and on `board.example` is one job.

A role's company is every name its employer goes by (`known_roles`), not only the one it was found under: a merge of
two employer records renames the one it keeps and leaves each role's company as it was found, and a board is read
under the record's name. Another employer with a similar name is another employer.

Nothing that tells two jobs apart is folded. Every word of the title that says what the job is stays, so "Director, X"
and "Senior Director, X" are two roles, and so are "Product Owner" and "Senior AI Product Owner". Where a fold would
lose words it is not used: a title with letters the fold cannot read (Japanese, an accent) is compared whole, and "C++"
and "C#" keep their symbol, because "C", "C++" and "C#" are three jobs. The place is not folded in either: it was never
part of a role's identity (the stored key has no place), so the same title in two cities is one role here as it always
was.

`Known` is the whole question, asked of an index of roles.

Pure functions and an index, and one read of the owner's roles (`known_roles`, kept between calls and checked against
the rows on every one). Services flush; this writes nothing.

The folds are pure functions of their arguments, and every role and posting asks for them again (a company's name is
folded once for each of its roles, a title twice, an address twice, and the index made again after any change asks
for every one), so four of them are kept between calls (`lru_cache`): the company's key (`_company_key`), the title's
words (`_title_words`), what an address names (`_link_marks`) and a board's host (`_board_host`). A cache holds only
what its arguments make, so it is never stale; it is bounded, so it does not grow with every title the scout ever
read; and what it hands back is a string, a tuple or a frozen set, so no caller can change what the next one is given.
Making the index again from a large account's roles is far quicker with them than with none.
"""

import asyncio
import re
import uuid
import weakref
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from sqlalchemy import DateTime, Uuid, literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import ats
from app.jobsearch.employers.core import key_of
from app.jobsearch.employers.follow import title_tokens
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import ADDED_LINK, ORIGIN_OWNER, Employer, JobPosting, Opportunity
from app.jobsearch.postings.coop import Cooperate
from app.jobsearch.row_digest import over, row_text
from app.sites import avoid

#: The query parameters that carry a posting's id: the ones the job systems and boards use for it. Any other parameter
#: names a filter, a page or a visitor, and says nothing.
_ID_PARAMS = frozenset({"gh_jid", "jid", "jobid", "job_id", "job", "jobreqid", "reqid", "req_id",
                        "requisitionid", "requisition_id", "opportunityid", "currentjobid", "jk", "vjk"})
#: A job system's own id key ends in this (`gh_jid`): any such key carries a posting's id.
_JID_SUFFIX = "_jid"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
#: A path segment that is itself the id: a number (`/jobs/view/3912345678`) or a requisition as a job system writes it
#: ("R-102345", "JR352235"; the number is the id).
_NUMBER = re.compile(r"[0-9]{5,}")
_REQUISITION = re.compile(r"[A-Za-z]{1,3}-?([0-9]{5,})")
#: An id inside the last segment: a slug that ends in it (`director-3912345678`) or starts with it (`7910947-manager`),
#: six digits or more because a slug also ends in a zip code; or the requisition a job system's title ends in after an
#: underscore (`Director_R-102345`, `Director_JR12345`, and `_R-102345-1` for a repost), five digits or more.
_SLUG_END = re.compile(r"(?:^|[-.])([0-9]{6,})$|_(?:[A-Za-z]{1,3}-?)?([0-9]{5,})(?:-[0-9]{1,2})?$")
_SLUG_START = re.compile(r"^([0-9]{6,})(?=[-_.])")
#: A number that is a day written out (20261013) is not an id.
_A_DAY = re.compile(r"20[0-9]{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12][0-9]|3[01])")
#: "C++", "C#" and "F#" are not "C" and "F": a symbol stuck to a lone letter is spelt out before a title is folded.
_SYMBOL_WORD = re.compile(r"(?<![A-Za-z0-9])([A-Za-z])(\+\+|#)")


#: How many answers each cache keeps (`lru_cache`). A pass over the roles asks for each company, title and address in
#: turn, and a cache smaller than the pass misses on every ask (the one used longest ago is always the next one asked),
#: so each is a good deal larger than a pass over tens of thousands of roles: a title is kept once, for both of its
#: folds. The bound is what keeps a cache from growing with every title the scout reads.
_COMPANIES, _TITLES, _LINKS, _HOSTS = 2**13, 2**16, 2**16, 2**12


# --- the same company and title ---------------------------------------------------------------------------------


def _squash(text: str | None) -> str:
    return " ".join((text or "").lower().split())


def _unreadable(text: str | None) -> bool:
    """Whether a fold would lose letters from `text`. The folds read a-z and 0-9 only, so a letter beyond ASCII
    (Japanese, an accent) falls out, and two different words could fold to one."""
    return any(c.isalnum() and not c.isascii() for c in text or "")


@lru_cache(maxsize=_COMPANIES)
def _company_key(company: str | None) -> str:
    return _squash(company) if _unreadable(company) else (key_of(company) or _squash(company))


@lru_cache(maxsize=_TITLES)
def _title_words(title: str | None) -> tuple[str, str]:
    """A title's words folded the two ways `writings` keys it by: whole, then without a tag that only says where."""
    if _unreadable(title):
        words = _squash(title)
        return words, words
    spelt = _SYMBOL_WORD.sub(lambda m: f"{m[1]} {'plus plus' if m[2] == '++' else 'sharp'} ", title or "")
    return (" ".join(title_tokens(spelt, loose=False)) or _squash(title),
            " ".join(title_tokens(spelt, loose=True)) or _squash(title))


def writings(company: str | None, title: str | None) -> tuple[str, ...]:
    """The keys a company and title are known by once folded: one for the title whole (`s`) and one for the title
    without a tag that only says where (`l`), as `employer_posting.titles_match` has it. Two writings of one role share
    at least one. Empty when either folds to nothing."""
    firm = _company_key(company)
    whole, loosely = _title_words(title)
    keys = []
    for tag, words in (("s", whole), ("l", loosely)):
        if firm and words:
            keys.append(f"{tag}:{firm}|{words}")
    return tuple(keys)


# --- the same posting -------------------------------------------------------------------------------------------


def _path_ids(path: str) -> set[str]:
    """The ids a path carries. Only the places a job system puts a posting's own: a segment that is wholly an id or a
    requisition, and the end or start of the last segment. An id anywhere else in the path is a tenant's, a board's or
    a site's, which every posting of it shares (a job system's section, an organisation's number, a board of one), and
    says nothing."""
    segments = [s for s in unquote(path).split("/") if s]
    found = {m[1] for s in segments if (m := _REQUISITION.fullmatch(s))}
    if segments:
        last = segments[-1]
        if _UUID.fullmatch(last) or _NUMBER.fullmatch(last):
            found.add(last.lower())
        elif m := _SLUG_END.search(last):
            found.add(m[1] or m[2])
        elif m := _SLUG_START.match(last):
            found.add(m[1])
    return {token for token in found if not _A_DAY.fullmatch(token)}


def _ids(link: str) -> set[str]:
    """The ids an address carries: those in its path (`_path_ids`) and the value of a parameter that names the job."""
    text = link.strip()
    try:
        parts = urlsplit(text if "//" in text else f"//{text}")
        pairs = parse_qsl(parts.query)
    except ValueError:
        return set()
    found = _path_ids(parts.path)
    for name, value in pairs:
        value = value.strip().lower()
        key = name.lower()
        named = key in _ID_PARAMS or key.endswith(_JID_SUFFIX)
        if named and 4 <= len(value) <= 64 and any(c.isdigit() for c in value):
            found.add(value)  # an id has a digit in it: `job=engineering` is a category
    return found


@lru_cache(maxsize=_LINKS)
def _link_marks(link: str) -> frozenset[str]:
    """What one address names: each id it carries, with its host (`marks`)."""
    host = avoid.host_of(link)
    return frozenset(f"{host}#{token}" for token in _ids(link)) if host else frozenset()


def marks(links: Iterable[object]) -> frozenset[str]:
    """What links name: each id an address carries, with the address's host (`jobs.example.test#48213`), so two links
    name one posting when they share a mark, whatever else their addresses say (a title slug, a tracking parameter, a
    `www.`). A link with no id names no posting: it may be a careers page, and two roles can share one."""
    found: set[str] = set()
    for link in links:
        if isinstance(link, str):
            found |= _link_marks(link)
    return frozenset(found)


def board_host(host: str, boards: Iterable[str] = ()) -> str:
    """A job board's host as the board's name: a board is one site whichever country's address a posting is on
    (`uk.board.example` is `board.example`, as `analysis.site_of` folds it). `boards` are the owner's own job sites,
    besides the boards the app knows by name (`ats.BOARDS`). Any other host is its own name."""
    return _board_host(host, tuple(boards))


@lru_cache(maxsize=_HOSTS)
def _board_host(host: str, boards: tuple[str, ...]) -> str:
    """`board_host` for boards in the order they were given: the first that covers the host is its name, so the order
    is part of the question."""
    return next((b for b in (*ats.BOARDS, *boards) if avoid.covers(b, host)), host)


def board_marks(links: Iterable[object], boards: Iterable[str] = ()) -> frozenset[str]:
    """`marks` with a job board's host folded to its name (`board_host`), so one job's id is one mark whichever
    country's address of the board names it: `ca.board.example/jobs/view/3912345678` and
    `www.board.example/jobs/view/3912345678` are `board.example#3912345678`. The id of a job is the board's, not a
    country's; any other host keeps its own."""
    found: set[str] = set()
    for mark in marks(links):
        host, _, token = mark.partition("#")
        found.add(f"{board_host(host, boards)}#{token}")
    return frozenset(found)


def posting_links(posting: JobPosting) -> list[str | None]:
    """Every address a posting is known by: its own, its apply link and each link it appeared under."""
    return [posting.url, posting.apply_url, *(posting.links or [])]


# --- the roles the owner has ------------------------------------------------------------------------------------


class Known[T]:
    """Roles by every key above, answering which one a writing is. The first to add a key keeps it, so add the oldest
    first. `T` is whatever names a role to the caller (a row's id, a place in a list).

    The keys are kept in two layers, asked in turn for each key: the roles' own writings, then the other names their
    employers go by (`add_by_other_name`). Asking the first layer and then the second for each key is what one index
    with the second layer added after the first gave, and it lets an index of roles be extended with the roles made
    since (`known_roles` keeps one) and be the same as one made again. An index `known_roles` hands out is shared
    (`shared`): it is never added to, and raises when asked to."""

    def __init__(self) -> None:
        self._at: dict[str, T] = {}
        self._then: dict[str, T] = {}
        self._shared = False

    @staticmethod
    def _keys(company: str | None, title: str | None, links: Iterable[object], key: str | None) -> list[str]:
        """What a writing is known by, in the order it is asked: its exact keys, its folded ones, then its marks. A
        mark is the employer's as well: a tracker row can carry another employer's posting link, and a link is the same
        posting only for the same employer (a company with no name has no marks)."""
        exact = match_key(company, title)
        keys = [f"x:{exact}"] if exact != "|" else []
        if key:
            keys.append(f"x:{key}")
        keys.extend(writings(company, title))
        firm = _company_key(company)
        if firm:
            keys.extend(f"m:{firm}|{mark}" for mark in sorted(marks(links)))
        return keys

    @staticmethod
    def _unnamed_keys(links: Iterable[object]) -> list[str]:
        """What a job the owner added by its link, with no name yet, is known by besides: each job id its links carry,
        with its host, and no company (`board_marks`)."""
        return [f"u:{mark}" for mark in sorted(board_marks(links))]

    def add(self, ident: T, company: str | None, title: str | None, links: Iterable[object] = (), *,
            key: str | None = None, unnamed: bool = False) -> None:
        """Put a role in. `key` is the match key it is stored under, when that is not what its company and title make
        (a tracker's own key column can be stale). `unnamed` is a job the owner added by its link that research has not
        named (`models.is_unnamed`): its company is no employer's, so it is also known by each id its links carry,
        whatever company asks (`find`)."""
        self._put(self._at, ident, company, title, links, key, unnamed)

    def add_by_other_name(self, ident: T, company: str | None, title: str | None, links: Iterable[object] = ()) -> None:
        """Put a role in under another name its employer goes by: the keys of that name are asked after every role's
        own."""
        self._put(self._then, ident, company, title, links, None, False)

    def _put(self, layer: dict[str, T], ident: T, company: str | None, title: str | None, links: Iterable[object],
             key: str | None, unnamed: bool) -> None:
        if self._shared:
            raise RuntimeError("this index of the owner's roles is shared with every caller of `known_roles`: "
                               "add to a copy of it")
        links = list(links)
        for item in [*self._keys(company, title, links, key), *(self._unnamed_keys(links) if unnamed else [])]:
            layer.setdefault(item, ident)

    def copy(self) -> "Known[T]":
        """An index with the same keys, that may be added to."""
        other: Known[T] = Known()
        other._at, other._then = dict(self._at), dict(self._then)
        return other

    def shared(self) -> "Known[T]":
        """This index, from now on never added to (it is handed to more than one caller)."""
        self._shared = True
        return self

    def find(self, company: str | None, title: str | None, links: Iterable[object] = (), *,
             key: str | None = None) -> T | None:
        """The role this writing is, or None. `key` is a match key the writing is also known by (a posting's stored
        one). A writing's ids are asked of the jobs that are not named yet last, with no company: those are known by
        their ids alone."""
        links = list(links)
        for item in [*self._keys(company, title, links, key), *self._unnamed_keys(links)]:
            found = self._at.get(item)
            if found is None:
                found = self._then.get(item)
            if found is not None:
                return found
        return None


async def same_posting(session: AsyncSession, owner: str, company: str | None, title: str | None,
                       key: str) -> JobPosting | None:
    """The posting the owner's index already holds for this company and title: the earliest one under its match key
    `key`, else the earliest with the same company and title in another writing. None when there is none.

    No column holds the folded key, so the postings of a company whose name has this one's longest word in it are read
    (the id, the company and the title only) and folded here: a few for most companies, a board's whole list for the
    largest. A name with no word of three letters (nothing to narrow by) is not looked for in another writing."""
    posting = await session.scalar(select(JobPosting).where(
        JobPosting.owner == owner, JobPosting.match_key == key).order_by(JobPosting.first_seen).limit(1))
    wanted = set(writings(company, title))
    word = max(key_of(company).split(), key=len, default="")
    if posting is not None or not wanted or len(word) < 3:
        return posting
    rows = (await session.execute(select(JobPosting.id, JobPosting.company, JobPosting.title).where(
        JobPosting.owner == owner, JobPosting.company.icontains(word, autoescape=True)
    ).order_by(JobPosting.first_seen))).all()
    same = next((row.id for row in rows if wanted & set(writings(row.company, row.title))), None)
    return await session.get(JobPosting, same) if same is not None else None


#: The columns of a role that `known_roles` reads, and the one it orders by (`created_at`): what the index is made of,
#: and so what its key (`_Digest`) is made of.
_ROLE_COLUMNS = (Opportunity.id, Opportunity.created_at, Opportunity.employer_id, Opportunity.match_key,
                 Opportunity.company, Opportunity.title, Opportunity.url, Opportunity.apply_url,
                 Opportunity.board_url, Opportunity.origin)
#: Before every role: the mark of an index of no roles.
_START = (datetime.min.replace(tzinfo=UTC), uuid.UUID(int=0))


@dataclass(frozen=True)
class _Digest:
    """A fingerprint of a set of rows that combines (`row_digest`): how many there are, and two 64-bit hashes of each
    row's text, xor-ed together. The same rows give the same digest in any order; two sets of rows together give the
    digests of the two combined (`+`); and a row added, removed or changed changes it. It is made in one pass over each
    row's text (`_text_of`): a role that is not changed is not built into the index to find that out."""

    n: int = 0
    a: int = 0
    b: int = 0

    def __add__(self, other: "_Digest") -> "_Digest":
        return _Digest(self.n + other.n, self.a ^ other.a, self.b ^ other.b)

    @staticmethod
    def over(texts: Iterable[str]) -> "_Digest":
        """The digest of rows given as the text of each (`_text_of`)."""
        return _Digest(*over(texts))


@dataclass
class _Entry:
    """The index of one owner's roles and what it was made from: the newest role in it (the mark), the digest of the
    roles up to the mark and of the employers' names, and the names (`called`) the roles made after the mark are
    given."""

    known: Known[uuid.UUID]
    mark: tuple[datetime, uuid.UUID]
    roles: _Digest
    employers: _Digest
    called: dict[uuid.UUID, list[str]]


#: owner -> the index `known_roles` last made. One entry for each owner, checked against the database on every call (it
#: is never trusted for its age, a version number or a count: a test database that is emptied and filled again, a second
#: process, or a role changed by hand all show in the key).
_KNOWN: dict[str, _Entry] = {}
#: The call that is making an owner's index, so a second does not make it as well. The locks belong to the event loop
#: that made them (a test's loop ends with the test).
_BUILDING: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = weakref.WeakKeyDictionary()


def clear_cache() -> None:
    """Forget every index `known_roles` kept (the tests', never the app's: the key is checked on every call)."""
    _KNOWN.clear()


def _text_of(*columns: Any) -> Any:
    """A row's text, as the database writes it (`row_digest.row_text`)."""
    return row_text(*columns)


def _after(mark: tuple[datetime, uuid.UUID]) -> Any:
    """The roles made after `mark`, in the order roles are put in the index (`created_at`, then the id)."""
    return tuple_(Opportunity.created_at, Opportunity.id) > tuple_(literal(mark[0], DateTime(timezone=True)),
                                                                    literal(mark[1], Uuid))


async def _roles_seen(session: AsyncSession, owner: str, mark: tuple[datetime, uuid.UUID]) -> tuple[_Digest, int]:
    """The digest of the owner's roles up to `mark` and how many have been made after it: one pass over each role's
    text, no role built. The order is the index's (`created_at`, then the id), which the stored text of a time and of an
    id sorts in."""
    rows = (await session.execute(select(Opportunity.created_at, Opportunity.id, _text_of(*_ROLE_COLUMNS)).where(
        Opportunity.owner == owner))).all()
    older = [text for at, role_id, text in rows if (at, role_id) <= mark]
    return _Digest.over(older), len(rows) - len(older)


async def _employers_seen(session: AsyncSession, owner: str) -> _Digest:
    """The digest of the names (and the other names) the owner's employers go by."""
    return _Digest.over((await session.scalars(select(_text_of(Employer.id, Employer.name, Employer.aliases)).where(
        Employer.owner == owner))).all())


async def _read_roles(session: AsyncSession, owner: str, after: tuple[datetime, uuid.UUID] | None
                      ) -> tuple[list, _Digest]:
    """The owner's roles (those after `after`, if given), oldest first, and the digest of the ones read, in one
    statement: the rows and their key are from the same moment. It is one statement and not a page at a time because no
    index serves the order (`created_at`, then the id): each page was a scan of every role and a sort, so a large
    account's roles
    were many scans. The loop is given back where the time is spent, in `_put_in`."""
    query = select(*_ROLE_COLUMNS, _text_of(*_ROLE_COLUMNS).label("text")).where(Opportunity.owner == owner)
    if after is not None:
        query = query.where(_after(after))
    got = list((await session.execute(query.order_by(Opportunity.created_at, Opportunity.id))).all())
    return got, _Digest.over(row.text for row in got)


async def _read_employers(session: AsyncSession, owner: str) -> tuple[dict[uuid.UUID, list[str]], _Digest]:
    """Every name each of the owner's employers goes by (its own and the others), and the digest of the rows."""
    rows = (await session.execute(select(
        Employer.id, Employer.name, Employer.aliases,
        _text_of(Employer.id, Employer.name, Employer.aliases).label("text")).where(Employer.owner == owner))).all()
    called = {row.id: [str(name) for name in (row.name, *(row.aliases or [])) if name] for row in rows}
    return called, _Digest.over(row.text for row in rows)


async def _put_in(known: Known[uuid.UUID], rows: list, called: dict[uuid.UUID, list[str]]) -> None:
    """Every role of `rows` (oldest first) put in under its own company, and under each other name its employer goes
    by: the loop is given back between roles (an index of a large account's roles is seconds of it)."""
    coop = Cooperate()
    for row in rows:
        await coop()
        links = (row.url, row.apply_url, row.board_url)
        # `models.is_unnamed`, asked of the columns: a job the owner added by its link, which research has not named
        known.add(row.id, row.company, row.title, links, key=row.match_key,
                  unnamed=row.origin == ORIGIN_OWNER and row.company == ADDED_LINK)
        own = _squash(row.company)
        for name in called.get(row.employer_id, ()):
            if _squash(name) != own:
                known.add_by_other_name(row.id, name, row.title, links)


async def _make(session: AsyncSession, owner: str, entry: _Entry | None, grew: bool) -> _Entry:
    """The index of the owner's roles as they are now: the roles made since `entry` added to a copy of it when that is
    all that changed (`grew`), else every role read again."""
    if entry is not None and grew:
        rows, added = await _read_roles(session, owner, entry.mark)
        known, called, employers, roles = entry.known.copy(), entry.called, entry.employers, entry.roles + added
        mark = entry.mark
    else:
        rows, roles = await _read_roles(session, owner, None)
        called, employers = await _read_employers(session, owner)
        known, mark = Known(), _START
    if rows:
        mark = (rows[-1].created_at, rows[-1].id)
    await _put_in(known, rows, called)
    return _Entry(known.shared(), mark, roles, employers, called)


async def known_roles(session: AsyncSession, owner: str) -> Known[uuid.UUID]:
    """Every role the owner has (the tracker's, a search's, one a gate stopped), oldest first, by role id. The roles are
    read whole, not asked for by key: a role the exact key does not find is the reason to ask.

    A role is known by the company it was found under, and also by every name its employer goes by now: the record's
    name and each name it is also called. A merge renames the record it keeps ("Harbor Mutual" becomes "Harbor Mutual
    Insurance") and leaves each role's own company as it was found, while a board is read under the record's name, so a
    posting of a role the owner already has comes filed under a name the role's own company does not carry. A link still
    names the same posting only for the same employer (`Known._keys`): the names are the employer's, not another's.

    Every role is put in under its own company first and the employer's names after, so a name an employer goes by
    never takes a key from a role that was found under it: this adds matches and changes none.

    **Kept between calls, and checked on every one.** Making the index is seconds of the loop for a large set of
    roles, and the night's filter asks for it every time it runs. The last index made for the owner is kept with the
    digest of what it was made from (`_Digest`: the nine columns of each role read, the roles' creation time, and each
    employer's name and other names), and each call makes that digest in one pass over the text the database gives for
    each role and employer, which builds no index.
    The same digest returns the index kept; the roles made since the newest one in it (the mark), with the old roles'
    digest and the employers' unchanged, are added to a copy of it, which is what making it again gives; any other
    change (a role retitled, relinked or deleted, one made behind the mark by a transaction that began before it, an
    employer renamed or merged) makes it again from every role. Nothing but the rows is trusted, so a database emptied
    and filled again, a second process or an edit by hand cannot return another database's index. One call makes it at
    a time (the others wait, then look again). The index handed out is shared, so it is never added to: it raises when
    asked to."""
    entry = _KNOWN.get(owner)
    state = await _compare(session, owner, entry) if entry is not None else _CHANGED
    if state is _SAME and entry is not None:
        return entry.known
    lock = _BUILDING.setdefault(asyncio.get_running_loop(), {}).setdefault(owner, asyncio.Lock())
    waited = lock.locked()
    async with lock:
        if waited:  # another call may have made it while this one waited: look again before making it once more
            entry = _KNOWN.get(owner)
            state = await _compare(session, owner, entry) if entry is not None else _CHANGED
            if state is _SAME and entry is not None:
                return entry.known
        made = await _make(session, owner, entry, state is _GREW)
        _KNOWN[owner] = made
        return made.known


#: How the roles and employers are against the index kept: as it was made from them, with roles made after its mark, or
#: otherwise.
_SAME, _GREW, _CHANGED = "same", "grew", "changed"


async def _compare(session: AsyncSession, owner: str, entry: _Entry) -> str:
    """How the roles and the employers are against what `entry` was made from."""
    older, later = await _roles_seen(session, owner, entry.mark)
    if older != entry.roles or await _employers_seen(session, owner) != entry.employers:
        return _CHANGED
    return _GREW if later else _SAME
