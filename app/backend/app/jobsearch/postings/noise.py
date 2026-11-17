"""A role the app's code could not get the original posting of is closed as noise (the owner, 2026-11-13: "yes,
code-only for followup needed. If we can't get it by code, we close it as noise").

A role in Followup needed is the backlog: it has no text, so nothing ranks it. Its `source` item is the app's code's
alone (`BrowserItem.code_only`, `browser/queue.py`: no Claude session is handed it). When the code tried it and could
not get the posting (no address, an address the app refuses, a page it cannot read, not a whole posting, a sign-in
wall) and the item has used its tries, the role is closed here: Gone Quiet with the reason `noise`, a history line that
says why, and the role kept with that reason on the Pipeline like any closed role (every role is kept, with a reason).

Never closed: a role the owner starred, asked to read first or to research, opened in Go or skipped there, moved by
hand or asked a packet of, one that has a packet, one of the owner's own data (their tracker's, or added by their link),
and any role not in Followup needed. A board role whose board page the app's code is still to read waits for that
read (its copy is the other way to get a text); the read's last try closes it. A rest or a stop of the host never closes
a role: that is the item waiting, not the code having tried.

Services flush; the caller commits."""

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import stages
from app.jobsearch.actors import CODE_CHECKER
from app.jobsearch.models import (
    OWN_ORIGINS,
    BrowserItem,
    JobPosting,
    Opportunity,
    Packet,
    board_link_unread_posting,
    board_unread_posting,
    is_on_followed_board,
    is_read_first,
)
from app.jobsearch.profile import store as profile_store
from app.jobsearch.states import BrowserKind, BrowserState
from app.sites import registry

EVENT = "noise_closed"
SENTENCE = "Closed as noise: the original posting could not be read by code."
#: The reason's words on the role's history line, which the owner reads.
REASON = "Noise: the original posting could not be read by code"


async def _owner_holds(session: AsyncSession, role: Opportunity) -> bool:
    """Whether the role is one the owner has a hand in, so the app never closes it as noise: a research asked for, a
    hand move or a packet asked for, or a packet."""
    from app.jobsearch.pipeline import quiet  # it reads the postings' modules; imported here, never at the top
    from app.jobsearch.research import line  # analysis reads the browser's modules

    if is_read_first(role) or role.go_opened_at is not None or role.go_skip or role.origin in OWN_ORIGINS:
        return True
    if await session.scalar(select(Opportunity.id).where(Opportunity.id == role.id, line.owners_asks())):
        return True
    if await quiet.hand_since(session, role.owner, [role.id]):
        return True
    return await session.scalar(select(Packet.id).where(Packet.role_id == role.id).limit(1)) is not None


async def _board_page_pending(session: AsyncSession, role: Opportunity) -> bool:
    """Whether a board role's board page is still to be read by the app's code, the other way to a text for it
    (the board's copy hook): a `listing` item that waits or is held, or an unread board posting of the role
    with no `listing` item given up on yet. False for a role not found on the board or while the owner does not read
    the board."""
    from app.jobsearch.postings import raw_copies

    board = registry.board_reader("guest")  # it reads postings
    if board is None or not is_on_followed_board(role):
        return False
    if not registry.follow_board_on((await profile_store.current(session, role.owner)).settings):
        return False
    states = select(BrowserItem.state).where(
        BrowserItem.role_id == role.id, BrowserItem.kind == BrowserKind.LISTING.value)
    seen = set((await session.scalars(states)).all())
    if seen & {BrowserState.WAITING.value, BrowserState.LEASED.value}:
        return True
    if seen & {BrowserState.STUCK.value, BrowserState.DONE.value}:
        return False
    return bool(await session.scalar(select(exists().where(
        JobPosting.opportunity_id == role.id,
        (board_unread_posting(board.source_id(role.owner), raw_copies.board_page())
         | board_link_unread_posting(raw_copies.board_page()))))))


async def close(session: AsyncSession, role: Opportunity, why: str, *, by_code: bool = True,
                link: str | None = None) -> bool:
    """Close a role in Followup needed as noise: its original posting could not be read by code (`why`, in a few words:
    what the code found). Returns whether it was closed; a role the owner has a hand in, or whose board page is yet
    to be read, is left as it is."""
    from app.jobsearch.pipeline import quiet  # it reads the postings' modules; imported here, never at the top
    from app.jobsearch.research import line

    if role.status != stages.FOLLOWUP_NEEDED or await _owner_holds(session, role):
        return False
    if await _board_page_pending(session, role):
        return False
    await line.leave(session, role, line.ROLE_CLOSED)  # research that was done stays done; one in line leaves the line
    role.browser_note = None
    words = (why or "").strip().rstrip(".")
    detail = f"{REASON}{f' ({words})' if words else ''}."
    if link:
        detail += f" The link the app tried: {link}"
    await quiet.end_role(session, role.owner, role, reason=stages.NOISE, event=EVENT, sentence=SENTENCE, detail=detail,
                         who=CODE_CHECKER if by_code else None)
    return True

