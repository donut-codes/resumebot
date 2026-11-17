"""A role keeps getting older after the gates have passed it (docs/PRD.md 40.6, the owner, 2026-11-04).

The owner's pass on how long ago a role was posted (the freshness dial's Must, "the days since it was posted is within N
days", and the older posting-age gate, "posted over N days ago") changes with the day and with nothing else: no write
says a role has crossed it, so no writer posts a to-do for it. It was applied only
when something else ran the gates over the role: a save of Hard pass (the re-gate), a start that found stops judged
under an older one, the turn's gate step for a role at Cleansed Data, and the settle of a role whose facts changed.
Without this look a role in Ready to Launch that goes past the Must days would stay there, and a role in Followup
needed, which no turn gates, would sit until a save of Hard pass reaches it.

`run` is the look for one day. It is the hour tick's, once for each day of the owner's (`handlers.age_out`), and it asks
two questions of what is stored, in this order:

1. **Which roles fail the pass now.** One read of the roles before the application (their own `posted` and `found`, no
   text) and one of their postings' dates and the time the app first saw each (`freshness.posted_by_role`), which gives
   each role the one posted day the whole app counts from (`freshness.posted_basis`). The days since that day are
   judged by the same comparison the gate check makes (`gating.results`: `scoring.dial_gate` for the dial, the days of a
   posting-age gate), on the lines that apply to the role's own target role (`GateSet.lines_for`). With no such line
   set, nothing else is read.
2. **Whether each may be stopped.** The roles that fail are given to the re-gate's own check for one role
   (`regate.one_role`): the owner's hand placement is honoured as it is there, a role in Second Impression or Ready to
   Launch is stopped as the re-gate stops it (a Ready role keeps its packet), and the first gate that fails (cheapest
   first) is the one the role is stopped on, with the gate's usual sentence and the line in its history, so the day's
   stops read exactly as the re-gate's. Roles at Applied or later are not read.

A role that is no longer as it was read when its slice is locked (another transaction moved it meanwhile:
`as_read.lock` skips it) is left and counted in `Aged.left`: the caller looks again at the next hour. There is no row
lock to wait for: a slice's transaction holds the one writer turn, and a look that begins while another has it waits.
Every slice is committed as it goes, so the owner's moves of a role are never held up by more than a slice. A role that
is stopped here posts the turn's to-dos through the work queue's hook as any stop does
(the deck refills the place it leaves).

Other passes that depend on the day: the posting-age gate counts the same days and is covered. An employer fact read
against today's date (`years_since_founded`, `years_since_layoff`, `layoffs_in_2_years`: `employers.facts`) counts
years and months of the employer's record, not the days since a role was posted, so it is not covered here: it moves at
most monthly and the next re-gate or research settles it. Services flush; the caller commits what is left open."""

import uuid
from dataclasses import dataclass
from datetime import date
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.jobsearch import as_read, off_turn, stages
from app.jobsearch.employers import follow as employer_posting
from app.jobsearch.fit import freshness, scoring
from app.jobsearch.gating import check as gating
from app.jobsearch.gating import regate
from app.jobsearch.measures.pay_place import age_in_days
from app.jobsearch.models import Opportunity
from app.jobsearch.pipeline import placement
from app.jobsearch.postings import cleansed
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.gates import Gate
from app.timeutil import owner_today

#: What a role's history says began the check (`regate_stopped`: "<cause>, so the gates were checked again.").
CAUSE = "A new day began"
#: How many roles one transaction stops: a slice is locked until it commits, and each role's check is some milliseconds
#: of reads and writes (a big slice holds the turn too long; the target is under 1 s; with the packets, stops and copies
#: read once for a slice, a small one holds it briefly).
CHUNK = 8
#: How many roles' postings one read of their dates takes (the rows are decoded on the loop between slices).
SLICE = 500


@dataclass(frozen=True)
class Aged:
    """What a look did: how many roles it read, how many it stopped, and how many it could not take up (another
    transaction held them: the next look tries them again)."""

    checked: int = 0
    closed: int = 0
    left: int = 0


def is_age_line(line: Gate) -> bool:
    """Whether a gate of the owner's is a pass on how long ago a role was posted: the freshness dial's Must, or a
    posting-age gate."""
    return (line.kind == "posting_age" and bool(line.days)) or (line.kind == "job_fact" and line.fact == "posted_days")


def fails(line: Gate, days: int) -> bool:
    """Whether a role posted `days` days ago fails this pass, as `gating.results` judges it."""
    if line.kind == "posting_age":
        return days > (line.days or 0)
    return scoring.dial_gate(line, {"posted_days": days})[0] == gating.FAIL


async def run(session: AsyncSession, owner: str, *, today: date | None = None, cause: str = CAUSE) -> Aged:
    """Stop every role before the application whose days since it was posted now fail the owner's pass, as the re-gate
    would stop it. `today` is the owner's day the days are counted to (the dispatcher's clock, in a test). Nothing is
    read of the roles while no such pass is set."""
    today = today or owner_today()
    # The whole read (the profile, every role before the application with its posting dates, and the rules for the roles
    # that fail) is a reader's, which takes no writer turn: it would hold the turn for seconds at every hour tick that
    # looked. The turn is taken for each slice's write alone.
    async with off_turn.reading(session) as reader:
        profile = await profile_store.current(reader, owner)
        if not any(is_age_line(line) for line in profile.gates.other):
            return Aged()
        roles = (await reader.execute(select(
            Opportunity.id, Opportunity.posted, Opportunity.found, Opportunity.board_url, Opportunity.target_role
        ).where(Opportunity.owner == owner, Opportunity.status.in_(stages.PRE_APPLY)))).all()
        boards = await employer_posting.job_board_hosts(reader, owner)
        posted: dict[uuid.UUID, freshness.PostedDay] = {}
        for start in range(0, len(roles), SLICE):
            part = roles[start:start + SLICE]
            held = await freshness.held_postings(reader, owner, [r.id for r in part], boards)
            posted.update(await freshness.posted_by_role(
                reader, owner, cast("list[freshness.Dated]", part), held, boards))
        lines: dict[str | None, list[Gate]] = {}
        over: list[uuid.UUID] = []
        for role in roles:
            if role.target_role not in lines:
                lines[role.target_role] = [line for line in profile.gates.lines_for(role.target_role)
                                           if is_age_line(line)]
            mine = lines[role.target_role]
            day = posted[role.id]
            days = age_in_days(day.posted, day.found, today)
            if days is not None and any(fails(line, days) for line in mine):
                over.append(role.id)
        checked = len(roles)
        rules = await gating.rules(reader, owner, profile, today=today) if over else None
    await db.release_if_free(session)  # where the read was made in `session` itself (it holds a transaction)
    if rules is None:
        return Aged(checked)
    over.sort()
    closed = left = 0
    for start in range(0, len(over), CHUNK):
        held = await as_read.lock(
            session, Opportunity, over[start:start + CHUNK], defer=(Opportunity.description,),
            where=(Opportunity.owner == owner, Opportunity.status.in_(stages.PRE_APPLY)))
        left += len(held.skipped)
        # The slice's standing and posted days are read once here, under the turn, from the rows as the lock found them
        # (the reader pass's days are stale by now); `one_role` would read them again for each role.
        rows = list(held.rows.values())
        places = await placement.load(session, owner, rows, placement.Purpose.GATES)
        days = await freshness.posted_by_role(session, owner, rows)
        ids = [row.id for row in rows]
        packets = await regate._latest_packets(session, ids)
        lefts = await regate._left(session, [row.id for row in rows if row.status == stages.GATED_OUT])
        copies = await cleansed.gate_copies_of(session, owner, ids)
        for row in rows:
            if await regate.one_role(session, owner, row, cause=cause, rules=rules, posted=days[row.id],
                                     standing=places[row.id], packet=packets.get(row.id), left=lefts.get(row.id),
                                     copy=copies.get(row.id)) and row.status == stages.GATED_OUT:
                closed += 1
        await db.release_if_free(session)
    return Aged(checked, closed, left)
