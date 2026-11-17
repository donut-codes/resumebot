"""The one way to read a posting's cleansed copy: the ranker
(`rank.py`), the gates (`roles.gate_results`, the re-gate in `regate.py`, the pipeline's gating in `pipeline/deck.py`
and the
one gate check of the analyst's research in `analysis.py`), the starred role's rank check (`favorites.py`), the
lifecycle's check that a role has its text (`lifecycle.py`) and the pool's check for a copy to rank (`pool/core.py`)
read
postings through this module and never import the raw model (`models.RawPosting`); a test fails if the ranker or this
module does (`tests/jobsearch/test_rank.py`). What a posting's raw rows hold is for the cleaner alone (`raw_copies.py`,
`cleanse.py`); everything that judges a posting judges the cleansed copy.

The current copy of a posting is the cleansed row of its newest raw row with the highest cleaner version, and the
posting names it (`JobPosting.current_cleansed_id`, kept by the code that writes copies: `raw_copies._point`). A role's
copy is that of its posting with the most text (a role on two boards has two postings): the one the ranker reads, the
lifecycle's check that a role has its text and the favorites' rank check (`copy_of`, `copies_by_role`,
`current_for_roles`, and `current_among`, the same copy as a statement for the roles a caller names: the ranker's
read of the roles waiting for a rank, `rank.unranked`).

The gates on the place, the way of working and the pay read another copy of a role with several postings: the one the
owner's rule picks (2026-11-04, `gate_copy`: the employer's own posting, the newest of them, else the newest posting,
among the postings that have been read: a copy of at least `MIN_TEXT` characters), so a role listed in several places
or bands is judged the same way every night (`gate_copy_of`, `gate_copies_by_role`, `gate_current_for_roles`). For a
role with one posting, the two are the same copy. Reads only; services flush."""

import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, fields

from sqlalchemy import ColumnElement, Select, and_, case, exists, false, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app import db
from app.jobsearch import off_turn
from app.jobsearch.models import CleansedPosting, JobPosting, Opportunity
from app.jobsearch.postings import gate_copy

#: The fewest characters of description a posting must have to be ranked or read as a posting: under this a copy is
#: a title and a few header lines, and a rank of it would be a rank of the title alone (the owner, 2026-10-21).
MIN_TEXT = 300
#: The most bytes one character takes in the database's encodings (UTF-8: four): a text of this many times `MIN_TEXT`
#: bytes has at least `MIN_TEXT` characters.
_MAX_CHAR_BYTES = 4

#: Reading a posting's current copy is reading `JobPosting.current_cleansed_id`, which the code that writes copies keeps
#: (`raw_copies._point`): the copy of the posting's newest raw row at its highest cleaner version. It was a search of
#: every raw row the owner has for each posting's newest (`DISTINCT ON`, then a lookup of that row's copies), several
#: statements of it, each over every raw and cleansed copy kept; the engine has no `DISTINCT ON`
#: and a window over that many rows is the cost this avoids. The pointer is the same answer
#: (`tests/core/test_sqlite_posting_copies.py` holds every statement here to the definition, `repoint`). A posting
#: whose pointer names a copy that is gone has no copy: every statement joins the copy.
_P, _K = JobPosting, CleansedPosting


def _size_of(copy_id) -> ColumnElement:
    """The characters of text of the copy with this id (a scalar subquery)."""
    other = aliased(CleansedPosting)
    return select(func.length(other.text_clean)).where(other.id == copy_id).scalar_subquery()


def _held(owner: str, *where: ColumnElement):
    """The postings of roles that have a current copy: the role, the posting, the copy and how many postings of the role
    have one (`postings`). Held by `ix_js_postings_owner_role_copy` alone, which has all four: the rows of the table
    (the posting's text is most of it) are not opened."""
    return (select(_P.opportunity_id.label("role_id"), _P.id.label("posting_id"),
                   _P.current_cleansed_id.label("cleansed_id"),
                   func.count().over(partition_by=_P.opportunity_id).label("postings"))
            .join(_K, _K.id == _P.current_cleansed_id)
            .where(_P.owner == owner, _P.opportunity_id.is_not(None), _P.current_cleansed_id.is_not(None), *where))


def _current(owner: str, *where: ColumnElement):
    """The role and the cleansed copy of its posting that has the most text, one row for each role that has a copy
    (`where` narrows the postings by their role, never to some of a role's postings: the choice is among all of them).
    Costly in the text it measures
    (`length` unpacks the stored text): only a role with several postings needs the measure, to choose the one with the
    most text, so the length is read for those roles and no others (`postings` counts a role's postings that have a
    copy). The one posting of every other role is its copy as it is."""
    found = _held(owner, *where).subquery("found")
    sized = select(found.c.role_id, found.c.cleansed_id,
                   case((found.c.postings > 1, _size_of(found.c.cleansed_id))).label("size")).subquery("sized")
    best = select(sized.c.role_id, sized.c.cleansed_id,
                  func.row_number().over(partition_by=sized.c.role_id,
                                         order_by=(sized.c.size.desc().nulls_last(), sized.c.cleansed_id)).label("n")
                  ).subquery("best")
    return select(best.c.role_id, best.c.cleansed_id).where(best.c.n == 1)


def _any_current(owner: str):
    """Whether any role has a current copy: `_current` is not empty, found without reading the rest once one is
    found."""
    return select(exists(select(1).select_from(_P).join(_K, _K.id == _P.current_cleansed_id).where(
        _P.owner == owner, _P.opportunity_id.is_not(None), _P.current_cleansed_id.is_not(None))))


def _current_of(owner: str, role_id: uuid.UUID):
    """The same for one role: only the role's own postings are read (`ix_js_postings_opportunity_id`)."""
    return (select(_P.opportunity_id.label("role_id"), _P.current_cleansed_id.label("cleansed_id"))
            .join(_K, _K.id == _P.current_cleansed_id)
            .where(_P.owner == owner, _P.opportunity_id == role_id)
            .order_by(func.length(_K.text_clean).desc(), _P.current_cleansed_id).limit(1))


def _gate_choices(owner: str, *where: ColumnElement, thin: bool):
    """The postings of roles that have a current copy, for the gates' choice among them (`gate_copy.pick`): the posting,
    its copy and what the choice weighs. With `thin` the choice's facts (the posting's links and dates, the text's
    size and the address of the role's Sourced employer posting, `source`) are read only for a role that has several
    postings that have a copy (`postings`): a role with one has its copy and nothing else read, and the text of the
    re-gate's roles is not measured. Without it they are read for every posting asked about (a role, or the
    few roles of one statement)."""
    found = _held(owner, *where).subquery("found")
    posting, role = aliased(JobPosting), aliased(Opportunity)
    several = found.c.postings > 1
    source = select(role.source_url).where(role.id == found.c.role_id, role.source_kind == "employer").scalar_subquery()
    size = _size_of(found.c.cleansed_id)
    facts = (posting.ats, posting.url, posting.apply_url, posting.links, posting.posted, posting.first_seen)
    if thin:  # the posting's own row is opened only for a role that has a choice
        query = select(found.c.role_id, found.c.cleansed_id, found.c.postings, found.c.posting_id, *facts,
                       case((several, size)).label("size"), case((several, source)).label("source")).outerjoin(
            posting, and_(posting.id == found.c.posting_id, several))
    else:
        query = select(found.c.role_id, found.c.cleansed_id, found.c.postings, found.c.posting_id, *facts,
                       size.label("size"), source.label("source")).join(posting, posting.id == found.c.posting_id)
    return query


async def current_for_roles(session: AsyncSession, owner: str) -> dict[uuid.UUID, uuid.UUID]:
    """The id of the current cleansed copy of each role that has one, by role id."""
    return {role_id: cleansed_id for role_id, cleansed_id in await off_turn.rows_in_slices(session, _current(owner))}


def current_among(owner: str, roles: Select[uuid.UUID]) -> Select[uuid.UUID, uuid.UUID]:
    """`current_for_roles` as a statement, for the roles `roles` names alone (a select of role ids): `role_id` and
    `cleansed_id`, one row for each of those roles that has a copy, the same copy `current_for_roles` gives it. For a
    caller that narrows further in the database: the ranker asks for the roles in play that hold no rank
    (`rank.unranked`: reading the copy of every role the owner has would be slow, to pick a few). Only those roles'
    postings are read, from `ix_js_postings_owner_role_copy`."""
    return _current(owner, _P.opportunity_id.in_(roles))


async def any_current(session: AsyncSession, owner: str) -> bool:
    """True when at least one role has a current cleansed copy (`current_for_roles` is not empty)."""
    return bool((await session.execute(_any_current(owner))).scalar_one())



def short_sql(column):
    """`length(column) < MIN_TEXT` as a SQL expression: whether a text has under `MIN_TEXT` characters, with the same
    answer for every text (`tests/jobsearch/test_current_copy_query.py` holds it to `length` on every shape). A text of
    `_MAX_CHAR_BYTES` times `MIN_TEXT` bytes or more is never short, and `octet_length` gives a stored text's size from
    its header without unpacking it, where `length` unpacks every stored text to count its characters (far slower over
    many copies): the characters are counted only for the texts under that size."""
    return case((func.octet_length(column) >= _MAX_CHAR_BYTES * MIN_TEXT, false()),
                else_=func.length(column) < MIN_TEXT)


async def short_ids(session: AsyncSession, ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    """Of these cleansed copies, the ids of those with under `MIN_TEXT` characters of text: no real posting text yet."""
    wanted = list(ids)
    if not wanted:
        return set()
    return set(await session.scalars(select(CleansedPosting.id).where(
        db.among(CleansedPosting.id, wanted), short_sql(CleansedPosting.text_clean))))


async def copies(session: AsyncSession, owner: str, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, CleansedPosting]:
    """The cleansed copies with these ids, by id."""
    wanted = list(ids)
    if not wanted:
        return {}
    rows = await session.scalars(select(CleansedPosting).where(CleansedPosting.owner == owner,
                                                               db.among(CleansedPosting.id, wanted)))
    return {c.id: c for c in rows}


async def copy_of(session: AsyncSession, owner: str, role_id: uuid.UUID) -> CleansedPosting | None:
    """The role's current cleansed copy, or None when it has none (a role made before its posting was read)."""
    found = await session.execute(_current_of(owner, role_id))
    row = found.first()
    return (await copies(session, owner, [row.cleansed_id])).get(row.cleansed_id) if row is not None else None


async def copies_by_role(session: AsyncSession, owner: str) -> dict[uuid.UUID, CleansedPosting]:
    """The current cleansed copy of each role that has one, by role id."""
    current = await current_for_roles(session, owner)
    held = await copies(session, owner, current.values())
    return {role_id: held[copy_id] for role_id, copy_id in current.items() if copy_id in held}


# --- the copy the place and pay gates read --------------------------------------------------------------------------


def _choice(row) -> gate_copy.Choice:
    """A posting of a role that has a copy, as the gates' choice reads it, from a row of `_GATE_CHOICES`."""
    links = row.links
    if isinstance(links, str):
        links = json.loads(links)
    held = tuple(str(link) for link in (row.url, row.apply_url, *(links or [])) if link)
    return gate_copy.Choice(row.posting_id, row.cleansed_id, row.ats, held, row.posted, row.first_seen, row.size or 0)


def _gate_pick(rows) -> uuid.UUID | None:
    """The id of the copy the gates read for a role, from the rows of its postings that have a copy (`gate_copy.pick`);
    the one a role with a single posting has is that posting's."""
    rows = list(rows)
    if len(rows) == 1:
        return rows[0].cleansed_id
    picked = gate_copy.pick([_choice(row) for row in rows], next((row.source for row in rows if row.source), None),
                            MIN_TEXT)
    return picked.cleansed_id if picked is not None else None


async def gate_current_for_roles(session: AsyncSession, owner: str) -> dict[uuid.UUID, uuid.UUID]:
    """The id of the copy the place and pay gates read for each role that has one, by role id (`gate_copy`)."""
    by_role: dict[uuid.UUID, list] = {}
    for row in (await session.execute(_gate_choices(owner, thin=True))).all():
        by_role.setdefault(row.role_id, []).append(row)
    return {role_id: copy_id for role_id, rows in by_role.items() if (copy_id := _gate_pick(rows)) is not None}


async def gate_copy_of(session: AsyncSession, owner: str, role_id: uuid.UUID) -> CleansedPosting | None:
    """The copy the place and pay gates read for the role, or None when it has none (`gate_copy`)."""
    rows = (await session.execute(_gate_choices(owner, _P.opportunity_id == role_id, thin=False))).all()
    chosen = _gate_pick(rows) if rows else None
    return (await copies(session, owner, [chosen])).get(chosen) if chosen is not None else None


@dataclass(frozen=True, slots=True)
class GateCopy:
    """A cleansed copy as the place, way of working and pay gates read it (`check.role_subject`, `check.silent_copy`):
    the fields it states, the marks (the way of working's source), whether it is partial and its text (a way of working
    stated in the words). Nothing else of the row is read by a gate, so the re-gate, which reads the copy of every role
    (a large library), loads these columns alone, as plain values: not the removed rules, the raw rows
    the copy came from or the title, and no object for the session to track (`gate_copies_by_role`). The names are the
    columns' (`tests/jobsearch/test_gate_copy.py` holds the two to each other)."""

    id: uuid.UUID
    locations: list
    employer: str | None
    work_mode: str | None
    pay_min: int | None
    pay_max: int | None
    partial: bool
    marks: dict
    text_clean: str


#: The columns of a `GateCopy`, in its order.
_GATE_COLUMNS = tuple(getattr(CleansedPosting, field.name) for field in fields(GateCopy))


async def gate_copies_by_role(session: AsyncSession, owner: str) -> dict[uuid.UUID, GateCopy]:
    """The copy the place and pay gates read for each role that has one, by role id (`gate_current_for_roles`), as the
    gates read it (`GateCopy`): the same copy `gate_copy_of` finds for the role, with the columns the gates read."""
    current = await gate_current_for_roles(session, owner)
    if not current:
        return {}
    rows = await session.execute(select(*_GATE_COLUMNS).where(CleansedPosting.owner == owner,
                                                              db.among(CleansedPosting.id, current.values())))
    held = {row.id: GateCopy(*row) for row in rows}
    return {role_id: held[copy_id] for role_id, copy_id in current.items() if copy_id in held}


async def gate_copies_of(session: AsyncSession, owner: str, role_ids: Iterable[uuid.UUID]
                         ) -> dict[uuid.UUID, GateCopy]:
    """`gate_copies_by_role` for these roles alone: the re-gate reads the roles a slice at a time, so no read carries
    every role's copy at once."""
    current = await gate_copy_ids_for(session, owner, role_ids)
    if not current:
        return {}
    rows = await session.execute(select(*_GATE_COLUMNS).where(CleansedPosting.owner == owner,
                                                              db.among(CleansedPosting.id, current.values())))
    held = {row.id: GateCopy(*row) for row in rows}
    return {role_id: held[copy_id] for role_id, copy_id in current.items() if copy_id in held}


async def gate_copy_ids_for(session: AsyncSession, owner: str, role_ids: Iterable[uuid.UUID]
                            ) -> dict[uuid.UUID, uuid.UUID]:
    """The id of the copy the place and pay gates read for each of these roles that has one, by role id: what
    `gate_copy_of` chooses for each, in one statement (`_gate_choices`). A role with no copy is not in the answer.
    Read again under a lock by a caller that chose from an earlier read: a copy is made new by every cleaning and never
    changed, so the same id is the same copy."""
    wanted = list(dict.fromkeys(role_ids))
    if not wanted:
        return {}
    by_role: dict[uuid.UUID, list] = {}
    for row in (await session.execute(_gate_choices(owner, db.among(_P.opportunity_id, wanted), thin=False))).all():
        by_role.setdefault(row.role_id, []).append(row)
    return {role_id: copy_id for role_id, rows in by_role.items() if (copy_id := _gate_pick(rows)) is not None}


async def gate_copies_for(session: AsyncSession, owner: str, role_ids: Iterable[uuid.UUID]
                          ) -> dict[uuid.UUID, CleansedPosting]:
    """The copy the place and pay gates read for each of these roles that has one, by role id: `gate_copy_of` for each,
    in two statements whatever the number of roles. A role with no copy is not in the answer."""
    chosen = await gate_copy_ids_for(session, owner, role_ids)
    held = await copies(session, owner, chosen.values())
    return {role_id: held[copy_id] for role_id, copy_id in chosen.items() if copy_id in held}
