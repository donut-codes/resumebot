"""The role's text: what text a role holds, and the one way one reaches it (docs/PRD.md 39.29, the owner, 2026-10-14).

A model that is handed a web page and asked for it comes back with a summary: what comes back is often a fraction of the
text the job system itself gave, and if it is stored it replaces the
text the app held. So the text of a posting is the app's to get, and an agent's reading of a page is the backup where
the app cannot read it, never a replacement.

**The interface is two verbs and a read-ahead.** `held` says what text the role holds and when it was read: its own
description when that is usable, else the fullest usable text a posting of the role holds, else the role's own snippet
(what the role page shows; a `Held` text can therefore be a snippet, and `whole.thin` says which), else nothing. `take`
is the one way a text, or the word that the posting is gone, reaches a role that exists; what it is given is a word for
where the text comes from: `Source` (the app reads the role's own source: the step "Fetch the posting's text" of
`analyse_role.bpmn`), `FromPostings` (only what a posting of the role already holds), `Capture` (what the owner's
browser, or the app's own browser job, read from the role's source page) or `BoardCopy` (the board's copy of the
posting, for a role whose employer's own posting could not be read: below). `ahead` makes the reads a `Source` take
would make, with no transaction open. `take` (`_keep`) is the one writer of `Opportunity.description` once a role
exists, bar the bridge `take_whole` below: the stores are in step when it returns (the description, the text of the
postings that take it, the raw copy that text needs), and what follows a changed text (the posted day, research queued
again, the gate facts) is done there, so no caller says it and none can skip it.

Three other kinds of writer exist, and none is `take`'s: a role is made holding a posting's own whole text
(`whole.of_posting`: `pipeline/from_posting.py`, the scout's report in `actions.py`), a text the posting already holds
with its raw copy, before anything is researched or gated from it; the readers write a posting's own text and keep its
raw copy in the same call (`postings.index` `store` and `keep_detail`, the browser reader's `_fill` and `_store`, and
its sighting of a known posting), `browser/add_link.name_posting` writes one with none, which the stage turn then
enters (`lifecycle.cleanse_missing`, `raw_copies.keep_legacy`), and the owner's read of a board posting's own page
(the board's capture: a job board's copy, which the role's own text takes the place of when it comes, `_board_copy`);
and the repair that replaces a held description by design (`repairs.py`). The stores stay in step there by what each
site does, each pinned by its own tests (`test_raw_and_cleansed`, `test_lifecycle`, `test_posting_text`), not by
`take`.

One verb takes a text away: `let_go` (a text the analyst found is not the role's posting: a careers page, a page's
shell). It blanks the description and the postings that hold that very text, so the source is read again; it keeps the
raw copies, which are what was read.

Three bridges still push a text from one store to the other after the fact: `take_whole` (a deck step),
`keep_whole_text` (in `raw_copies`) and the `thin_roles` block of `lifecycle.settle`. They stood in for writers that
left the stores apart; the last one found (the browser reader's sighting of a known posting) kept no raw copy until it
was closed, and every writer above is now pinned by a test that the copy follows its text. They stay because only a
counter at zero shows nothing needs them: they run every turn and keep the count that would (a role whose description
is a snippet while a posting passes `whole.of_posting`; a role in `lifecycle.PLACED` whose current copy is under
`cleansed.MIN_TEXT` while a posting holds that much) at zero.

- **Usable means whole, verbatim, from a source** (`whole.py`, the owner's rule). A text is kept only when nothing says
  it is not the whole posting (`whole.why_not`: a fragment, a text cut off at "see more", a sign-in wall, a page read
  whole), it is as the posting writes it, and it comes from a job system's own answer, the app's own read of the role's
  source or the owner's browser's capture of it. A listing's snippet is never the text a role holds, and neither is a
  scout's or a reader's report or the analyst's summary: a role the app cannot get the text for has none, and its asks
  come out unknown, which is the truth (migration 0074 blanked the summaries stored before).
- **A page is kept only when it is the role's posting**. The employer's own career page the
  app reads is kept as the text only when it names the role by its title (`this_posting`: the title's words together
  in the text, or the title of the JobPosting the page embeds or one of its headings is the role's title); a careers
  page around a posting that is drawn in the browser is no text, and the role waits for a capture. Every read of a
  page that keeps its text asks it (`read_page` with the title: this module's, `index.read_details` for a posting on
  an employer's own site, `repairs.reextract_posting_header`). A job system's own answer and a capture are not asked.
- **Nothing is cut.** A capture past what one reply to an agent can hold (`whole.CEILING_CHARS`) is refused, with its
  size; the text the app reads itself is stored as it is.
- **A usable description is never overwritten** (the owner's own edit, a posting's own words), except by the board's
  own posting for an Easy Apply (`Capture.replaces`), and except the board's copy (below), which the employer's text
  replaces.
- **the board's copy stands in only where the employer's text is out of reach** (the owner, 2026-11-05, docs/PRD.md
  48.14 f). A role whose employer's posting could not be read (its site refused, the posting is gone, the lookup found
  none: the board's copy hook) and that holds a whole page of its board posting is given that page's text as
  its description, labelled (`Opportunity.description_from` `board`, `BoardCopy`), so it can be analysed and read.
  It is not a source's text and it is not usable the way the employer's is: whatever brings the employer's text (the
  app's read, a browser capture, an employer posting in the index) replaces it and clears the label (`_keep`), and the
  text `held` gives is the employer's whenever a posting of the role holds one. A role not Sourced is Sourced to
  the board's posting for it (`sourced.BOARD_COPY`: nothing else makes a role the pipeline will read), and
  Sourced to the employer again when the employer's text comes (`_resource`).
- **Only from the source** (the owner, 2026-10-15). A role's text is the text of its source (`Opportunity.source_url`,
  `postings/sourced.py`): the employer's own posting, or the posting where a recruiter's Apply lands, or, for an Easy
  Apply with no link to follow, the board's own posting (a `Capture`, given by `sourced.accept_source`). A `Source` take
  reads that link alone and never a job board; nothing here gets round a refusal, which is a stop.
- **A posting the source says is gone** (the owner, 2026-10-15). A source that clearly says the posting no longer exists
  (an HTTP 404 or 410 on its page or its job system's answer: `polite.gone`) ends the role (`quiet.end_gone`). A block,
  a CAPTCHA, a sign-in wall, a rate limit, a timeout or a 5xx is never that: the role waits, as before.

The network is one seam, `polite.make`: the module calls it, tests replace that one attribute (a `polite.Fetcher` on the
network, one on an `httpx.MockTransport`, `reparse.Replay` for a reader's `detail`). Services flush; the process step or
the route commits."""

import asyncio
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import NamedTuple

from sqlalchemy import and_, func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.jobsearch import adapters, ats, off_turn
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.models import (
    DESCRIPTION_BOARD,
    SOURCE_KIND_BOARD_COPY,
    Employer,
    JobPosting,
    Opportunity,
    is_board_copy,
    is_unnamed,
)
from app.jobsearch.pipeline import quiet
from app.jobsearch.postings import cleansed, hold, raw_copies, role_postings, whole
from app.jobsearch.postings import dates as posted_dates
from app.jobsearch.postings import index as postings

log = logging.getLogger(__name__)

#: The most seconds one `Source` take reads for one role. The reads of a role are a few requests, each paced by the
#: fetcher; a site that is slow costs the role its fetch, never the research: the analyst is the backup.
FETCH_SECONDS = 40.0
#: What `take` says: the role holds a usable text (it already did, or a posting's was put on it), a text was put on it
#: now (the app read it, or a capture was kept: the words of `fetched.how` in `analyse_role.bpmn`), or the source said
#: the posting is gone and the role was ended.
HELD, FETCHED, GONE = "held", "fetched", "gone"


class Held(NamedTuple):
    """The text a role holds, verbatim ("" when it holds none), and when it was read."""

    text: str
    read_at: datetime | None


@dataclass(frozen=True)
class Source:
    """The default: what the role's own source says. The text a posting at the source holds, else the text the app reads
    from that address alone (the job system's reader for one posting, else the page itself, its embedded job posting
    first). A role that is not Sourced, or whose source is a board's own posting, is given none by this word."""


@dataclass(frozen=True)
class FromPostings:
    """Only what a posting of the role already holds: it reads nothing."""


@dataclass(frozen=True)
class Capture:
    """The text the owner's browser, or the app's own browser job, read from the role's source page."""

    text: str
    #: The page as it came, kept as the raw copy of each posting of the role (reader `code:page`, `text/html`).
    page: bytes | None = None
    #: Only for the board's own posting of an Easy Apply (`sourced.accept_source`): it replaces a description.
    replaces: bool = False


@dataclass(frozen=True)
class BoardCopy:
    """The board's copy of the role's posting, as the app's Chrome read it from the posting's own page (the read-out of
    the board's capture), for a role whose employer's own posting is out of reach (the board's copy hook decides that
    and gives this word). The description it makes is labelled (`Opportunity.description_from`), and the employer's text
    replaces it."""

    text: str
    #: When the page was read: the posted day its words give is counted from it.
    read_at: datetime | None = None


def _text(posting: JobPosting) -> str:
    return (posting.text or "").strip()


def _snippet_only(posting: JobPosting) -> bool:
    """Whether the text a posting holds is only what a listing showed: a posting read in the owner's browser that was
    never opened (its `detail_at` is None). A snippet is not the posting's text (the owner, 2026-10-14: a partial
    result is bad data), so it never makes a role "hold" its text, and a text that is the posting's takes its place."""
    return posting.ats == ats.BROWSER and posting.detail_at is None


async def _board_copy(session: AsyncSession, posting: JobPosting) -> bool:
    """Whether the text a job board's posting holds is that board's page as the app's Chrome read it
    (the board's capture, `raw_copies.holds_board_page`): a copy a role's own text takes the place of, as it takes the
    place of a snippet, so the gates never read the board's words once the employer's have come."""
    return ((posting.ats == ats.JOB_SITE or posting.ats in ats.OF_ROLES) and bool(_text(posting))
            and await raw_copies.holds_board_page(session, posting))


def _fullest(rows: list[JobPosting]) -> JobPosting | None:
    """The posting of these that holds the most text, if any holds a usable one (`whole.of_posting`)."""
    return max((p for p in rows if whole.of_posting(p)), key=lambda p: len(_text(p)), default=None)


async def _loaded(session: AsyncSession, role: Opportunity) -> None:
    """The role's text and its date, loaded: a role listed without its text, in this session, has them refreshed."""
    if {"description", "description_at"} & inspect(role).unloaded:
        await session.refresh(role, ["description", "description_at"])


def needs_postings(role: Opportunity) -> bool:
    """Whether what the role holds depends on its postings: its own description is empty, only a snippet, or the board's
    copy (which the employer's text in a posting takes the place of). A role whose own description is usable holds that
    whatever its postings say (`held_from`)."""
    return whole.thin((role.description or "").strip()) or is_board_copy(role)


def held_from(role: Opportunity, postings: Iterable[JobPosting]) -> Held:
    """`held`, from the role and its postings already read: the one rule, so a caller that reads the postings of many
    roles at once (`gate_facts.stale`) judges each on the text `held` gives it. `postings` need name only the postings
    that could be the fullest usable one (a text under `cleansed.MIN_TEXT` characters never is) and need be given only
    when `needs_postings`."""
    own = (role.description or "").strip()
    if own and not whole.thin(own) and not is_board_copy(role):
        return Held(own, role.description_at)
    best = _fullest(list(postings))  # the employer's text, when a posting holds it, beats a snippet and the board's
    # copy
    if best is not None:
        return Held(_text(best), best.detail_at)
    return Held(own, role.description_at) if own else Held("", None)


async def held(session: AsyncSession, role: Opportunity) -> Held:
    """The text the app holds of the role's posting, whole, and when it was read: the role's own description when it is
    usable, else the fullest usable text a posting of the role holds, else the role's own snippet if it has one (what
    the role page shows), else nothing. Reads only."""
    return held_from(role, await role_postings.of_role(session, role) if needs_postings(role) else [])


# --- keeping it -------------------------------------------------------------------------------------------------------


async def _after(session: AsyncSession, role: Opportunity) -> None:
    """What follows a changed text: a role researched before it is queued for research again
    (`analysis.requeue_if_stale`), and a role not yet researched has its gate facts read from it
    (`gate_facts.after_text`: the cheap gates on what the text states, at once)."""
    from app.jobsearch.gating import gate_facts  # both read this module; imported here, never at the top
    from app.jobsearch.research import analysis  # both read this module; imported here, never at the top

    await session.flush()
    await analysis.requeue_if_stale(session, role)
    await gate_facts.after_text(session, role)


async def _keep(session: AsyncSession, role: Opportunity, text: str, *, read_at: datetime,
                counted_from: datetime | None = None, posted=None, fill: bool, page: bytes | None = None,
                reader: str = "code:text", copy_of: str | None = None) -> None:
    """The only writer of a role's text: `Opportunity.description` and its date `read_at`, and whose copy it is
    (`description_from`: `copy_of`, which only the board's copy gives; every other text clears it, so the employer's
    text takes the label off the copy it replaces); with `fill`, the text of each
    posting of the role that holds none, only a listing's snippet (`_snippet_only`) or only a job board's page as the
    app's Chrome read it (`_board_copy`), which takes it; the raw copy that
    text needs, so the cleansed copy holds it in the same call (`hold.hold`), for the postings that took
    the text, or, with a `page`, the page as it came for every posting of the role; the posted day
    (`posted_dates.apply`: the job system's own date `posted` first, else the text's words, counted from
    `counted_from`, `read_at` by default); then what follows a changed text (`_after`). `fill` is False for a posting's
    own text put on the role (there is nothing to copy) and for a job system's answer (already on its posting, with its
    raw copy). `reader` names where a text with no page came from: `code:text` for the app's own read, `browser:page`
    for a capture."""
    rows = await role_postings.of_role(session, role)
    role.description, role.description_at, role.description_from = text, read_at, copy_of
    took = []
    if fill:
        for posting in rows:
            if not _text(posting) or _snippet_only(posting) or await _board_copy(session, posting):
                took.append(posting)
    for posting in rows if page is not None else took:
        put = hold.Put(text, read_at) if posting in took else None  # the others keep the text they have
        if page is not None:  # the copy holds the role's text for a posting that did not take it, on purpose
            await hold.hold(session, posting, hold.page(page, "code:page"), put=put,
                            copy_text=hold.POSTING if put else text, now=read_at)
        else:
            await hold.hold(session, posting, hold.plain(reader), put=put, now=read_at)
    posted_dates.apply(role, rows, text=text, system=posted, read_at=counted_from or read_at)
    await _after(session, role)


async def let_go(session: AsyncSession, role: Opportunity, text: str) -> None:
    """Let go of a text found not to be the role's posting (`analysis.send_back`): the role's description and its date,
    and the text and date of each posting of the role that holds exactly that text (white space apart: the idiom of
    `repairs.py`), so the role holds no text and its source is read again for one (`reader.to_read_at_source`, which a
    posting still holding the page would hide the role from). A posting holding another text is left as it is: it may be
    the posting's own, and `held` then gives it. The raw copies stay: they are what was read. Services flush."""
    kept = (text or "").strip()
    if (role.description or "").strip() == kept:
        role.description, role.description_at, role.description_from = None, None, None
    for posting in await role_postings.of_role(session, role):
        if kept and _text(posting) == kept:
            posting.text, posting.detail_at = None, None
    await session.flush()


async def _resource(session: AsyncSession, role: Opportunity, posting: JobPosting) -> bool:
    """Source a role that is Sourced to the board's posting (`sourced.BOARD_COPY`: its employer's own was out of reach)
    to the employer's own posting that has come, the address it holds (`sourced.stored_source`), so the employer's text
    replaces the board's copy with the source it belongs to. False, changing nothing, when the posting gives no address
    that is one posting of the employer's own. Services flush."""
    from app.jobsearch.employers import follow as employer_posting  # sourced reads this module; never at the top
    from app.jobsearch.postings import sourced

    hosts = await employer_posting.job_board_hosts(session, role.owner)
    employer = await session.get(Employer, role.employer_id) if role.employer_id else None
    held = sourced.stored_source(role, [posting], employer, hosts)
    if held is None:
        return False
    await sourced.keep_source(session, role, held[0], sourced.EMPLOYER, "code", hosts, held[1])
    return True


async def _adopt(session: AsyncSession, role: Opportunity, posting: JobPosting, now: datetime, *, fresh: bool) -> None:
    """Put the usable text of a posting of the role on the role: it is the posting's text word for word, from the job
    system, and it belongs on the role, which shows and keeps it after the posting comes down. `fresh`: a snippet it
    takes the place of is dated now, so research made from the snippet is out of date (`analysis.is_stale`) and `_keep`
    queues it again at once (`_after`); else it is dated as the posting was read. A role Sourced to the board's posting
    is Sourced to this one first (`_resource`), and is left as it is when it cannot be: the text and its source go
    together."""
    if role.source_kind == SOURCE_KIND_BOARD_COPY and not await _resource(session, role, posting):
        return
    read_at = posting.detail_at or now
    await _keep(session, role, whole.of_posting(posting), read_at=now if fresh else read_at, counted_from=read_at,
                posted=posting.posted, fill=False)


async def _capture(session: AsyncSession, role: Opportunity, word: Capture, now: datetime) -> str | None:
    """The `Capture` take: the text is refused, changing nothing, when the role has no source (only a source's text is a
    description) or when it is not the whole of a posting or no reply could hold it (a partial capture is bad data); a
    usable description is never overwritten, unless the capture is the board's own posting (`Capture.replaces`)."""
    kept = (word.text or "").strip()
    if not role.source_url or not role.source_kind:
        return None
    if whole.refusal(kept, "the posting's text") is not None:
        return None
    own = (role.description or "").strip()
    if own and not whole.thin(own) and not word.replaces and not is_board_copy(role):  # the board's copy gives way
        if word.page is not None:  # the page is kept beside the text the role already holds
            for posting in await role_postings.of_role(session, role):
                # the copy holds the text the role already has, on purpose: the posting's own is left as it is
                await hold.hold(session, posting, hold.page(word.page, "code:page"), copy_text=own, now=now)
        return HELD
    await _keep(session, role, kept, read_at=now, fill=True, page=word.page, reader="browser:page")
    return FETCHED


# --- reading it -------------------------------------------------------------------------------------------------------


def _source_of(posting: JobPosting, source: str) -> bool:
    """Whether a posting of the role is the role's source: the same address."""
    mine = source.strip().rstrip("/").lower()
    return mine in {(x or "").strip().rstrip("/").lower() for x in (posting.url, posting.apply_url)}


def _readable(posting: JobPosting) -> bool:
    """Whether a job system the app has a reader for can give the posting's text."""
    return posting.ats in adapters.ADAPTERS


@dataclass
class _Read:
    """What one role's reads brought back: the postings read and what each answered, the text of a link or a page, what
    the system or the page said besides (work mode, place, posted day), and what the source said when it said the
    posting is gone (`polite.gone`), the first time it did, and the title of another job the page stated, if it did
    (`postings.Reading.other`: the page is not this role's posting)."""

    details: list[tuple[JobPosting, dict | None]] = field(default_factory=list)
    text: str | None = None
    facts: dict = field(default_factory=dict)
    gone: str | None = None
    other: str | None = None


async def _read(fetch: Fetcher, found: _Read, readable: list[tuple[JobPosting, Employer | None]],
                links: list[str], page: str | None, title: str | None = None) -> None:
    """The reads, in order, until one gives the text: the role's postings on their job systems, the links of the role
    that are on one, then the employer's own career page, which gives its text only when it names the role (`title`:
    `this_posting`; a job system's own answer is not asked). Network only: what it learns is put in `found`, and the
    caller keeps it."""
    for posting, employer in readable:
        gone: list[str] = []
        more = await postings.read_detail(fetch, employer, posting, gone=gone)
        if gone:
            found.gone = found.gone or gone[0]
        found.details.append((posting, more))
        if more and more.get("text"):
            return
    for link in links:
        got = await postings.read_link(fetch, link)
        found.gone = found.gone or got.gone
        found.text, found.facts = got.text, got.facts
        if found.text:
            return
    if page:
        got = await postings.read_page(fetch, page, title)
        found.gone = found.gone or got.gone
        found.facts = {**{k: got.facts[k] for k in postings.READ_FACTS if got.facts.get(k)}, **found.facts}
        found.text, found.other = got.text, got.other


@dataclass
class _Plan:
    """What a `Source` take does for one role, worked out from what the database holds with nothing written: `how` is
    `"whole"` (a snippet the role holds gives way to the whole text of `posting`), `"held"` (the role holds its text),
    `"none"` (nothing to fetch), `"best"` (`posting`, a posting of the source, holds the source's own text) or `"read"`
    (the source must be read: the role's postings `rows`, its `source` and the reads `_read` makes: `readable`, `links`,
    `page`)."""

    how: str
    posting: JobPosting | None = None
    rows: list[JobPosting] = field(default_factory=list)
    source: str = ""
    readable: list[tuple[JobPosting, Employer | None]] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    page: str | None = None

    def key(self, role: Opportunity) -> tuple:
        """The reads, as `polite.read_ahead` keeps them: a step that would make the same reads takes what was read."""
        return ("posting_text", role.id, self.source, tuple(p.id for p, _ in self.readable), tuple(self.links),
                self.page)


async def _plan(session: AsyncSession, role: Opportunity) -> _Plan:
    """The database part of a `Source` take, before any read, writing nothing."""
    await _loaded(session, role)
    rows = await role_postings.of_role(session, role)
    own = (role.description or "").strip()
    copy = is_board_copy(role)
    longer = _fullest(rows) if own and (whole.thin(own) or copy) else None
    if longer is not None:  # a snippet kept before the posting was read whole, or the board's copy, gives way to the
        return _Plan("whole", posting=longer)  # posting's text
    if own and not copy and not (whole.thin(own) and any(_text(p) == own for p in rows)):
        return _Plan("held")  # a snippet a posting's list gave, copied onto the role, is no text (below): the source
        # is read
    source = (role.source_url or "").strip()
    if role.sourced_at is None or not source or role.source_kind in ("board", SOURCE_KIND_BOARD_COPY):
        return _Plan("none")  # only a source's own text is a description: a role that is not Sourced has none to fetch,
        # and a board's own posting is given whole by `sourced.accept_source`, never fetched
    best = _fullest([p for p in rows if _source_of(p, source)])  # the source's own text, as its job system gave it
    if best is not None:
        return _Plan("best", posting=best)
    # A link on a job system whose reader reads one posting by its address is read by that reader first (`links`), and
    # as the page it is when the reader gives no text: politely, its embedded job posting first (a refusal is a stop). A
    # system that draws its postings in a browser is never read as a page: the page
    # says nothing. The source on any other system or on an employer's own site is read as the page it is.
    at = ats.parse(source)
    links = [source] if at and at.read else []
    page = None if at and at.drawn else source
    employers = {e.id: e for e in (await session.scalars(select(Employer).where(
        Employer.id.in_({p.employer_id for p in rows} or {None})))).all()}
    readable = [(p, employers.get(p.employer_id)) for p in rows if _readable(p) and _source_of(p, source)]
    if not (readable or links or page):
        return _Plan("none")
    return _Plan("read", rows=list(rows), source=source, readable=readable, links=links, page=page)


async def holds_without_read(session: AsyncSession, role: Opportunity) -> bool:
    """Whether a `Source` take of the role gives it a text with no read: it holds one, a snippet it holds gives way to a
    posting's whole text, or a posting at its source holds the source's own (`_plan`: `held`, `whole`, `best`), or it
    holds the board's copy, which `take` says `HELD` for when the employer's text cannot be had. The one question the
    take asks before it reads, asked with nothing read and nothing written, so that what puts a waiting role back in
    line (`analysis.resume_waiting`) and what the take answers cannot differ: a role whose text only a posting at some
    other address than its source holds is read at its source by the take, which can fail, and so is not put back in
    line by that text (`held` alone says "text" for it).

    A role that is not Sourced is asked as it will be once the step before the fetch (`sourced.source_stored`) has kept
    the source the app already holds (`sourced.predicted_source`, no network): the take then finds the posting at that
    address, and the start makes progress. A role that step has no source for stays "no": the take gives it nothing, the
    process leaves it where it was (an unbuilt role before Ready, which `built_out` marks waiting again), and a start
    every sweep would be the loop this question exists to end."""
    if is_board_copy(role):
        return True
    plan = await _plan(session, role)
    if plan.how == "none" and role.sourced_at is None:
        from app.jobsearch.postings import sourced  # sourced reads this module; imported here, never at the top

        source = await sourced.predicted_source(session, role, {})
        if source is not None:
            plan = await _plan_as_sourced(session, role, source)
    return plan.how in ("held", "whole", "best")


async def _plan_as_sourced(session: AsyncSession, role: Opportunity, source: str) -> _Plan:
    """`_plan` for a role as it will be once `source` is kept as its employer source (`sourced._keep_source`, which the
    step before the read does), with nothing changed: the role is put back as it was."""
    was = (role.source_url, role.source_kind, role.sourced_at, role.description, role.description_at)
    with session.no_autoflush:
        try:
            if role.source_url and role.source_url != source:
                role.description, role.description_at = None, None
            role.source_url, role.source_kind, role.sourced_at = source, "employer", datetime.now(UTC)
            return await _plan(session, role)
        finally:
            role.source_url, role.source_kind, role.sourced_at, role.description, role.description_at = was


async def _fetch(role: Opportunity, plan: _Plan) -> _Read:
    """The network part of a `Source` take: the reads of `plan`, in at most `FETCH_SECONDS`. Never raises. A page is
    kept as the role's text only when it names the role by its title; a role added by its link has the link for a
    title, which names nothing, so its page is not asked."""
    found = _Read()
    fetch = polite.make()
    title = None if is_unnamed(role) else role.title
    try:
        async with asyncio.timeout(FETCH_SECONDS):
            await _read(fetch, found, plan.readable, plan.links, plan.page, title)
    except TimeoutError:
        log.info("the text of role %s was not fetched in %g seconds", role.id, FETCH_SECONDS)
    except Exception:  # whatever the read does, the research of the role goes on without its text
        log.warning("fetching the text of role %s failed unexpectedly", role.id, exc_info=True)
    finally:
        await fetch.close()
    return found


async def ahead(session: AsyncSession, role: Opportunity, source: str | None = None) -> dict:
    """The reads a `Source` take of `role` would make, made now with no transaction open, for `polite.read_ahead`: the
    database part is done in `session`, which is then released (`db.release`: what it did so far is committed), and the
    source is read. `source` is the link a role not yet Sourced is about to be given, by the step before the read.
    Empty, with the session left as it is, when nothing needs a read, or when the session cannot be released here
    (inside a process step: the step's own caller reads ahead). The caller starts the process inside
    `polite.read_ahead(...)` with what this gave; forget it and the step reads inside its transaction (`db.strict_io`
    says so in tests)."""
    if not db.can_release(session):
        return {}
    plan = await (_plan_as_sourced(session, role, source) if source is not None else _plan(session, role))
    if plan.how != "read":
        return {}
    key = plan.key(role)
    await db.release(session)
    return {key: await _fetch(role, plan)}


def another_job(reads: dict) -> str | None:
    """The title of another job that the page read ahead (`ahead`) stated, or None: the source's page is not the role's
    posting, whatever else it said."""
    return next((r.other for r in reads.values() if r.other), None)


async def _place(session: AsyncSession, role: Opportunity, facts: dict) -> None:
    """The work mode and place a read stated, put on the role where it has none (`stated_place.keep_facts`), and the
    role judged against the owner's place rule at once (`stated_place.decide`). Every read does it, not only one that
    changed the role's own place: the same read also gave the role's postings a place or a way of working, which the
    role's cleansed copy states and the gate reads."""
    from app.jobsearch.gating import stated_place  # stated_place reads this module; imported here, never at the top

    stated_place.keep_facts(role, facts)
    await stated_place.decide(session, role.owner, role, cause="The role's place changed")


async def _from_source(session: AsyncSession, role: Opportunity, now: datetime) -> str | None:
    """The `Source` take."""
    plan = await _plan(session, role)
    if plan.how in ("whole", "best") and plan.posting is not None:
        # A snippet gives way to a posting's whole text, or a posting of the source holds the source's own text.
        await _adopt(session, role, plan.posting, now, fresh=plan.how == "whole")
        return HELD
    if plan.how == "held":
        return HELD
    if plan.how != "read":
        return None
    source = plan.source
    ready = polite.ahead(plan.key(role))
    if isinstance(ready, _Read):
        found = ready
    else:
        await db.release_if_free(session)  # what was done so far is saved; the source is read with none open
        found = await _fetch(role, plan)
    gave = []  # the postings the system gave text for just now
    facts = dict(found.facts)
    for posting, more in found.details:
        posting.detail_at = now
        if more is not None and await postings.keep_detail(session, posting, more, now):
            gave.append(posting)
        for key in postings.READ_FACTS:  # what its job system said of the way, place and date
            if more and more.get(key) and key not in facts:
                facts[key] = more[key]
    await _place(session, role, facts)
    for posting in plan.rows:  # the posting that is the role's source holds them too, when it has none
        if _source_of(posting, source):
            posting.work_mode = posting.work_mode or facts.get("work_mode")
            posting.location = posting.location or facts.get("location")
    best = _fullest(gave)
    text = _text(best) if best is not None else (found.text or "").strip()
    if text and not whole.thin(text):  # a read that gave only a title and a place is no text, never a description
        await _keep(session, role, text, read_at=now, posted=facts.get("posted"), fill=best is None)
        return FETCHED
    if found.gone and await quiet.end_gone(session, role, source, found.gone):
        return GONE
    await session.flush()
    return None


async def _from_postings(session: AsyncSession, role: Opportunity, now: datetime) -> str | None:
    """The `FromPostings` take: a role with no usable description is given the fullest usable text a posting of its own
    holds. A snippet it holds gives way to it (dated now); none takes it as the posting was read."""
    await _loaded(session, role)
    own = (role.description or "").strip()
    if own and not whole.thin(own) and not is_board_copy(role):
        return HELD
    best = _fullest(await role_postings.of_role(session, role))
    if best is None:
        return HELD if is_board_copy(role) else None  # the board's copy stays where no posting holds the employer's
    await _adopt(session, role, best, now, fresh=bool(own))
    return HELD


async def _take_board_copy(session: AsyncSession, role: Opportunity, word: BoardCopy, now: datetime) -> str | None:
    """The `BoardCopy` take: the board's copy is the role's description, labelled (`description_from`), unless the
    role holds a usable text already (the employer's, the owner's own edit, or a copy: `HELD`, nothing changes), or the
    copy is not the whole of a posting (`whole.refusal`: None, nothing changes). A snippet gives way to it. No raw copy
    is made: the page's read-out is the raw copy already. Whether the employer's text is out of reach is the caller's to
    say (the board's copy hook)."""
    kept = (word.text or "").strip()
    if whole.refusal(kept, "the board's copy of the posting") is not None:
        return None
    if not whole.thin(role.description):
        return HELD
    await _keep(session, role, kept, read_at=now, counted_from=word.read_at, fill=False,
                copy_of=DESCRIPTION_BOARD)
    return FETCHED


async def take(session: AsyncSession, role: Opportunity,
               word: Source | FromPostings | Capture | BoardCopy | None = None, *,
               now: datetime | None = None) -> str | None:
    """The one way a text, or the word that the posting is gone, reaches a role. Returns `HELD` (the role holds a
    usable text: it already did, or a posting's was put on it), `FETCHED` (a text was put on it now: the app read it,
    or the capture was kept), `GONE` (the source said 404 or 410 and the role was ended: only a `Source` take says so)
    or None (it holds none and none could be had; a `Capture` that was refused: nothing changed).

    - `Source` (the default, `word=None`): a usable description is kept; a snippet gives way to a whole posting text of
      the role (dated now, so research made from the snippet is queued again at once). A Sourced role with none is
      given the text a posting at its source holds, else the text the app reads from that address alone (the job
      system's reader for one posting, else the page itself, its embedded job posting first), with the work mode and
      place it states put on the role. A role that is not Sourced, or whose source is a board's own posting, is given
      none: that comes by a `Capture`. Never from any other link. A read that fails, is refused or takes too long
      leaves the role as it was and never stops the research; it is read with no transaction open
      (`db.release_if_free`), and inside a process step or a savepoint, which cannot end its transaction, the reads
      made ahead of it are taken (`ahead`, `polite.read_ahead`).
    - `FromPostings`: the same step for a role a posting of its own can give text, with nothing read.
    - `BoardCopy`: the board's copy of the posting becomes the description, labelled, for a role that holds no usable
      text; the employer's text replaces it later, by any of the takes here (a role that holds only a copy is read at
      its source, from its postings and by a capture as a role with a snippet is, and `take` says `HELD` while it
      holds the copy and the employer's cannot be had).
    - `Capture`: refused when the role has no source or the text is not whole or is too long to hold
      (`whole.refusal`); a usable description is kept (with the page, when given, kept as the raw copy of each
      posting). Else the text is the role's: a snippet description gives way to it.

    Nothing raises for a read that is refused (robots.txt, a 403), fails, times out or gives a fragment or a wall; a
    block, a wall or a rate limit is never `GONE`. A database error propagates. The caller commits."""
    now = now or datetime.now(UTC)
    if isinstance(word, Capture):
        return await _capture(session, role, word, now)
    if isinstance(word, FromPostings):
        return await _from_postings(session, role, now)
    if isinstance(word, BoardCopy):
        return await _take_board_copy(session, role, word, now)
    got = await _from_source(session, role, now)
    # the employer's text could not be had: the board's copy stands
    return HELD if got is None and is_board_copy(role) else got


# --- the bridges between the two stores (to go: the writer keeps both in step) ---------------------------------------


#: How many roles one transaction of `take_whole` gives a text: each is a read of its postings, a write, a flush and a
#: raw copy, so a hundred are about a quarter of a second. The shape's constant, not a setting; the tests patch it.
WHOLE_BATCH = 100


async def take_whole(session: AsyncSession, owner: str, now: datetime | None = None) -> list[uuid.UUID]:
    """Give every role, at any stage, whose description is only a snippet (`thin`: a listing's few words kept before
    the posting was read) the whole text a posting of its own now holds (`_fullest`), and enter that text as the
    posting's newest cleansed copy (`raw_copies.keep_whole_text`). A role made from a listing's snippet kept
    it as its description after its posting was read whole, and every reader of the role took the snippet for the
    posting's text. Its text is dated now, so research made from the snippet is out of date (`analysis.is_stale`); it is
    not queued again here (the `requeue` action's scope `stale` does that). Returns the roles given text.

    The roles that wait are found apart from the writer's turn (`off_turn.reading`: the scan of every role's
    description held the file's one turn for a long while a turn, though nearly
    every turn finds none), and written `WHOLE_BATCH` to a transaction, each batch read again with the same conditions
    in the transaction that writes it, so a role that changed meanwhile is left as it is."""
    now = now or datetime.now(UTC)
    size = func.length(func.trim(func.coalesce(Opportunity.description, "")))
    wanted = (Opportunity.owner == owner, size > 0, size < cleansed.MIN_TEXT,
              select(JobPosting.id).where(
                  JobPosting.owner == owner, JobPosting.opportunity_id == Opportunity.id,
                  func.length(func.coalesce(JobPosting.text, "")) >= cleansed.MIN_TEXT).exists())
    async with off_turn.reading(session) as reader:
        waiting = list((await reader.scalars(select(Opportunity.id).where(*wanted).order_by(Opportunity.id))).all())
    given = []
    for at in range(0, len(waiting), WHOLE_BATCH):
        # By key, the database saying whether each still waits (the conditions in the select list: beside `id IN (...)`
        # in the where SQLite chooses a walk of the roles over one by key).
        found = (await session.execute(
            select(Opportunity, and_(*wanted).label("waits")).where(Opportunity.id.in_(waiting[at:at + WHOLE_BATCH]))
            .order_by(Opportunity.id).execution_options(populate_existing=True))).all()
        taken = []
        for role in (role for role, waits in found if waits):
            rows = await role_postings.of_role(session, role)
            best = _fullest(rows)
            if best is None:
                continue
            role.description, role.description_at = _text(best), now
            posted_dates.apply(role, rows, text=_text(best), system=best.posted, read_at=best.detail_at or now)
            taken.append(role.id)
        if taken:
            await session.flush()
            await raw_copies.keep_whole_text(session, owner, taken, now)
            given += taken
        if at + WHOLE_BATCH < len(waiting):
            await db.release_if_free(session)
    return given
