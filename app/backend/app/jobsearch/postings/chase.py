"""Which roles the app goes and gets the original posting of: the roles in `models.wants_sourcing`, less the roles in
Followup needed that are too old to be worth it (the owner, 2026-11-04, docs/PRD.md 32.6).

A role in Followup needed has no text of its posting, so it is followed to the employer's own posting, last in the
browser's queue, and the chase costs a session. It is spent only on a role that would still be fresh when the posting is
in hand: the days since the role was posted (`freshness.posted_basis`, the one meaning the gates use) plus a margin the
owner sets (`Settings.chase_fresh_days`, "Chase a missing posting only while at least N days of freshness are left",
default 3) must still pass the owner's freshness hard pass. With a pass of 15 days that is a role at most 12 days old.
The test is the gate's own (`scoring.dial_gate`, or a posting age line's days), asked of the age plus the margin, so it
reads whatever the owner's pass says.

- With no freshness hard pass in force for the role, or a role whose age is not known, nothing says it is too old: it
  is wanted, as every role in Followup needed was.
- A role on deck (Second Impression), a role read first (a star, or a move to Ready to Launch before it was read,
  `models.read_first`) and a role the owner asked research for (its `asked_by` mark, `line.owners_asks`, held until
  the findings are taken) are wanted whatever their age.
- A role that is not wanted is not lost. It waits in Followup needed. Nothing closes it as it ages: the re-gate
  (`gating/regate.py`) stops it once it is over the limit, and that runs when Hard pass is saved, at a start of the
  app that finds stale stops, and when asked for (PRD 32.6, the open question on a role too old to chase). It is
  wanted again if the owner's pass or margin lets it back.
- A board role in Followup needed whose board page nobody has read is not followed to the employer first: the
  app's code reads the page (a `listing` item, docs/PRD.md 48.14) and its text takes the role out of Followup needed;
  the employer's posting is looked for at the read-next mark. `listed` says which roles, `listing` lists them (the same
  freshness margin), and `wanted` leaves them out for the callers that ask (`listing_exempt`: the queue's `source`
  items). A role the owner asked for (a star, a move to Ready to Launch, a research) is sourced at once, and a role with
  no board search posting to read (or whose postings are all read) is sourced as before. Reading the board off in the
  owner's settings (`registry.follow_board_on`) puts every role back on the sourcing it had.

Reads the database and nothing else. Services flush; nothing here commits."""

import asyncio
import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import ColumnElement, and_, exists, false, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.jobsearch import stages
from app.jobsearch.fit import freshness, scoring
from app.jobsearch.measures.pay_place import age_in_days
from app.jobsearch.models import Opportunity, board_listed, is_read_first, read_first, wants_sourcing
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.gates import Gate, GateSet
from app.sites import registry
from app.timeutil import owner_today


def freshness_lines(gates: GateSet, target_role: str | None) -> list[Gate]:
    """The owner's freshness hard pass lines for a role: the Freshness Must dial (`posted_days`) and a posting age line,
    among the lines being checked for a posting matched to this target role (`GateSet.lines_for`: a part that is off
    has none). Empty: no freshness hard pass applies."""
    return [g for g in gates.lines_for(target_role)
            if (g.kind == "job_fact" and g.fact == "posted_days") or (g.kind == "posting_age" and g.days)]


def fresh_after(lines: Iterable[Gate], age: int | None, margin: int) -> bool:
    """Whether a role `age` days old still passes every freshness line `margin` days from now: asked as the gate asks
    it, of `age + margin`. No lines, or an age that is not known, passes (nothing says the role is too old); a line
    that waits (`dial_gate`) is no stop either."""
    if age is None:
        return True
    later = age + margin
    for line in lines:
        if line.kind == "posting_age":
            if line.days and later > line.days:
                return False
        elif scoring.dial_gate(line, {"posted_days": later})[0] == "fail":
            return False
    return True


def listed(owner: str) -> ColumnElement[bool]:
    """The roles whose board page the app's code reads for their text (SQL; docs/PRD.md 48.14): in Followup needed,
    not Sourced, found on the board with a posting of the nightly search nobody has read the page of
    (`models.board_listed`), and not the owner's own (a star, a move to Ready to Launch before the role was read, a
    research they asked for: those are sourced at once, at any hour). Whether the owner reads the board at all is the
    caller's to ask (`listing`, `wanted`)."""
    from app.jobsearch.postings import raw_copies
    from app.jobsearch.research import line as research_line  # it reads the browser code, which reads this module

    board = registry.board_reader("guest")  # it reads postings, which read this module
    if board is None:  # no reader of a followed board's pages: no role waits for one
        return false()
    return and_(Opportunity.status == stages.FOLLOWUP_NEEDED, Opportunity.sourced_at.is_(None), not_(read_first()),
                not_(func.coalesce(research_line.owners_asks(), false())),
                board_listed(board.source_id(owner), raw_copies.board_page()))


#: How many rows a loop of the browser's queue works through before it gives the loop back (`await asyncio.sleep(0)`),
#: and the most rows a load makes at once: about 5 ms of the loop's one core, so no task waits behind a plan or a
#: hand-out for more than that. The queue's loops are thousands of roles and items long.
YIELD_EVERY = 250

#: A list of more than this many roles has the owner's postings read whole (`HeldOnce`); fewer, by the roles' ids. The
#: whole set costs the loop's core the same however few roles ask, while the postings of a
#: thousand roles cost far less, so the whole read pays only for a list of most of the owner's roles
#: (`freshness.NARROW_AT`, the same 20,000, is `freshness.posted_by_role`'s own, for its other callers).
WHOLE_AT = 20_000
#: How many roles' ids one statement carries: their postings are read and made into dates in the one stretch the
#: statement is answered in (`YIELD_EVERY` roles, a few milliseconds).
IDS_AT_ONCE = YIELD_EVERY


class HeldOnce:
    """The dates of the postings of the owner's roles (`freshness.held_postings`), read once for the lists one pass asks
    `wanted` for: the browser's queue reads up to four of them in a plan (the roles to source, the pages of the board's
    to read, and the two that need a board copy), each of which read them again. The postings of the roles asked
    about are read by the roles' ids, each role once (`IDS_AT_ONCE` ids to a statement), and the owner's whole set only
    for a list of more than `WHOLE_AT` roles, after which every later list uses it."""

    def __init__(self) -> None:
        self._whole: freshness.Held | None = None
        self._some: freshness.Held = {}
        self._read: set[uuid.UUID] = set()

    async def posted_by_role(self, session: AsyncSession, owner: str,
                             roles: Iterable[Any]) -> dict[uuid.UUID, freshness.PostedDay]:
        """`freshness.posted_by_role` for these roles, from what this pass has read."""
        roles = list(roles)
        if self._whole is None and len(roles) > WHOLE_AT:
            self._whole = await freshness.held_postings(session, owner)
        if self._whole is None:
            unread = [r.id for r in roles if r.id not in self._read]
            for start in range(0, len(unread), IDS_AT_ONCE):
                ids = unread[start:start + IDS_AT_ONCE]
                self._some.update(await freshness.held_postings(session, owner, ids))
                self._read.update(ids)
        held = self._whole if self._whole is not None else self._some
        days: dict[uuid.UUID, freshness.PostedDay] = {}
        for start in range(0, len(roles), YIELD_EVERY):
            days.update(await freshness.posted_by_role(session, owner, roles[start:start + YIELD_EVERY], held))
            await asyncio.sleep(0)
        return days


async def wanted(session: AsyncSession, owner: str, *where: Any, order_by: Sequence[Any] = (),
                 base: ColumnElement[bool] | None = None, listing_exempt: bool = False,
                 columns: Sequence[InstrumentedAttribute[Any]] | None = None,
                 held: HeldOnce | None = None) -> list[Any]:
    """The roles whose original posting the app goes and gets: `models.wants_sourcing` (or `base`, the condition a
    caller starts from instead), narrowed by `where` and put in `order_by`, with the roles in Followup needed that are
    too old to be worth the chase left out (see the top of this module; a role the owner asked research for is never
    left out for its age). The one list `browser/reader.board_roles` and `postings/sourced.source_held` both read. With
    `listing_exempt`, and while the owner reads the board, the roles whose board page the app's code reads first
    (`listed`) are left out too: their `source` item waits until the page is read. `source_held`, which reads nothing
    and loads no page, never asks for that.

    The roles come as `Opportunity` instances, or, with `columns`, as plain rows of those columns (the browser's queue,
    which builds a dict from a few of them for each role of thousands and writes none: the columns this function reads
    itself, `id`, `status`, `favorited_at`, `ready_asked_at`, `target_role`, `posted`, `found` and `board_url`, must be
    among them). `held` is the postings' dates a caller that asks for several lists reads once."""
    profile = None
    conditions = [Opportunity.owner == owner, wants_sourcing() if base is None else base, *where]
    if listing_exempt:
        profile = await profile_store.current(session, owner)
        if registry.follow_board_on(profile.settings):
            conditions.append(not_(listed(owner)))
    if columns is None:
        rows: list[Any] = list(
            (await session.scalars(select(Opportunity).where(*conditions).order_by(*order_by))).all())
    else:  # plain rows of these columns: no instance of the model is made, or refreshed in the caller's session
        rows = list((await session.execute(select(*columns).where(*conditions).order_by(*order_by))).all())
    behind = [r for r in rows if r.status == stages.FOLLOWUP_NEEDED and not is_read_first(r)]
    if behind:  # the owner's research asks are chased at any age; asked apart, so no plan load reads `analysis`
        from app.jobsearch.research import line as research_line  # it reads the browser code, which reads this module

        asked = set((await session.scalars(select(Opportunity.id).where(
            Opportunity.owner == owner, Opportunity.status == stages.FOLLOWUP_NEEDED,
            research_line.owners_asks()))).all())
        behind = [r for r in behind if r.id not in asked]
    if not behind:
        return rows
    profile = profile or await profile_store.current(session, owner)
    lines = {}
    for n, r in enumerate(behind, 1):
        lines[r.id] = freshness_lines(profile.gates, r.target_role)
        if n % YIELD_EVERY == 0:
            await asyncio.sleep(0)
    checked = [r for r in behind if lines[r.id]]
    if not checked:
        return rows
    posted = await (held.posted_by_role(session, owner, checked) if held is not None
                    else freshness.posted_by_role(session, owner, checked))
    today = owner_today()
    too_old = set()
    for n, r in enumerate(checked, 1):
        if not fresh_after(lines[r.id], age_in_days(posted[r.id].posted, posted[r.id].found, today),
                           profile.settings.chase_fresh_days):
            too_old.add(r.id)
        if n % YIELD_EVERY == 0:
            await asyncio.sleep(0)
    return [r for r in rows if r.id not in too_old]


async def fallback_listing(session: AsyncSession, owner: str, *, order_by: Sequence[Any] = (),
                           columns: Sequence[InstrumentedAttribute[Any]] | None = None,
                           held: HeldOnce | None = None) -> list[Any]:
    """The roles whose board page the app's code reads because their employer's text is out of reach (the owner,
    2026-11-05, the board's copy hook): in the stages a text is asked of, holding no usable text, the employer's
    posting gone, refused or not found (the board copy's `needs_a_page`), and a board posting of theirs whose page
    nobody has read, a posting of the nightly search or the role's own link (`models.board_unread_posting`,
    `board_link_unread_posting`). A role in Followup needed is held to the same freshness margin as `wanted` keeps
    (a role the chase leaves is not worth a page); any other stage is not. Empty with the board reading off."""
    from app.jobsearch.models import JobPosting, board_link_unread_posting, board_unread_posting
    from app.jobsearch.postings import raw_copies

    board, board_copy = registry.board_reader("guest"), registry.board_reader("copy")  # they read postings
    if board is None or board_copy is None:
        return []
    if not registry.follow_board_on((await profile_store.current(session, owner)).settings):
        return []
    unread = exists().where(JobPosting.opportunity_id == Opportunity.id, or_(
        board_unread_posting(board.source_id(owner), raw_copies.board_page()),
        board_link_unread_posting(raw_copies.board_page())))
    return await wanted(session, owner, board_copy.needs_a_page(), unread, order_by=order_by,
                        base=Opportunity.status.in_(board_copy.STAGES), columns=columns, held=held)


async def listing(session: AsyncSession, owner: str, *, order_by: Sequence[Any] = (),
                  columns: Sequence[InstrumentedAttribute[Any]] | None = None,
                  held: HeldOnce | None = None) -> list[Any]:
    """The roles whose board page the app's code reads now: `listed`, while the owner reads the board, and still fresh
    enough to be worth a page (the same margin the sourcing chase keeps, `wanted`: a role the chase leaves is not
    worth a page either). Empty with the board reading off."""
    if not registry.follow_board_on((await profile_store.current(session, owner)).settings):
        return []
    return await wanted(session, owner, listed(owner), order_by=order_by,
                        base=Opportunity.status == stages.FOLLOWUP_NEEDED, columns=columns, held=held)
