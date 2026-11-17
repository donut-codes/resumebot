"""A favorite role (the owner, 2026-10-23): a star the owner sets or clears on any role, kept as `favorited_at`.

A favorite is fast-tracked, through the same steps every role goes through, and it runs both impressions before it
takes a place at the top (the owner, 2026-10-28):

1. **Its posting, its rank, then its research, each at the front of its line.** A favorite that is not Sourced has its
   sourcing put first in the browser's queue (`browser_reader.board_roles`), after what the app already holds is tried.
   A favorite with no rank is asked for a rank
   first in the Haiku line (`rank.ask_first`), and once it has one (or has no text to rank) the owner's research ask
   is made for it (`analysis.ask`): ahead of every role nobody asked for. The star makes the first ask at once and the
   pipeline's turn the next (`advance`), so the research follows the rank without waiting for anyone. The code gates
   run as for every role (`pipeline.gate` at Cleansed Data, the settle step after the research): a favorite a hard
   pass stops goes to Gated out with its reason.
2. **A place in Ready to Launch, once it is read.** A favorite that has a Second Impression that graded its posting
   (`analysis.graded_read`; one that graded nothing is asked for its research again and waits) is put
   in Ready to Launch (`to_ready`) after its gates are checked again, and holds its place while it is starred
   (`placement.starred`): the best-of queue (`pipeline.rebalance`) counts it toward the owner's daily number and never
   gives its place away, and the stage rule (`lifecycle.stage_of`) leaves it there, as it does a role the owner moved by
   hand or one Launch opened (`placement` holds all of them). A favorite not read yet holds no place. It holds the place
   until it is applied, closed (a gate stops it at any stage, with its reason, as for every role) or unstarred;
   unstarred, it competes on its score again. One that is read already goes up at once, as soon as it is starred.
3. **Its packet.** Once it is read (at once, when it is read already) its packet is asked (`packets.start`, by
   `actors.BY_FAVORITE`: the app's ask on the owner's star, so the last gate check runs before the writer drafts). The
   read's own process asks it: the step "Ask a favorite's packet" of `analyse_role.bpmn` (`action_favorite_packet`),
   which puts the role in Ready to Launch first.

A role that is applied to, in talks or closed can be starred too; it keeps its stage and its reason, and nothing more is
asked for it. Every step is a line in the role's history. Services flush; the route commits.

**A move to Ready to Launch asked before the read** (the owner, 2026-11-03, docs/PRD.md 24.13): no role sits in Ready
to Launch without a Second Impression that graded its posting (`analysis.graded_read`), so when the owner moves such a
role there by hand the move is not made yet (`board.move`). The role is asked what it lacks, as a star is (`ask_reads`,
`advance`: `Opportunity.ready_asked_at` is the mark), and once it has a read that graded something the owner's move is
made (`place_asked`) and it holds its place as any role the owner placed does. The owner moving it anywhere else
withdraws the ask."""

import uuid
from datetime import UTC, datetime

from process_engine.registry import actions as registry
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import DomainError
from app.jobsearch import stages
from app.jobsearch.actors import BY_FAVORITE, actor_name
from app.jobsearch.gating import stated_place
from app.jobsearch.models import Opportunity, Packet, ParseJob, RoleRank, read_first
from app.jobsearch.pipeline import placement
from app.jobsearch.postings import cleansed
from app.jobsearch.postings import sourced as sourcing
from app.jobsearch.research import analysis, line, rank, staleness
from app.jobsearch.states import PacketState, ParseKind, ParseState
from app.jobsearch.writing import packets
from app.trail import service as trail

#: The statuses a favorite is fast-tracked from: before the application.
FAST_TRACKED = stages.PRE_APPLY
#: A packet in one of these is one the role has, or will have: no other is asked.
LIVE_PACKETS = (PacketState.WRITING, PacketState.READY, PacketState.HELD)


class NoSuchRole(DomainError):
    status = 404


def is_favorite(role: Opportunity) -> bool:
    return role.favorited_at is not None


async def _has_live_packet(session: AsyncSession, role_id: uuid.UUID) -> bool:
    return await session.scalar(select(Packet.id).where(
        Packet.role_id == role_id, Packet.status.in_(LIVE_PACKETS)).limit(1)) is not None


async def ask_packet(session: AsyncSession, owner: str, role: Opportunity) -> Packet | None:
    """Ask a read favorite's packet, unless it has one (written, being written or held) or is past the application.
    A packet the writer cannot be asked for (no master resume, an unnamed employer, a gate the last check now fails)
    is a line in the role's history, never an error: the role keeps its place and its reason. Returns the packet."""
    if not placement.starred(role, analysis.graded_read(role)) or await _has_live_packet(session, role.id):
        return None
    try:
        return await packets.start(session, owner, role.id, by=BY_FAVORITE)
    except packets.PacketError as exc:
        await trail.record(session, owner, event="favorite_packet_not_asked", actor="app", who=trail.APP_WHO,
                           link=("opportunity", role.id),
                           sentence=f"{role.title} at {role.company} is your favorite, but its packet was not asked: "
                                    f"{exc}")
        await session.flush()
        return None


async def to_ready(session: AsyncSession, owner: str, role: Opportunity) -> bool:
    """Put a read favorite before Ready to Launch there. Returns whether it moved."""
    if role.status not in stages.PRE_READY:
        return False
    was, role.status = role.status, stages.READY_TO_LAUNCH
    role.ready_asked_at = None  # the place the owner asked for is taken (a star and a move can both be on a role)
    await trail.record(session, owner, event="favorite_ready", actor="app", who=trail.APP_WHO,
                       link=("opportunity", role.id),
                       sentence=f"{role.title} at {role.company} is your favorite and is read: it moved from "
                                f"{stages.LABELS.get(was, was)} to Ready to Launch and holds its place there until it "
                                "is applied, closed or unstarred.")
    return True


async def place_asked(session: AsyncSession, owner: str, role: Opportunity) -> bool:
    """Make the move the owner asked for before the role was read (`board.move`, `Opportunity.ready_asked_at`): it is
    read now, so it goes to Ready to Launch, written as the owner's own move (a `placement.MOVED_EVENT` line, in the
    transaction that changes the status), so it holds its place as any role the owner placed does. Returns whether it
    moved. The caller has checked that the role has a read that graded its posting."""
    if role.ready_asked_at is None or role.status not in stages.PRE_READY:
        return False
    was, role.status = role.status, stages.READY_TO_LAUNCH
    role.ready_asked_at = None
    await trail.record(session, owner, event=placement.MOVED_EVENT, actor="person",
                       who=await actor_name(session, owner), link=("opportunity", role.id),
                       sentence=f"You moved {role.title} at {role.company} from {stages.LABELS.get(was, was)} to Ready "
                                "to Launch.",
                       detail="You asked for this move before the role was read. It is read now, so it is made.")
    return True


async def needs_rank(session: AsyncSession, owner: str, role: Opportunity) -> bool:
    """Whether the role's First Impression is still to do: it has a current cleansed copy with real text and no Haiku
    rank. A role with no text to rank is read by the analyst, who finds the posting."""
    copy = await cleansed.copy_of(session, owner, role.id)
    if copy is None or len(copy.text_clean or "") < cleansed.MIN_TEXT:
        return False
    return await session.scalar(select(RoleRank.id).where(
        RoleRank.owner == owner, RoleRank.role_id == role.id).limit(1)) is None


async def _rank_is_open(session: AsyncSession, owner: str, role: Opportunity) -> bool:
    """Whether a rank job that holds the role is open (waiting for a session, or with one): its research waits."""
    for (fields,) in (await session.execute(select(ParseJob.fields).where(
            ParseJob.owner == owner, ParseJob.kind == ParseKind.RANK,
            ParseJob.state.in_((ParseState.WAITING, ParseState.READING))))).all():
        if str(role.id) in {str(i["role_id"]) for i in (fields or {}).get("items", [])}:
            return True
    return False


async def get_read(session: AsyncSession, owner: str, role: Opportunity, done: dict) -> None:
    """Put an unread favorite at the front of what it still lacks, and say what was asked in `done` (`source`, `rank`,
    `research`): its posting first (`sourcing.source_stored` now, else the browser's queue), then its rank
    (`rank.ask_first`), then, once it has one, the owner's research ask (`analysis.ask`). A role in the analyst's line
    that the owner has not asked for goes first now; one taken by a session or asked for already is left to finish.
    One whose research failed or waits for the posting's text is left for the owner or the text (asking again every
    turn would try it again every turn). A role whose rank is asked for and not in yet is waited for."""
    if role.analysis_state in (analysis.FAILED, analysis.NO_TEXT):
        return
    if role.sourced_at is None and role.analysis_state != analysis.WAITING:
        # Its posting first (the owner, 2026-10-28): what the app already holds sources it now, else its sourcing is
        # first in the browser's queue (`browser_reader.board_roles`), and the rank and the research follow.
        if await sourcing.source_stored(session, role) != sourcing.SOURCED:
            done["source"] = True
            return
    if role.analysis_state == analysis.WAITING:
        if line.Standing.of(role) is line.Standing.FREE and not line.asked_by_owner(role):
            done["research"] = await analysis.ask(session, owner, role.id)
        return
    if await needs_rank(session, owner, role):
        done["rank"] = await rank.ask_first(session, owner, role)
        if done["rank"] or await _rank_is_open(session, owner, role):
            return
    done["research"] = await analysis.ask(session, owner, role.id)


async def advance(session: AsyncSession, owner: str) -> int:
    """The pipeline's turn for the favorites, and the roles the owner asked for in Ready to Launch (`board.move`), that
    are not read yet (the owner, 2026-10-28): each is asked what it still
    lacks (`get_read`), so the research follows the rank without waiting for anyone, and a favorite with research
    waiting is put first in the line. One whose rank or research is asked for already is asked nothing again. Returns
    how many research asks were made."""
    roles = (await session.scalars(select(Opportunity).where(
        Opportunity.owner == owner, read_first(), Opportunity.status.in_(FAST_TRACKED))
        .order_by(Opportunity.id).execution_options(populate_existing=True))).all()
    asked = 0
    for role in roles:
        if analysis.counts(role):
            continue
        done: dict = {"source": False, "rank": False, "research": None}
        await get_read(session, owner, role, done)
        asked += bool(done["research"] and not done["research"]["already"])
    return asked


async def ask_reads(session: AsyncSession, owner: str, role: Opportunity, done: dict | None = None) -> dict:
    """Put a role that is not read, or whose read graded nothing of its posting, at the front of what it still lacks
    (`get_read`), as a star does, and say what was asked (`done`: `source`, `rank`, `research`; a new one when none is
    passed). A role read already whose read graded nothing is asked for its research again: the posting may be graded
    this time. The browser's queue is refreshed when its sourcing is first in it."""
    if done is None:
        done = {"moved": False, "source": False, "rank": False, "research": None, "packet": None}
    if analysis.counts(role):
        done["research"] = await analysis.ask(session, owner, role.id)
        return done
    await get_read(session, owner, role, done)
    if done["source"]:  # its sourcing is first in the browser's queue now, not at the queue's next look
        from app.jobsearch.browser import queue as browser_queue  # the browser's queue reads the pool, which reads this

        await browser_queue.refresh(session, owner)
    return done


async def fast_track(session: AsyncSession, owner: str, role: Opportunity) -> dict:
    """The fast track of a favorite (the module's three steps), as the star starts it. A role that is read, with its
    research made under today's inputs, has its gates checked again and goes up to Ready to Launch at once, its packet
    asked; any other is put at the front of what it still lacks (`get_read`) and is not moved. Returns what it did:
    `moved`, `source` (its sourcing is first in the browser's queue), `rank` (a rank job was made), `research` (what
    `analysis.ask` said, or None) and `packet` (the packet's id, or None)."""
    done: dict = {"moved": False, "source": False, "rank": False, "research": None, "packet": None}
    if role.status not in FAST_TRACKED:
        return done
    if not analysis.counts(role):
        return await ask_reads(session, owner, role, done)
    if await staleness.analysis_reasons(session, role):  # read under inputs that have changed: read again first
        done["research"] = await analysis.ask(session, owner, role.id)
        return done
    if not analysis.graded_read(role):  # read, and graded nothing of the posting: no place in Ready to Launch on that
        return await ask_reads(session, owner, role, done)
    if role.status in stages.PRE_READY:
        checked = await stated_place.recheck(session, owner, role, cause="You starred it")
        if checked == stated_place.GATED:  # a hard pass stops it: Gated out with its reason, as any role
            return done
        if checked == stated_place.SENT_BACK:  # the analyst is asked the place first: it is not read now
            done["research"] = {"already": False, "role": str(role.id)}
            return done
    done["moved"] = await to_ready(session, owner, role)
    await session.flush()
    packet = await ask_packet(session, owner, role)
    done["packet"] = str(packet.id) if packet is not None else None
    return done


async def set_favorite(session: AsyncSession, owner: str, role_id: uuid.UUID, on: bool, *,
                       defer_fast_track: bool = False) -> tuple[Opportunity, dict]:
    """Star (`on`) or unstar a role. Starring a role that is a favorite already, or unstarring one that is not, changes
    nothing. A starred role is fast-tracked (`fast_track`); an unstarred one keeps its stage and everything asked for
    it, and from the next turn of the pipeline competes on its score again. Returns the role and what was done:
    `favorite`, `changed`, `moved`, `source`, `rank`, `research` and `packet`. With `defer_fast_track` the star and its
    line are saved and the fast track is left to the caller (the page's route asks the engine, the ask
    `role.fast_track`): the keys after `changed` then hold their start values."""
    role = await session.scalar(select(Opportunity).where(Opportunity.id == role_id, Opportunity.owner == owner)
                                .execution_options(populate_existing=True))
    if role is None:
        raise NoSuchRole("no such role")
    done: dict = {"favorite": on, "changed": on != is_favorite(role), "moved": False, "source": False,
                  "rank": False, "research": None, "packet": None}
    if not done["changed"]:
        return role, done
    name = f"{role.title} at {role.company}"
    role.favorited_at = datetime.now(UTC) if on else None
    if on:
        sentence = (f"You starred {name} as a favorite: it runs its First and Second Impression first, then it holds "
                    "a Ready to Launch place and its packet is asked." if role.status in FAST_TRACKED else
                    f"You starred {name} as a favorite; it stays in {stages.LABELS.get(role.status, role.status)}.")
    else:
        sentence = f"You unstarred {name}: it competes for its place on its score again."
    await trail.record(session, owner, event="favorite_set" if on else "favorite_cleared", actor="person",
                       who=await actor_name(session, owner), link=("opportunity", role.id), sentence=sentence)
    await session.flush()
    if on and not defer_fast_track:
        done.update(await fast_track(session, owner, role))
    return role, done


async def action_favorite_packet(session, instance, data):
    """The step "Ask a favorite's packet" of `analyse_role.bpmn`: a favorite that the read just settled, and that is
    still before the application, goes up to Ready to Launch and has its packet asked (`ask_packet`). Any other role
    passes through."""
    role = await session.get(Opportunity, uuid.UUID(data["role"]))
    if role is None:
        return {"asked": False}
    packet = await ask_packet(session, role.owner, role)
    if placement.starred(role, analysis.graded_read(role)):  # read now, and not stopped by the packet's last gate check
        await to_ready(session, role.owner, role)
    return {"asked": packet is not None}


registry.register(
    "jobs.analysis_favorite_packet", action_favorite_packet, inputs=["role"], outputs=["asked"],
    description="Puts a favorite the read just settled in Ready to Launch and asks its packet, when it is still before "
                "the application and has no packet; any other role passes through.",
    source="backend/app/jobsearch/pipeline/favorites.py")
