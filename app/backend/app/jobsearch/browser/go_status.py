"""Where Launch in Browser stands, as the page reads it: the owner's `GoRun` row and nothing else.

The page program serves `GET /api/v1/apply/go` and must not load the runner (`browser/go.py` opens Chrome and is the
engine's). The runner saves the row at each change the owner sees; this module reads it, on a reader, so a read takes
no writer's turn, and writes it in one place: `ask`, the press of the button. It imports nothing of the runner, the
browser or the queue."""

from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.jobsearch.models import GoRun
from app.timeutil import utc_now

#: The addresses Launch in Browser answers a press from: this computer only.
THIS_MACHINE = ("127.0.0.1", "::1", "localhost")


def from_this_machine(host: str | None) -> bool:
    return host in THIS_MACHINE


def fresh() -> dict:
    """A run's status before anything has happened: every key the page reads."""
    return {"phase": "checking", "total": 0, "built": 0, "checked": 0, "check_total": 0, "opened": [],
            "skipped": [], "notes": [], "current": None, "building": [], "stalled": None, "done": False}


async def stored(owner: str) -> dict:
    """What the page reads (`GET /apply/go`): the run's row and nothing else, on a reader, so it takes no turn of the
    writer and is the same answer in whichever program serves it. Idle when no run was ever kept. A run that was going
    when the app stopped reads as it was left until the next start closes it as stopped (`settle_runs`)."""
    async with db.read_session_factory()() as session:
        row = await session.get(GoRun, owner)
    if row is None or not row.state:
        return {**fresh(), "phase": "idle", "done": True}
    return {**fresh(), **row.state}


#: The phase of a run the page asked for and the engine has not begun: `browser/go.py` `settle_runs` begins it.
ASKED = "asked"


async def ask(session: AsyncSession, owner: str) -> tuple[dict, bool]:
    """The page's press of the button (`POST /apply/go`): what to answer, and whether the engine is to be rung. A run
    that has not ended (the row says `done` false) is answered as it stands and takes no turn of the writer; it is
    rung for only when it is still `asked` (a bell lost on the way is asked for again). Otherwise the row is written
    fresh in phase `asked` in the caller's session, which the caller commits, and the engine is to be rung. The engine
    begins an `asked` row when it hears the bell or at its next start (`settle_runs`), so with the engine stopped the
    press leaves an `asked` row and Go begins when the engine runs."""
    seen = await stored(owner)  # on a reader: a press while a run goes takes no turn
    if not seen["done"]:
        return seen, seen["phase"] == ASKED
    row = await session.get(GoRun, owner)  # looked at again under the turn, before it is written
    if row is not None and row.state and not row.state.get("done", False):
        state = {**fresh(), **row.state}
        return state, state["phase"] == ASKED
    state = {**fresh(), "phase": ASKED}
    if row is None:
        session.add(GoRun(owner=owner, state=state))
    else:
        row.state, row.updated_at = state, utc_now()
    await session.flush()
    return state, True
