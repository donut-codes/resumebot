"""Bringing in the files the owner already has: the tracker spreadsheet and the employer list. The command line
(`jobsearch/cli.py`: `import-tracker`, `import-employer-list`) and the owner door (`owner_actions/onboarding.py`:
`tracker.import`, `employers.import`) both call these two functions, so the order of the steps and the numbers they
answer are written once (docs/PRD.md 50.4). They write through the session and never commit; the caller commits, then
sends the knocks a started catch-up asks for. A refusal is the step's own `DomainError`, in words."""

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch.doors import tools
from app.jobsearch.employers import core as employers
from app.jobsearch.employers import employer_list
from app.jobsearch.employers import pairs as employer_pairs
from app.jobsearch.imports import tracker
from app.jobsearch.missions import catch_up
from app.jobsearch.models import Employer


@dataclass
class TrackerBrought:
    """What reading a tracker came to: its counts (`tracker.import_tracker`'s, and the duplicate employers' beside
    them), what went wrong beside it and did not stop it, the catch-up it started (None when there was nothing to
    research, and on a dry run) and, on a dry run, how many roles it would research."""

    counts: dict
    notes: list[str] = field(default_factory=list)
    started: catch_up.Started | None = None
    would_research: int | None = None


async def tracker_file(session: AsyncSession, owner: str, source, *, dry_run: bool = False) -> TrackerBrought:
    """Read the tracker `source` (a path or a file object) into the app: its roles, the employers settled, the
    employer records that are one employer merged (`employer_pairs.settle`; no other pair is asked about), and then
    the catch-up started over what the tracker brought in and nobody researched (docs/PRD.md 2.9). The catch-up comes
    last and keeps the ids of the employers whose job system it looks for: a
    merge after it would leave out the record that stays. It is started only if it can start, and the caller saves
    nothing when it cannot (`CatchUpError`), so running it again after a fix starts it.

    With `dry_run` the same reading is done and the catch-up is not started: `would_research` says how many roles it
    would research. The caller rolls the session back."""
    counts = await tracker.import_tracker(session, owner, source)
    await employers.settle(session, owner)
    notes: list[str] = []
    # The import stands whatever the duplicate check finds (docs/PRD.md 36.5).
    try:
        async with session.begin_nested():
            duplicates = await employer_pairs.settle(session, owner)
        counts |= {f"employers {k}": v for k, v in duplicates.items()}
    except Exception as exc:
        notes.append(f"the employers were not checked for duplicates ({type(exc).__name__})")
    if dry_run:
        wanted = await catch_up.plan(session, owner, ordered=False)
        return TrackerBrought(counts, notes, would_research=len(wanted.picked))
    return TrackerBrought(counts, notes, started=await catch_up.start(session, owner))


@dataclass
class ListBrought:
    """What an employer list file came to: the rows read (`employer_list.Read`), how they meet the app's employers
    (`employer_list.Matches`) and how many employers wait for the scout to find their job board."""

    read: employer_list.Read
    found: employer_list.Matches
    waiting: int


async def employer_list_file(session: AsyncSession, owner: str, name: str, text: str, *,
                             dry_run: bool = False) -> ListBrought:
    """Keep the names and websites of the owner's employer list (`text`, a CSV with a heading row; `name` is the file's
    name) as the scout's lookup, in place of an earlier load of a file of that name, and say how the list meets the
    app's employers. A file with no usable row is refused with the counts, and nothing is changed. With `dry_run`
    nothing is saved. It makes no employer and researches nothing (`employer_list.py`)."""
    read = employer_list.read(text)
    if not read.entries:
        raise employer_list.ListError(
            f"no usable row in the file ({read.rows} rows read: {read.dropped} marked drop, {read.unusable} with no "
            "usable name or website); nothing was changed")
    mine = (await session.scalars(select(Employer).where(Employer.owner == owner))).all()
    waiting = set(await tools.boards_waiting(session, owner, None))
    found = employer_list.matches(read.entries, mine, waiting)
    if not dry_run:
        await employer_list.replace(session, owner, name, read.entries)
    return ListBrought(read, found, len(waiting))
