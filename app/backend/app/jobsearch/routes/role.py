"""The roles and the board over HTTP: the Pipeline's columns, one role whole, adding a job by its link, moving a role by
hand, asking research of a role and correcting a requirement's class
(docs/PRD.md paths 7, 11, 24.10, 26, 30 and 39.28).

Each service's refusal answers with its own status (`app.errors`): a role there is none of is 404, a move that cannot be
made is 409, a class that is not P, T or U is 422."""

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.db import ReadSession, Session
from app.errors import Busy, DomainError
from app.jobsearch import off_turn
from app.jobsearch.browser import go_status, link_held, link_text
from app.jobsearch.models import Opportunity
from app.jobsearch.pipeline import board, ended, favorites, live, search
from app.jobsearch.pipeline import page as roles
from app.jobsearch.pipeline import row as role_row
from app.jobsearch.research import analysis, role_summary
from app.jobsearch.work import engine_calls
from app.settings import settings

router = APIRouter(prefix="/api/v1")

#: How many seconds the page waits for the engine to do what an owner's click on a role leaves to it (the research it
#: asks, the settle of a corrected class, the reads a move to Ready to Launch or a star asks): the same twenty as
#: the owner-action wait. Past it the page answers what it saved, and the ask stays for the engine.
ROLE_WAIT_S = 20


class _Refused(DomainError):
    """A refusal the engine gave to an ask, said again with the status and words it had."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class AddRole(BaseModel):
    #: The link to the job's page, as the owner pasted it (`link_text.read_link` checks it and says what is wrong).
    url: str


class Move(BaseModel):
    to: str = Field(max_length=20)
    note: str | None = None
    #: Why the role ended, for a move to an Ended stage; worked out when empty. The move box offers nine
    #: (`stages.OWNER_END_REASONS`) and needs none; the API takes any reason in `stages.END_REASONS`, `no_reply`
    #: and `unknown` too.
    ended_reason: str | None = Field(default=None, max_length=20)


class Favorite(BaseModel):
    #: True to star the role as a favorite, false to unstar it.
    favorite: bool


class ClassBody(BaseModel):
    index: int
    cls: str = Field(alias="class")
    why: str = ""


@router.get("/board")
async def board_view(session: ReadSession, column: str | None = None,
                     role: Annotated[str | None, Query(max_length=80)] = None,
                     limit: Annotated[int | None, Query(ge=1, le=5000)] = None,
                     reason: Annotated[str | None, Query(max_length=40)] = None,
                     page: Annotated[int | None, Query(ge=1)] = None,
                     size: Annotated[int, Query(ge=1, le=200)] = role_row.PAGE_SIZE) -> dict:
    """Every column of the board with its first cards (`role_row.columns`); `column` for one column whole; `role` (a
    target role's id) for only the roles found for that target role; `limit` how many cards Added links (the postings
    waiting for the title check) holds; `reason` with column `closed`, the closed roles under that Closed reason;
    `page` (from 1) with `column`, one page of `size` cards of that column, with the column's `page` and `pages`."""
    return {"columns": await role_row.columns(session, settings.owner, column, target_role=role, reason=reason,
                                              page=page, size=size, added_links=limit),
            "ended_reasons": ended.offered()}


@router.get("/favorites")
async def favorites_view(session: ReadSession) -> dict:
    """Every starred role, whatever its stage, as one column of cards (`role_row.favorites`): the Favorites."""
    return dict(await role_row.favorites(session, settings.owner))


@router.post("/apply/go")
async def open_apply_batch(request: Request, session: Session) -> dict:
    """Press Go: ask for the day's Launch in Browser run. The page writes the owner's `GoRun` row in phase `asked`
    (`go_status.ask`), commits, and rings the engine, which checks the day's Ready to Launch roles, builds their
    packets and opens each role in the app's Chrome as its packet is done (`browser/go.py`). This answers at once with
    the row (`phase`, `total`, `built`, `opened`, `skipped`, `notes`). A run that goes already is answered as it
    stands. The app runs on the user's computer; it refuses anyone who is not on this machine. With the engine stopped
    the row stays `asked` and Go begins when the engine runs."""
    if not go_status.from_this_machine(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="Only the app on this computer can open your browser.")
    went, ring = await go_status.ask(session, settings.owner)
    await session.commit()
    if ring:  # after the commit: the engine reads the row, so the row is there before the bell
        engine_calls.start("go", settings.owner)
    return went


@router.get("/apply/go")
async def apply_go_progress() -> dict:
    """Where Go stands now, or how its last run ended: the page asks while the progress bar is up."""
    return await go_status.stored(settings.owner)


@router.post("/roles")
async def add_role(body: AddRole, session: Session) -> dict:
    """The owner adds a job by pasting its link (docs/PRD.md 24.10): a posting that waits in Added links and goes the
    way a scouted posting goes. A job the owner already has answers with where it stands (`role` for a role, `posting`
    for a posting that is no role yet) and `already`, and nothing is made. A text that is not a public web address is
    refused with 422, here, before the engine is asked. The owner never waits for the engine (2026-11-16): a job the
    owner already has is answered at once with where it stands and `already`; a new link has its posting written here
    (`link_held.make_posting`, database work only), so it is in Added links before the answer, and the engine is asked
    (`engine_asks`, `role.add_link`) with no wait to put it through the gates, or to do what pasting the link of a role
    does. In the whole app the ask runs on the spot and its answer is returned as it is; in the page program the answer
    is what the page knows, and a stopped engine loses nothing, the ask being kept for when it runs."""
    key, address = link_text.read_link(body.url)
    args: dict = {"url": body.url}
    quick = await link_held.known(session, settings.owner, key, address)
    if quick is None:  # a new link: its posting is written here, so it shows in Added links before the engine runs
        posting, made = await link_held.make_posting(session, settings.owner, key, address)
        quick = link_held.posting_answer(posting, already=not made)
        if made:
            args["posting"] = str(posting.id)
    answer = await engine_calls.ask(session, settings.owner, "role.add_link", args, wait_s=0)
    if answer is None:
        return quick
    if "refused" in answer:
        raise link_text.LinkError(answer["refused"])
    return answer


async def _row_and_counts(session: Session, role_id: uuid.UUID) -> dict:
    """The role's row whole (`role_row.row`) and the board's counts after the change (`live.counts`): what the page
    needs to show a move at once, without reading the board again. Read on a reader once the change is committed: the
    writer's turn is for writing."""
    async with off_turn.reading(session) as reader:
        return {**await role_row.row(reader, settings.owner, role_id),
                "counts": await live.counts(reader, settings.owner)}


async def _save_move(session: Session, role_id: uuid.UUID, done: dict) -> None:
    """Commit what a move saved. A move to Ready to Launch of a role not yet read is held (`done["asked"]`,
    `board.move`): the engine is then asked to put the role at the front of what it still lacks (`role.read_first`,
    which also wakes the analyst and asks the dispatcher for a pass), and this waits `ROLE_WAIT_S` seconds for it. With
    no answer in time the move is answered as it is saved, and the ask is kept for the engine. Any other move asks
    nothing."""
    if done["asked"]:
        await engine_calls.ask(session, settings.owner, "role.read_first", {"role_id": str(role_id)}, ROLE_WAIT_S)
    else:
        await session.commit()


@router.post("/roles/{role_id}/move")
async def move_role(role_id: uuid.UUID, body: Move, session: Session) -> dict:
    """Move a role by hand. Answers with the role's row as its new column shows it, and `counts`: every lane's and
    Closed reason's count after the move. A move to Ready to Launch of a role with no Second Impression that graded its
    posting is not made (`board.move`): the role is read first, as a star is, the engine is asked for the reads (the ask
    `role.read_first`), and the row says so."""
    done = await board.move(session, settings.owner, role_id, body.to, body.note, body.ended_reason, defer_reads=True)
    await _save_move(session, role_id, done)
    return await _row_and_counts(session, role_id)


@router.post("/roles/{role_id}/move-back")
async def move_role_back(role_id: uuid.UUID, session: Session) -> dict:
    """Move a closed role back to where it was before it closed (the Closed review's button). Answers as a move does:
    the role's row and `counts`."""
    done = await board.move_back(session, settings.owner, role_id, defer_reads=True)
    await _save_move(session, role_id, done)
    return await _row_and_counts(session, role_id)


@router.put("/roles/{role_id}/favorite")
async def favorite_role(role_id: uuid.UUID, body: Favorite, session: Session) -> dict:
    """Star or unstar a role (`favorites.py`). A starred role before the application runs its impressions first (the
    owner, 2026-10-28): its sourcing is put first in the browser's queue when it is not Sourced, then it is put at the
    front of the Haiku line for its rank when it has none, then of the analyst's line for its research, and it goes up
    to Ready to Launch once it is read (at once when it is read already, its packet asked). The page saves the star and
    its line in the role's history; the engine does the rest (the ask `role.fast_track`, which also wakes the scout and
    asks the dispatcher for a pass) and this waits `ROLE_WAIT_S` seconds for it. Answers with the role's row, `counts`,
    and what was done: `favorite`, `changed`, `moved`, `source` (its sourcing is first in the browser's queue), `rank`
    (a rank job was made), `research` (what the research ask said, or null) and `packet` (the packet asked, or null).
    With no answer in time the star is answered as saved, with nothing launched; the ask is kept for the engine. Nothing
    is asked of the engine for an unstar, for a star that changes nothing, or for a role past the application."""
    role, done = await favorites.set_favorite(session, settings.owner, role_id, body.favorite, defer_fast_track=True)
    if done["changed"] and body.favorite and role.status in favorites.FAST_TRACKED:
        asked = await engine_calls.ask(session, settings.owner, "role.fast_track", {"role_id": str(role_id)},
                                       ROLE_WAIT_S)
        done.update((asked or {}).get("done") or {})
    else:
        await session.commit()
    async with off_turn.reading(session) as reader:  # read apart from the writer's turn, once the star is committed
        row = await role_row.row(reader, settings.owner, role_id)
        counts = await live.counts(reader, settings.owner)
    return {**done, "role": row, "counts": counts}


@router.get("/search")
async def search_all(session: ReadSession, q: str = Query(default="", max_length=200),
                     page: int = Query(default=1, ge=1),
                     size: int = Query(default=search.PAGE, ge=1, le=search.MOST_PER_PAGE)) -> dict:
    """One search across every role, whatever its status (Closed and Gated out with their reasons), and the postings
    that are no role yet: every word of `q` in the title, employer, place, status or reason. Roles first, then postings,
    the newest first, a page of `size` at a time (`search.py`)."""
    return await search.find(session, settings.owner, q, page, size)


@router.get("/live")
async def live_updates(request: Request, topics: str | None = Query(default=None, max_length=200)) -> StreamingResponse:
    """The owner's live updates, as server-sent events (`live.py`): `hello`, then `role`, `counts`, `packet`,
    `summary`, `task` and `notification` as changes are committed, and `resync` when the page fell too far behind.
    `topics` (comma-separated message names) asks for only those; without it every message is sent."""
    try:
        wanted = live.parse_topics(topics)
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    return StreamingResponse(live.stream(settings.owner, request.is_disconnected, topics=wanted),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/roles/{role_id}/research")
async def research_role(role_id: uuid.UUID, session: Session) -> dict:
    """The owner asks for the research of a role at any stage, Ended and Gated out included (docs/PRD.md 39.28). It is
    queued for the analyst ahead of the roles nobody asked for; a role researched already is researched again; one
    already in the line answers that it is queued. A role past Ready keeps its stage. The engine does it
    (`engine_asks`, `role.research`: the research starts by reading the network, which the page never does) and wakes
    the analyst; this waits `ROLE_WAIT_S` seconds for its answer. With no answer in time (the engine is stopped or busy)
    the owner is told the worker is not running, 503; the ask is kept, and the research is queued when the engine
    runs."""
    answer = await engine_calls.ask(session, settings.owner, "role.research", {"role_id": str(role_id)}, ROLE_WAIT_S)
    if answer is None:
        raise Busy(engine_calls.WORKER_NOT_RUNNING)
    if "refused" in answer:
        raise _Refused(answer["refused"]["status"], answer["refused"]["message"])
    return answer["asked"]


@router.post("/roles/{role_id}/summary")
async def ask_summary(role_id: uuid.UUID, session: Session) -> dict:
    """The owner opened a researched role with no summary: a summary alone is asked for it (`role_summary.ask`), and
    the scout is woken. Nothing is asked for a role that has one, is asked already, or is not researched."""
    asked = await role_summary.ask(session, settings.owner, role_id)
    await session.commit()
    if asked["asked"]:
        await engine_calls.knock(str(role_id), reason=engine_calls.ANALYSIS_ASKED)
    return asked


@router.post("/roles/{role_id}/requirements")
async def correct_requirement(role_id: uuid.UUID, body: ClassBody, session: Session) -> dict:
    """The owner corrects a required item's class (docs/PRD.md 26.2); the score and the expectation follow. The page
    saves the correction and its line in the role's history, then asks the engine to settle the role again (the ask
    `role.settle`: the expectation, the score, the gates) and waits `ROLE_WAIT_S` seconds. The answer is the research as
    it stands after, with `said` (the line the gates wrote) when the engine answered; with the engine stopped or slow it
    is the research with the correction, and the settle is made when the engine runs."""
    await analysis.correct_class(session, settings.owner, role_id, body.index, body.cls, body.why, defer_settle=True)
    answer = await engine_calls.ask(session, settings.owner, "role.settle",
                                    {"role_id": str(role_id), "cause": analysis.CORRECTED_CAUSE}, ROLE_WAIT_S)
    async with off_turn.reading(session) as reader:  # the role as it is now, read apart from the writer's turn
        role = await reader.get(Opportunity, role_id)
        shown = (analysis.view(role) if role is not None else None) or {}
    if said := (answer or {}).get("said"):
        shown["said"] = said
    return shown


@router.get("/roles/{role_id}")
async def role_detail(role_id: uuid.UUID, session: ReadSession) -> dict:
    """One role whole: its facts, its history with the reasoning behind each event, and its packets."""
    return await roles.detail(session, settings.owner, role_id)


@router.get("/roles/{role_id}/row")
async def role_row_of(role_id: uuid.UUID, session: ReadSession) -> dict:
    """One role's row (`role_row.row`) and nothing else: what its column shows, its note (there only for what is wrong
    or waits on the owner, docs/PRD.md 24.18) included. The page reads it again when the live stream says a role
    changed, so a change costs one row, not the role's whole page (`GET /roles/{id}`, whose top is this same row)."""
    return dict(await role_row.row(session, settings.owner, role_id))
