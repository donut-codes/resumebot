"""Which employer records there are: merging two into one, splitting roles out of one (docs/PRD.md 36.5 and 36.7).
The owner does these by hand on an employer's page; the code merges two records by itself when they share a website or
a job-system board, and asks about the rest (`employers/pairs.py`); the employer researcher calls the same functions
with
its evidence. Nothing is done to the records from outside the app.

- **Merge** (`merge`): every role, posting and site of one record moves onto the other, with the facts of both (the
  owner's own word first, then the record that is kept, then the other), the hard pass of either (it holds on the one
  record left), its watch, its notes, the names it goes by, and the Inbox and Activity lines that point at it. The other
  name stays on the kept record as one it is also called, so a role found under it is matched to the record that is
  left. The kept record takes the fuller name (the one with the legal name, not an acronym or a "via" tag) unless the
  owner named one. The other record is deleted and a line in Activity says what was merged and why.
- **Split** (`split`): chosen roles (with the postings that became them, and any sites and names named) move onto
  another record, made when there is none. Nothing else of the old record changes.

Each is one step in the caller's transaction and a savepoint of its own: a failure part way changes nothing, and it can
be asked again (a merge of a record that is gone, a split of roles already moved, do nothing). A merge or a split that
moved something ends by settling whom the app watches (`where.settle_watch`, docs/PRD.md 29.9), since the roles and
sites that moved change which employer has a role in play. Services flush; the route or the command commits.
"""

import json
import re
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.jobsearch import ats
from app.jobsearch.actors import BY_APP, BY_OWNER, actor_name
from app.jobsearch.employers import core as employers
from app.jobsearch.employers.core import EmployerError, NoSuchEmployer, key_of
from app.jobsearch.models import Employer, JobPosting, Opportunity
from app.jobsearch.postings import raw_copies
from app.jobsearch.stages import GATED_OUT
from app.jobsearch.work import queue
from app.models.work import Notification, Task, TrailEvent
from app.sites import watch_record, where
from app.sites.models import SEARCH, Site
from app.trail import service as trail

#: What an Inbox task, a notification or a line of Activity is linked to when it is about an employer record.
LINK = "employer"

#: Facts that are one value each: the owner's word first, then the record that is kept, then the other record's.
_ONE = ("website", "careers_url", "reviews_url", "headquarters", "work_style", "employees", "founded", "industry",
        "ownership", "ai_native", "group")
#: Facts that are lists: the entries of both records.
_MANY = ("offices", "sectors", "events")
#: The job system a record posts on, taken or left as one piece (a board address without its read is of no use).
_BOARD = ("ats", "board", "board_url", "ats_found_by", "fetched_at", "fetch_ok", "fetch_note", "fetch_kind",
          "open_roles")
#: The most characters of a name the Activity line repeats.
_SHORT = 40


@dataclass(frozen=True)
class Merged:
    """What a merge did. `merged` is False when the other record was already gone (nothing was done). `postings` is how
    many moved and `folded` how many were the same posting read under both records (now one); `gated_out` how many roles
    the hard pass then sent to Gated out."""

    merged: bool
    employer: Employer
    roles: int = 0
    postings: int = 0
    folded: int = 0
    sites: int = 0
    names: tuple[str, ...] = ()
    gated_out: int = 0
    #: With `defer_refresh`: what the caller asks the engine to work out again (the ask `employer.refresh`, its args),
    #: else None.
    refresh: dict | None = None


@dataclass(frozen=True)
class Split:
    """What a split did: the record the roles went to (made by this split when `created`), what moved, the names that
    went with them, and how many of the roles that moved are still in Gated out for the old record's hard pass (they
    stay there: the owner moves one back by hand)."""

    employer: Employer
    created: bool
    roles: int = 0
    postings: int = 0
    sites: int = 0
    names: tuple[str, ...] = ()
    still_gated_out: int = 0
    #: With `defer_refresh`: what the caller asks the engine to work out again (the ask `employer.refresh`, its args),
    #: else None.
    refresh: dict | None = None


# --- small helpers ------------------------------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _n(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _short(name: str) -> str:
    return name if len(name) <= _SHORT else name[:_SHORT - 1].rstrip() + "…"


def _names(names, exclude: str = "") -> list[str]:
    """The names, trimmed and without repeats (ignoring case) or the one named `exclude`, in order."""
    seen = {" ".join(exclude.split()).lower()}
    out: list[str] = []
    for name in names or []:
        text = " ".join(str(name).split())
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out


def _union(first: list, second: list) -> list:
    """The entries of both lists, the first's before the second's, each once (a text ignoring case)."""
    out, seen = [], set()
    for item in [*first, *second]:
        mark = item.strip().lower() if isinstance(item, str) else json.dumps(item, sort_keys=True, default=str)
        if mark not in seen:
            seen.add(mark)
            out.append(item)
    return out


def _blank(field: str, value) -> bool:
    return value is None or value == "" or value == [] or (field in ("work_style", "ownership") and value == "unknown")


def _fullness(name: str) -> int:
    """How much of a real name a name is: how many words it has outside any parenthesis ("Grand River Independent
    Distributor (GRID)" has four, "GRID (Grand River Independent Distributor)" one)."""
    return len(re.findall(r"[A-Za-z0-9]+", re.sub(r"\(.*?\)", " ", name)))


def chosen_name(keep: Employer, away: Employer) -> str:
    """The name the one record left goes by: the owner's own if either was named by them (the kept record's first), else
    the one with more words outside any parenthesis (the kept record's when they have as many)."""
    if "name" in (keep.owner_fields or []):
        return keep.name
    if "name" in (away.owner_fields or []):
        return away.name
    return away.name if _fullness(away.name) > _fullness(keep.name) else keep.name


async def _who(session: AsyncSession, owner: str, by: str) -> dict:
    """Who the Activity line says did it: the owner by name, the app itself, or an agent."""
    if by == BY_OWNER:
        return {"actor": "person", "who": await actor_name(session, owner)}
    if by == BY_APP:
        return {"actor": "app"}
    return {"actor": "agent", "who": by}


async def get(session: AsyncSession, owner: str, employer_id: uuid.UUID, lock: bool = False) -> Employer:
    """The owner's record with this id (`NoSuchEmployer` when there is none); `lock` reads it afresh: the write
    transaction is the one writer, so nothing else changes it before the caller's write."""
    query = select(Employer).where(Employer.owner == owner, Employer.id == employer_id)
    if lock:
        query = query.execution_options(populate_existing=True)
    employer = await session.scalar(query)
    if employer is None:
        raise NoSuchEmployer("no such employer")
    return employer


async def _lock(session: AsyncSession, owner: str, *ids: uuid.UUID) -> dict[uuid.UUID, Employer]:
    """The records, in one order, read fresh: the write transaction is the one writer, so two merges at once take
    turns."""
    rows = (await session.scalars(
        select(Employer).where(Employer.owner == owner, Employer.id.in_(ids)).order_by(Employer.id)
        .execution_options(populate_existing=True))).all()
    return {e.id: e for e in rows}


async def tally(session: AsyncSession, owner: str, employer_id: uuid.UUID) -> dict[str, int]:
    """How many roles, postings and sites a record holds."""
    return {
        "roles": await session.scalar(select(func.count()).select_from(Opportunity).where(
            Opportunity.owner == owner, Opportunity.employer_id == employer_id)) or 0,
        "postings": await session.scalar(select(func.count()).select_from(JobPosting).where(
            JobPosting.owner == owner, JobPosting.employer_id == employer_id)) or 0,
        "sites": await session.scalar(select(func.count()).select_from(Site).where(
            Site.owner == owner, Site.employer_id == employer_id)) or 0,
    }


# --- a new record (the one a split moves roles to) ----------------------------------------------------------------


async def _make(session: AsyncSession, owner: str, name: str, aliases, by: str) -> Employer:
    clean = " ".join((name or "").split())
    key = key_of(clean)
    if not key:
        raise EmployerError("give the employer a name")
    employer = Employer(owner=owner, key=key, name=clean, aliases=_names(aliases, exclude=clean), offices=[],
                        sectors=[], events=[], sources={}, owner_fields=[])
    if by == BY_OWNER:  # a name the owner typed is theirs, which a merge later keeps
        employer.owner_fields = ["name"]
        employer.sources = {"name": {"source": "you", "at": _now(), "by": by}}
    session.add(employer)
    await session.flush()
    return employer


# --- the facts ------------------------------------------------------------------------------------------------------


def _watched_by(employer: Employer) -> str:
    """Who set the employer's watch (`watch_record.py`): the app for a role in play, else the owner, whose it also is
    when nothing was kept."""
    return watch_record.BY_PLAY if watch_record.set_by(employer) == watch_record.BY_PLAY else watch_record.BY_OWNER


def _watch_source(keep: Employer, away: Employer) -> dict | None:
    """What the one record left should say of who set its watch, when the kept record's own word is not it: the owner's
    when the owner set the watch of either (the app never unwatches what the owner watched), the app's when it set the
    only one there is. None when the kept record's own word stands."""
    mine = _watched_by(keep) if keep.check_directly else None
    theirs = _watched_by(away) if away.check_directly else None
    if theirs == watch_record.BY_OWNER and mine != watch_record.BY_OWNER:
        return (away.sources or {}).get(watch_record.FACT) or {
            "source": watch_record.SOURCE_WORDS[watch_record.BY_OWNER], "at": _now(), "by": watch_record.BY_OWNER}
    if theirs == watch_record.BY_PLAY and mine is None:
        return (away.sources or {}).get(watch_record.FACT)
    return None


def _take_facts(keep: Employer, away: Employer, by: str) -> tuple[list[str], list[str]]:
    """Fill the kept record with the other's facts, each with its source. For a fact of one value: the owner's word wins
    (the kept record's before the other's), else the kept record's own, else the other's where the kept record has none.
    The lists (offices, sectors, events) hold the entries of both, and the notes both texts. Whether it is local, hard
    passed or watched is yes if either is, and a watch is the owner's if either was (`_watch_source`). Returns the
    fields filled from the other record, and the values the owner set on the other that the kept record's own word beat
    (for the Activity line's detail)."""
    stamp = _now()
    keep_own, away_own = set(keep.owner_fields or []), set(away.owner_fields or [])
    sources = dict(keep.sources or {})
    taken: list[str] = []
    beaten: list[str] = []

    def source_of(field: str) -> dict:
        return (away.sources or {}).get(field) or {"source": f"the record for {away.name}", "at": stamp, "by": by}

    for field in _ONE:
        mine, theirs = getattr(keep, field), getattr(away, field)
        if field in keep_own:
            if field in away_own and not _blank(field, theirs) and theirs != mine:
                beaten.append(f"{field}: {theirs}")
            continue
        if field in away_own or (_blank(field, mine) and not _blank(field, theirs)):
            if theirs != mine:
                setattr(keep, field, theirs)
                sources[field] = source_of(field)
                taken.append(field)
            if field in away_own:
                keep_own.add(field)
    for field in _MANY:
        mine, theirs = list(getattr(keep, field) or []), list(getattr(away, field) or [])
        both = _union(mine, theirs)
        if both != mine:
            setattr(keep, field, both)
            taken.append(field)
            if field not in sources and field in (away.sources or {}):
                sources[field] = source_of(field)
        if field in away_own:
            keep_own.add(field)
    notes_a, notes_b = (keep.notes or "").strip(), (away.notes or "").strip()
    if notes_b and notes_b not in notes_a:
        keep.notes = f"{notes_a}\n\n{notes_b}" if notes_a else notes_b
        taken.append("notes")
    if "notes" in away_own:
        keep_own.add("notes")
    if "local" in keep_own:
        pass
    elif "local" in away_own:
        keep.local = away.local
        keep_own.add("local")
    else:
        known = [v for v in (keep.local, away.local) if v is not None]
        keep.local = any(known) if known else None
    watch = _watch_source(keep, away)  # read before the kept record's watch takes the other's
    keep.check_directly = bool(keep.check_directly or away.check_directly)
    if watch is not None:
        sources[watch_record.FACT] = watch
    if away.gated:  # a hard pass on either holds on the record that is left
        if not keep.gated:
            keep.gated, keep.gated_reason, keep.gated_at = True, away.gated_reason, away.gated_at or datetime.now(UTC)
            for field in ("gated", "gated_reason"):
                if field in away_own:
                    keep_own.add(field)
                if field in (away.sources or {}):
                    sources[field] = away.sources[field]
        else:
            keep.gated_reason = keep.gated_reason or away.gated_reason
            if away.gated_at and (keep.gated_at is None or away.gated_at < keep.gated_at):
                keep.gated_at = away.gated_at
    if _board_rank(away) > _board_rank(keep):  # the job system goes whole, from the record that knows it best
        for field in _BOARD:
            setattr(keep, field, getattr(away, field))
        if "ats" in away_own:
            keep_own.add("ats")
        taken.append("job system")
    keep.sources = sources
    keep.owner_fields = sorted(keep_own)
    return taken, beaten


def _board_rank(employer: Employer) -> tuple[bool, bool, bool, bool]:
    """How good a record's job system is: known, set by the owner, whole, and confirmed to be this employer's."""
    owned = "ats" in (employer.owner_fields or []) or employer.ats_found_by == "owner"
    return (bool(employer.ats), owned, ats.complete(employer.ats, employer.board),
            (employer.board or {}).get("confirmed", True) is not False)


# --- postings and sites ----------------------------------------------------------------------------------------------


def _fold(kept: JobPosting, other: JobPosting, texts: tuple[str, str] | None = None) -> None:
    """`other` is the same posting as `kept` (the same system's id, read under two records): what only it knew is kept
    on `kept`, the role it became first. `texts` is given when `kept` is given `other`'s text as its copy
    (`raw_copies.fuller_text`): each posting's own text, `kept`'s and `other`'s. What the posting holds with that text
    goes with it: `other`'s own text when `kept`'s is shorter, and when it was read when `kept` was read before it or
    never."""
    if kept.opportunity_id is None and other.opportunity_id is not None:
        kept.opportunity_id = other.opportunity_id
    if texts is not None:
        mine, theirs = texts
        if len(theirs) > len(mine):
            kept.text = theirs
        if other.detail_at is not None and (kept.detail_at is None or kept.detail_at < other.detail_at):
            kept.detail_at = other.detail_at
    kept.owner_override = bool(kept.owner_override or other.owner_override)
    kept.first_seen = min(kept.first_seen, other.first_seen)
    kept.last_seen = max(kept.last_seen, other.last_seen)
    if kept.closed_at is not None:
        kept.closed_at = None if other.closed_at is None else max(kept.closed_at, other.closed_at)
    kept.links = _union(list(kept.links or []), list(other.links or []))


async def _own_texts(session: AsyncSession, kept: JobPosting, other: JobPosting) -> tuple[str, str]:
    """Each posting's own text, `kept`'s and `other`'s: read here because `_move_postings` loads them without it."""
    texts = {posting_id: text or "" for posting_id, text in (await session.execute(
        select(JobPosting.id, JobPosting.text).where(JobPosting.id.in_([kept.id, other.id])))).all()}
    return texts.get(kept.id, ""), texts.get(other.id, "")


def _moved(to_id: uuid.UUID, from_id: uuid.UUID) -> list[tuple[str, list[object]]]:
    """What moving roles or postings from one employer record to another asks to be looked at: both employers' limits,
    the one that now holds them for research, the browser's queue, the gates that read every employer, the deck and
    the pool's pull (`employer.picks` reads which employer a role worth researching has)."""
    return [(queue.EMPLOYER_LIMIT, [from_id, to_id]), (queue.EMPLOYER_RESEARCH, [to_id]),
            (queue.BROWSER_RECONCILE, [queue.ANY]), (queue.RULES_CHANGED, ["employers"]), (queue.DECK_DIRTY, [""]),
            (queue.POOL_OWED, [""])]


async def _move_postings(session: AsyncSession, owner: str, to_id: uuid.UUID, from_id: uuid.UUID,
                         only: list[uuid.UUID] | None = None) -> tuple[int, int]:
    """Put the postings read under `from_id` (all of them, or just `only`) under `to_id`. A posting both records read
    (the same id of the same job system) is one: the one already under `to_id` stays, with the role the other became.
    The stored copies of each posting that moves go with it (`raw_copies.follow`): they are kept under the posting's
    address, so one that moved without them would have no current copy. Those of a posting folded into its twin stay
    where they are when the twin holds any of its own: it keeps those, and its current copy with them, unless the
    folded posting's copy holds more text (the twin read a list item, the other the page): then that text is entered
    on the twin as a row of its own (`raw_copies.keep_text`), so the role it takes is not left with the thinner text
    until its page is read again, which a closed posting's never is. A twin that holds none takes them as a posting
    that moves does: it has the role now, and the rows would be nobody's under a record a merge deletes. Returns
    (moved, folded)."""
    wanted = [JobPosting.owner == owner, JobPosting.employer_id == from_id]
    if only is not None:
        if not only:
            return 0, 0
        wanted.append(JobPosting.id.in_(only))
    #: Each posting that moves and the id it has after the move, by the id it has before it (`raw_copies.follow`):
    #: the same id, unless the loop below says otherwise.
    went = {ext_id: (posting_id, ext_id) for posting_id, ext_id in (await session.execute(
        select(JobPosting.id, JobPosting.ext_id).where(*wanted))).all()}
    total = len(went)
    there = select(JobPosting.ext_id).where(JobPosting.owner == owner, JobPosting.employer_id == to_id)
    clashing = (await session.scalars(
        select(JobPosting).where(*wanted, JobPosting.ext_id.in_(there)).options(defer(JobPosting.text)))).all()
    twins = {t.ext_id: t for t in (await session.scalars(select(JobPosting).where(
        JobPosting.owner == owner, JobPosting.employer_id == to_id,
        JobPosting.ext_id.in_([p.ext_id for p in clashing] or [""])).options(defer(JobPosting.text)))).all()}
    #: The twins that hold stored copies of their own, by id (an address's rows, whatever posting they name).
    holds = await raw_copies.heads(session, owner, to_id, [
        p.ext_id for p in clashing if p.ext_id in twins and twins[p.ext_id].ats == p.ats])
    folded = 0
    for posting in clashing:
        twin = twins.get(posting.ext_id)
        if twin is not None and twin.ats == posting.ats:
            #: The folded posting's text, when both hold copies and its is the fuller: the twin is given it.
            fuller = await raw_copies.fuller_text(session, twin, posting) if twin.ext_id in holds else None
            _fold(twin, posting, await _own_texts(session, twin, posting) if fuller else None)
            if twin.ext_id in holds:
                del went[posting.ext_id]  # its rows are not the twin's: a copy is made of one posting's rows
            else:
                went[posting.ext_id] = (twin.id, twin.ext_id)  # the twin has none: the rows and their copy are its
            await session.delete(posting)
            if fuller:  # after `_fold`: the twin has the role by now, and the role is told of its new copy
                await raw_copies.keep_text(session, twin, fuller)
            folded += 1
        else:  # the same id in another job system: a different posting, so it keeps its own id
            was = posting.ext_id
            posting.ext_id = f"{posting.ats}:{was}"
            went[was] = (posting.id, posting.ext_id)
    await session.flush()
    await session.execute(update(JobPosting).where(*wanted).values(employer_id=to_id))
    await raw_copies.follow(session, owner, from_id, to_id, went)
    queue.post_all(session, owner, _moved(to_id, from_id), cause="core:employers.move_postings")
    return total - folded, folded


async def _move_sites(session: AsyncSession, owner: str, keep: Employer, away_id: uuid.UUID,
                      watch_by: str = watch_record.BY_OWNER) -> tuple[int, int]:
    """The sites of the other record go to the kept one. When the kept record has an own site already, one the app made
    that holds nothing of its own (`where.is_plain`) is folded into it instead of kept twice: its watch switch carries
    over, set by whoever set it (`watch_by`: the owner, or the app for a role in play). Returns (moved, folded)."""
    sites = list((await session.scalars(
        select(Site).where(Site.owner == owner, Site.employer_id == away_id).order_by(Site.created_at))).all())
    mine = list((await session.scalars(select(Site).where(
        Site.owner == owner, Site.employer_id == keep.id, Site.list_name == SEARCH, Site.kind == "employer")
        .order_by(Site.created_at))).all())
    moved = folded = 0
    for site in sites:
        if mine and await where.is_plain(session, site):
            if site.on and not mine[0].on:
                try:
                    await where.switch(session, owner, mine[0].id, True, by=watch_by)
                except where.WhereError:
                    pass  # its host is one the owner avoids: the watch stays off, as the owner's list says
            await session.delete(site)
            folded += 1
        else:
            site.employer_id = keep.id
            moved += 1
    await session.flush()
    return moved, folded


# --- merge ------------------------------------------------------------------------------------------------------------


async def preview_merge(session: AsyncSession, owner: str, keep_id: uuid.UUID, away_id: uuid.UUID) -> dict:
    """What merging `away_id` into `keep_id` would do, counted, with nothing changed: the name the one record would go
    by, the other names it would also be called, what moves, and what a hard pass would then do."""
    if keep_id == away_id:
        raise EmployerError("pick another employer")
    keep, away = await get(session, owner, keep_id), await get(session, owner, away_id)
    name = chosen_name(keep, away)
    mine, theirs = await tally(session, owner, keep.id), await tally(session, owner, away.id)
    twins = await session.scalar(select(func.count()).select_from(JobPosting).where(
        JobPosting.owner == owner, JobPosting.employer_id == away.id, JobPosting.ext_id.in_(
            select(JobPosting.ext_id).where(JobPosting.owner == owner, JobPosting.employer_id == keep.id)))) or 0
    hard_pass = bool(keep.gated or away.gated)
    to_gated_out = 0
    if hard_pass:
        to_gated_out = await session.scalar(select(func.count()).select_from(Opportunity).where(
            Opportunity.owner == owner, Opportunity.employer_id.in_([keep.id, away.id]),
            Opportunity.status.in_(employers.GATE_TAKES))) or 0
    called = _names([keep.name, away.name, *(keep.aliases or []), *(away.aliases or [])], exclude=name)
    return {
        "keep": {"id": str(keep.id), "name": keep.name, **mine},
        "away": {"id": str(away.id), "name": away.name, **theirs},
        "name": name, "names": called,
        "roles": theirs["roles"], "postings": theirs["postings"] - twins, "folded": twins, "sites": theirs["sites"],
        "hard_pass": hard_pass, "to_gated_out": to_gated_out,
    }


async def merge(session: AsyncSession, owner: str, keep_id: uuid.UUID, away_id: uuid.UUID, *, why: str,
                by: str = BY_OWNER, defer_refresh: bool = False) -> Merged:
    """Merge the record `away_id` into the record `keep_id` (which is the one that stays, with its id). `why` is the
    clause Activity gives ("you merged them by hand", "they share the website acme.test"); `by` is who did it
    (`BY_OWNER`, `BY_APP`, or an agent's name). A record already gone is nothing to do (`merged` False); a record that
    is not the owner's is `NoSuchEmployer`. With `defer_refresh` the roles' gates and scores are not worked out again
    here: the answer carries `refresh` (the ask `employer.refresh`'s args) for the caller to ask the engine."""
    if keep_id == away_id:
        raise EmployerError("that is the same employer")
    async with session.begin_nested():
        found = await _lock(session, owner, keep_id, away_id)
        keep, away = found.get(keep_id), found.get(away_id)
        if keep is None:
            raise NoSuchEmployer("no such employer")
        if away is None:
            return Merged(False, keep)
        done = await _merge(session, owner, keep, away, why, by)
    # The roles and sites that moved change which employer has a role in play, and so whom the app watches (PRD 29.9).
    await where.settle_watch(session, owner)
    if done.merged:  # one record now holds the facts and the roles of both: the gates and scores of its roles run again
        cause = f"{done.employer.name} was merged with another record of it"
        if defer_refresh:  # the caller asks the engine (`employer.refresh`) once it has committed
            return replace(done, refresh={"employer_ids": [str(done.employer.id)], "cause": cause,
                                          "keep_gated_out": False})
        from app.jobsearch.research import (
            employer as employer_research,  # it reads these records: imported here so the two never loop
        )

        await employer_research.after_a_change(session, owner, [done.employer.id], cause=cause)
    return done


async def _merge(session: AsyncSession, owner: str, keep: Employer, away: Employer, why: str, by: str) -> Merged:
    away_id, away_name, away_aliases = away.id, away.name, list(away.aliases or [])
    old_name = keep.name
    name = chosen_name(keep, away)
    away_named = "name" in (away.owner_fields or []) and name == away_name
    away_name_source = (away.sources or {}).get("name")
    roles = (await tally(session, owner, away_id))["roles"]
    await session.execute(update(Opportunity).where(
        Opportunity.owner == owner, Opportunity.employer_id == away_id).values(employer_id=keep.id)
        .execution_options(synchronize_session="fetch"))
    queue.post_all(session, owner, _moved(keep.id, away_id), cause="core:employers.merge")
    postings, folded = await _move_postings(session, owner, keep.id, away_id)
    sites, folded_sites = await _move_sites(session, owner, keep, away_id, _watched_by(away))
    taken, beaten = _take_facts(keep, away, by)
    await session.delete(away)  # nothing points at it now
    await session.flush()
    # The record is stored under its name's key (`employers.key_of`), so a find by the name lands on it; the other
    # record's key is free now. When another record has the key, this one keeps the name and key it had.
    key = key_of(name)
    if not key or await session.scalar(select(Employer.id).where(
            Employer.owner == owner, Employer.key == key, Employer.id != keep.id)) is not None:
        name, key = old_name, keep.key
    if name != keep.name:
        keep.name = name
        if away_named:
            keep.owner_fields = sorted({*(keep.owner_fields or []), "name"})
            if away_name_source:
                keep.sources = {**(keep.sources or {}), "name": away_name_source}
    keep.key = key
    keep.aliases = _names([*(keep.aliases or []), *away_aliases, old_name, away_name], exclude=name)
    gated_out = await employers.gate_roles(session, owner, keep) if keep.gated else 0
    await _repoint(session, owner, keep.id, away_id)
    sentence = f"Merged {_short(away_name)} into {_short(old_name)}: {why}"
    lines = [
        f"{_n(roles, 'role')}, {_n(postings, 'posting')} and {_n(sites + folded_sites, 'site')} of {away_name} moved "
        f"onto {old_name}" + (f", now called {name}" if name != old_name else "")
        + (f"; {_n(folded, 'posting')} read under both records {'is' if folded == 1 else 'are'} now one" if folded
           else "") + ".",
        f"It is also called: {'; '.join(keep.aliases)}." if keep.aliases else "",
        f"Facts taken from {away_name}: {', '.join(taken)}." if taken else "",
        f"Your own word was kept over: {'; '.join(beaten)}." if beaten else "",
        f"{_n(gated_out, 'role')} not yet applied to went to Gated out, as the hard pass holds." if gated_out else "",
    ]
    who = await _who(session, owner, by)
    await trail.record(session, owner, event="employer_merged", sentence=sentence, tier="result",
                       link=(LINK, keep.id), detail="\n".join(line for line in lines if line), **who)
    await session.flush()
    return Merged(True, keep, roles, postings, folded, sites + folded_sites, tuple(keep.aliases), gated_out)


async def _repoint(session: AsyncSession, owner: str, keep_id: uuid.UUID, away_id: uuid.UUID) -> None:
    """What was about the deleted record is about the kept one: the Inbox, the notifications and Activity link to it,
    and the questions about pairs it was in are settled (`employer_pairs.forget`)."""
    from app.jobsearch.employers import pairs as employer_pairs  # the questions about pairs read these records

    with queue.exempt("lines linked to an employer are read by no loop: the role_moved lines link roles"):
        await session.execute(update(Task).where(
            Task.owner == owner, Task.link_kind == LINK, Task.link_id == away_id).values(link_id=keep_id))
        await session.execute(update(Notification).where(
            Notification.owner == owner, Notification.link_kind == LINK, Notification.link_id == away_id)
            .values(link_id=keep_id))
        await session.execute(update(TrailEvent).where(
            TrailEvent.owner == owner, TrailEvent.link_kind == LINK, TrailEvent.link_id == away_id)
            .values(link_id=keep_id))
    await employer_pairs.forget(session, owner, keep_id, away_id)


# --- split ------------------------------------------------------------------------------------------------------------


@dataclass
class _Plan:
    """What a split would move."""

    source: Employer
    target: Employer | None
    new_name: str
    roles: list[Opportunity]
    postings: list[uuid.UUID]
    sites: list[Site]
    names: list[str]


def ids_of(raw, what: str = "an id") -> list[uuid.UUID]:
    """Ids given as text or as ids, each once, in order; one that is not an id is an `EmployerError`."""
    out = []
    for item in raw or []:
        try:
            out.append(item if isinstance(item, uuid.UUID) else uuid.UUID(str(item)))
        except ValueError:
            raise EmployerError(f"{what} is not an id") from None
    return list(dict.fromkeys(out))


async def _plan_split(session: AsyncSession, owner: str, source: Employer, *, role_ids, posting_ids, site_ids, names,
                      into_id: uuid.UUID | None, new_name: str | None) -> _Plan:
    """Work out what a split would move, and refuse one that cannot be done."""
    if (into_id is None) == (not (new_name or "").strip()):
        raise EmployerError("say which employer they move to: one that exists, or a new name")
    target: Employer | None = None
    if into_id is not None:
        target = await get(session, owner, into_id)
    else:
        key = key_of(new_name)
        if not key:
            raise EmployerError("give the employer a name")
        target = await session.scalar(select(Employer).where(Employer.owner == owner, Employer.key == key))
    if target is not None and target.id == source.id:
        raise EmployerError(f"{source.name} is the employer they are at; choose another name")
    roles_asked, postings_asked, sites_asked = ids_of(role_ids, "a role"), ids_of(posting_ids, "a posting"), ids_of(
        site_ids, "a site")
    roles: list[Opportunity] = []
    if roles_asked:
        found = {r.id: r for r in (await session.scalars(select(Opportunity).where(
            Opportunity.owner == owner, Opportunity.id.in_(roles_asked)))).all()}
        for role_id in roles_asked:
            role = found.get(role_id)
            if role is None:
                raise EmployerError("no such role")
            if role.employer_id == source.id:
                roles.append(role)
            elif target is None or role.employer_id != target.id:  # one already moved is done; any other is a mistake
                raise EmployerError(f"{role.title} is not at {source.name}")
    postings = [p for p in (await session.scalars(select(JobPosting.id).where(
        JobPosting.owner == owner, JobPosting.employer_id == source.id,
        JobPosting.opportunity_id.in_([r.id for r in roles] or [None])))).all()]
    if postings_asked:
        mine = set((await session.scalars(select(JobPosting.id).where(
            JobPosting.owner == owner, JobPosting.employer_id == source.id,
            JobPosting.id.in_(postings_asked)))).all())
        theirs = set((await session.scalars(select(JobPosting.id).where(
            JobPosting.owner == owner, JobPosting.employer_id == (target.id if target else None),
            JobPosting.id.in_(postings_asked)))).all())
        if set(postings_asked) - mine - theirs:
            raise EmployerError(f"a posting is not at {source.name}")
        postings = list(dict.fromkeys([*postings, *mine]))
    sites: list[Site] = []
    if sites_asked:
        found_sites = {s.id: s for s in (await session.scalars(select(Site).where(
            Site.owner == owner, Site.id.in_(sites_asked)))).all()}
        allowed = {source.id, *([target.id] if target else [])}
        for site_id in sites_asked:
            site = found_sites.get(site_id)
            if site is None or site.employer_id not in allowed:
                raise EmployerError(f"a site is not {source.name}'s")
            if site.employer_id == source.id:
                sites.append(site)
    # The names that go with the roles: the ones asked for, and the names of the source that the moving roles were found
    # under and no role left behind was (else a later find under that name would land on the old record again).
    leaving = _names(names)
    if roles:
        moving_ids = [r.id for r in roles]
        stay = {key_of(c) for c in (await session.scalars(select(Opportunity.company).where(
            Opportunity.owner == owner, Opportunity.employer_id == source.id,
            Opportunity.id.not_in(moving_ids)))).all()}
        went = {key_of(r.company) for r in roles} - stay - {source.key}
        leaving = _names([*leaving, *(a for a in source.aliases or [] if key_of(a) in went)])
    return _Plan(source, target, " ".join((new_name or "").split()), roles, postings, sites, leaving)


async def preview_split(session: AsyncSession, owner: str, from_id: uuid.UUID, *, role_ids, new_name: str | None = None,
                        into_id: uuid.UUID | None = None, posting_ids=(), site_ids=(), names=()) -> dict:
    """What splitting roles out of a record would do, counted, with nothing changed."""
    source = await get(session, owner, from_id)
    plan = await _plan_split(session, owner, source, role_ids=role_ids, posting_ids=posting_ids, site_ids=site_ids,
                             names=names, into_id=into_id, new_name=new_name)
    gated_out = sum(1 for r in plan.roles if source.gated and r.status == GATED_OUT)
    target = plan.target
    return {
        "from": {"id": str(source.id), "name": source.name},
        "to": {"id": str(target.id) if target else None, "name": target.name if target else plan.new_name,
               "exists": target is not None},
        "roles": len(plan.roles), "postings": len(plan.postings), "sites": len(plan.sites), "names": plan.names,
        "still_gated_out": gated_out,
    }


async def split(session: AsyncSession, owner: str, from_id: uuid.UUID, *, role_ids=(), posting_ids=(), site_ids=(),
                names=(), into_id: uuid.UUID | None = None, new_name: str | None = None, why: str,
                by: str = BY_OWNER, defer_refresh: bool = False) -> Split:
    """Move chosen roles out of the record `from_id` onto another: `into_id`, else the record whose name is `new_name`
    (made when none is). The postings that became those roles go with them; so do the `site_ids` and `posting_ids`
    named and the `names` (other names the old record goes by, which the new one now does), and the old record's names
    that only these roles were found under. A role already at the new record is already moved. `why` is the clause
    Activity gives. With `defer_refresh` the moved roles' gates and scores are not worked out again here: the answer
    carries `refresh` (the ask `employer.refresh`'s args) for the caller to ask the engine."""
    async with session.begin_nested():
        source = await get(session, owner, from_id, lock=True)
        plan = await _plan_split(session, owner, source, role_ids=role_ids, posting_ids=posting_ids, site_ids=site_ids,
                                 names=names, into_id=into_id, new_name=new_name)
        target, created = plan.target, False
        if target is None:
            target, created = await _make(session, owner, plan.new_name, (), by), True
        else:
            target = await get(session, owner, target.id, lock=True)
        for role in plan.roles:
            role.employer_id = target.id
        moved, _folded = await _move_postings(session, owner, target.id, source.id, only=plan.postings)
        for site in plan.sites:
            site.employer_id = target.id
        sent_to_gated_out = await employers.gate_roles(session, owner, target) if target.gated else 0
        if plan.names:
            gone = {key_of(n) for n in plan.names}
            source.aliases = [a for a in source.aliases or [] if key_of(a) not in gone]
            target.aliases = _names([*(target.aliases or []), *plan.names], exclude=target.name)
        gated_out = sum(1 for r in plan.roles if source.gated and r.status == GATED_OUT)
        await session.flush()
        if plan.roles or moved or plan.sites or plan.names or created:
            count = _n(len(plan.roles), "role")
            lines = [f"{count}, {_n(moved, 'posting')} and {_n(len(plan.sites), 'site')} moved from {source.name} to "
                     f"{target.name}.",
                     f"{target.name} is also called: {'; '.join(target.aliases)}." if target.aliases else "",
                     f"{_n(gated_out, 'role')} of them {'is' if gated_out == 1 else 'are'} in Gated out for the hard "
                     f"pass on {source.name}: they stay there until the gates are checked again, which returns the "
                     "ones nothing else stops." if gated_out else "",
                     f"{_n(sent_to_gated_out, 'role')} not yet applied to went to Gated out, as {target.name} is hard "
                     "passed." if sent_to_gated_out else ""]
            who = await _who(session, owner, by)
            await trail.record(
                session, owner, event="employer_split",
                sentence=f"Split {count} out of {_short(source.name)} into {_short(target.name)}: {why}",
                tier="result", link=(LINK, target.id), detail="\n".join(line for line in lines if line), **who)
        done = Split(target, created, len(plan.roles), moved, len(plan.sites), tuple(plan.names), gated_out)
    if done.roles or done.sites:  # who has a role in play, and so whom the app watches, may have changed (PRD 29.9)
        await where.settle_watch(session, owner)
    if done.roles:  # the moved roles read another record's facts now: their gates and scores run again (PRD 39.19)
        cause = f"Roles were split out of {source.name} into {done.employer.name}"
        if defer_refresh:  # the caller asks the engine (`employer.refresh`) once it has committed
            return replace(done, refresh={"employer_ids": [str(done.employer.id)], "cause": cause,
                                          "keep_gated_out": True})
        from app.jobsearch.research import (
            employer as employer_research,  # it reads these records: imported here so the two never loop
        )

        await employer_research.after_a_change(session, owner, [done.employer.id], keep_gated_out=True, cause=cause)
    return done
