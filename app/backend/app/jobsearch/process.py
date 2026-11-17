"""The job-search processes, loaded and ready to run: `playbooks/jobsearch/*.bpmn` in a library of their own, bound
to the job-search actions (the engine's default registry; `load_actions` imports the modules that register them).

The engine is the `process_engine` package (app/packages/process-engine). This module configures it once, on import:
the roles an agent step may name (`scout`, `analyst`, `writer`, `coach` and `reviewer`), the instruction and template
files (the real ones in `instructions/` and `templates/`, read through `app.harness.instructions`, so a file that names
one that isn't there fails to load, here and in the tests), and the key of an instance's data that holds its owner.
Everything that runs the engine imports this module first.

The owner's versions of these files (docs/PRD.md path 16.2) are the engine's own (`process_engine.versions`, the
table `proc_versions`): every instance here belongs to the one owner (`owner_candidates`), so `Engine.start` takes
the newest version that loads from the database, and a run already going finishes on the file it started with.
Version 1 is the file in `playbooks/jobsearch/` as it shipped, kept at start (`app.agents.edit_process.reconcile`).
The job-search actions are in the engine's default registry, the one `versions.definition` checks an edited file
against.
"""

import copy
import functools
import logging
from functools import lru_cache
from pathlib import Path

from process_engine import config as engine_config
from process_engine import versions as proc_versions
from process_engine.engine import Engine
from process_engine.errors import ActionError
from process_engine.loader import ProcessLibrary
from process_engine.models import RUNNING, WAITING, ProcInstance
from process_engine.registry import Catalog, default_catalog
from process_engine.registry import actions as registry

from app import db, owner_keys
from app.config import settings as base_settings
from app.harness import instructions as harness_instructions
from app.jobsearch.actors import actor_name
from app.settings import settings

log = logging.getLogger(__name__)

ROUND = "jobsearch_round"
SCREEN = "jobsearch_screen_role"
SCOUT_STEP = "Scout_searches"
ROLE = "scout"
#: The last step of the search round (docs/PRD.md 25.13): the analyst researching the round's own finds when Analyse the
#: finds is on. The round waits there until none of them is with the analyst (`analysis.settle_rounds`).
ROUND_ANALYST_STEP = "Analyst_researches_finds"
PACKET = "jobsearch_packet"
#: The story library's build: its agent steps are seats of the writer's kind
#: (`writing/library.py`), as the packet's are (`packet_writer.py`).
LIBRARY = "jobsearch_library"
#: One part of a library build (the resume lines with the owner's rules, or the letter blocks), called once per part
#: from the build so the parts are written side by side (`build_library_part.bpmn`).
LIBRARY_PART = "jobsearch_library_part"
WRITER = "writer"
ANALYSE = "jobsearch_analyse"
ANALYST_STEP = "Analyst_researches"
ANALYST = "analyst"
READ_BOARDS = "jobsearch_read_boards"
#: Find Gaps (docs/PRD.md path 38): what the postings for a role ask that the master resume does not show. The agent's
#: step is the analyst's role with its own instruction file.
FIND_GAPS = "jobsearch_find_gaps"
FINDER_STEP = "Analyst_finds"
#: The employer researcher (docs/PRD.md path 36): one employer researched. The agent's step is the analyst's role with
#: its own instruction file, as Find Gaps is.
RESEARCH_EMPLOYER = "jobsearch_research_employer"
RESEARCHER_STEP = "Researcher_researches"
#: Read the mail (docs/PRD.md path 14, `mail/sync.py`): the owner's mail read read-only, classed by rules and a small
#: session, matched to roles and acted on. The agent's step is the analyst's role with its own instruction file.
READ_MAIL = "jobsearch_read_mail"
MAIL_STEP = "Mail_reader_reads"
#: Launch in Browser (Go, docs/PRD.md path 18, `browser/go.py`): the day's Ready to Launch roles checked, their packets
#: built one after another and each role opened as a tab. Every step is code.
GO = "jobsearch_go"
#: Interview prep (docs/PRD.md 15.2): one interview prepared. The Coach writes the report (step `Coach_writes`, role
#: `coach`), a second session reads it as the interviewer (step `Reviewer_reads`, role `reviewer`); both are sessions of
#: the coach kind.
PREPARE_INTERVIEW = "jobsearch_prepare_interview"
COACH_STEP = "Coach_writes"
REVIEW_STEP = "Reviewer_reads"
#: Working through a Find Gaps run for a job with the Coach (docs/PRD.md 38.13 to 38.19, `gaps/coach.py`): the Coach
#: writes the questions for its gaps, or proposes master lines from the owner's answers; a session of the coach kind, in
#: its own mode with its own instruction file.
CLARIFY_GAPS = "jobsearch_clarify_gaps"
CLARIFY_STEP = "Coach_clarifies"
#: The catch-up after a tracker import (docs/PRD.md 2.9, `catch_up.py`): every role the import brought in that is still
#: in play, researched at once.
CATCH_UP = "jobsearch_catch_up"

#: The roles an agent step may name: this app's scout, analyst, writer, coach and reviewer.
ROLES = frozenset({ROLE, ANALYST, WRITER, "coach", "reviewer"})


async def _drive_failed(session, entity_id, key, what, exc, subject=None) -> None:
    """A live attempt's work failed and was rolled back: the run is held where it was. A job-search process's failure is
    logged where the run's own error shows it to the owner; a site project's round (`playbooks/sites/`) also becomes an
    Inbox task with Try again (`app.sites.failures`)."""
    from app.sites import failures  # the sites library reuses this module's configuration

    if failures.is_site_process(key):
        log.warning("site process %s held after a failed %s (%s): %s", key, what, subject, exc)
        await failures.held(session, entity_id, key, what, exc, subject)
        return
    log.warning("process %s held after a failed %s (%s): %s", key, what, subject, exc)


async def _drive_worked(session, entity_id, key, what, subject=None) -> None:
    """A live attempt worked. Only a site project's round has anything to settle: its Inbox task about a failure closes
    by itself. Nothing is done for a job-search process."""
    from app.sites import failures

    if failures.is_site_process(key):
        await failures.worked(session, entity_id, key, what, subject)


def process_dir() -> Path:
    return Path(base_settings.playbooks_path) / "jobsearch"


def _owner_candidates(data, subject) -> list[str]:
    """Every instance in this app is the one owner's: the id their versions of the process files are kept under."""
    return [str(owner_keys.entity_id())]


def load_actions() -> None:
    """Import the modules that register the actions the shipped files name (`actions`, and the analysis, employer
    research, interview prep, Find Gaps, packet, story library and catch-up processes' own steps). They import this
    module, so it cannot import them
    at its top."""
    from app.jobsearch import actions  # noqa: F401
    from app.jobsearch.browser import go_actions  # noqa: F401  (registers only: loads no runner)
    from app.jobsearch.gaps import coach as gap_coach  # noqa: F401
    from app.jobsearch.gaps import runs as find_gaps  # noqa: F401
    from app.jobsearch.interviews import coach  # noqa: F401
    from app.jobsearch.mail import sync as mail_sync  # noqa: F401
    from app.jobsearch.missions import catch_up  # noqa: F401
    from app.jobsearch.pipeline import favorites  # noqa: F401
    from app.jobsearch.research import analysis  # noqa: F401
    from app.jobsearch.research import employer as employer_research  # noqa: F401
    from app.jobsearch.writing import library as story_library  # noqa: F401
    from app.jobsearch.writing import (
        packet_writer,  # noqa: F401
        packets,  # noqa: F401
    )


async def _member_names(session, entity_id) -> dict:
    """Who saved a version: the one owner, by name."""
    return {owner_keys.member_id(): await actor_name(session, settings.owner)}


def configure_engine() -> None:
    """Tell the process engine what only this app knows. Safe to call again."""
    engine_config.configure(
        roles=ROLES,
        instructions=harness_instructions,  # `names(kind)` and `short_name(kind, ref)`: the module is the source
        owner_key="owner",
        owner_candidates=_owner_candidates,
        shipped_dir=process_dir,
        register_processes=load_actions,
        member_names=_member_names,
        shipped_label="The file as it first shipped.",
        on_drive_failure=_drive_failed,
        on_drive_success=_drive_worked,
    )


configure_engine()


def catalog() -> Catalog:
    return default_catalog()


def build_library() -> ProcessLibrary:
    load_actions()
    files = sorted(process_dir().glob("*.bpmn"))
    library = ProcessLibrary()
    cat = catalog()
    for path in files:
        library.load(path.read_text(encoding="utf-8"), catalog=cat, registry=registry)
    library.validate()
    return library


@lru_cache(maxsize=1)
def library() -> ProcessLibrary:
    return build_library()


class AppEngine(Engine):
    """The engine as the app runs it. Each call that moves an instance (`start`, `complete`, a message, a timer, a
    cancel) runs its steps' actions inside the caller's transaction, with the instance's row locked and its token
    between two places: inside one the transaction must stay whole, so `db.release` refuses there (`db.holding`). A step
    that reads the network takes what its caller read before the call (`polite.read_ahead`)."""


#: The calls that begin a run. Each reads the owner's saved versions of the process files first
#: (`process_engine.versions.current_for`): the page program saves a version and tells no one, so the engine program
#: looks before it starts work. One small read when nothing was saved since the last look; the files are parsed again
#: only when something was. A run already going is not touched: it finishes on the file it started with (its
#: `file_hash`). A sub-process a run calls starts on what the library held at the last of these calls.
READS_VERSIONS_FIRST = frozenset({"start", "start_by_message"})


def _kept_whole(name: str):
    base = getattr(Engine, name)
    fresh = name in READS_VERSIONS_FIRST

    @functools.wraps(base)
    async def call(self, session, *args, **kwargs):
        if fresh:
            # In the caller's session, before the engine's call: a start is a write, so the turn is the caller's
            # already, and the read sees a version the caller itself has just saved.
            await proc_versions.current_for(session, owner_keys.entity_id())
        with db.holding("a call of the process engine"):
            return await base(self, session, *args, **kwargs)

    return call


for _name in ("start", "start_by_message", "complete", "deliver_message", "tick", "fire_timer", "cancel",
              "move_stranded"):
    setattr(AppEngine, _name, _kept_whole(_name))


def engine() -> Engine:
    """A live engine (no hand-out hook: the scout finds its open step through `jobs.profile`)."""
    return AppEngine(library(), registry, catalog=catalog())


#: The job-search steps that read the network and run after an agent's answer (an employer's careers pages and the
#: open feeds, after the researcher's report). A `reading_engine` stops at each instead of running it inside its own
#: call, and the caller runs it with `run_deferred`, with no transaction open while it reads.
READS_NETWORK = frozenset({"jobs.employer_board", "mail.act"})


def reading_engine() -> Engine:
    """A live engine that stops at the steps that read the network (`READS_NETWORK`) instead of running them inside its
    call, which holds the caller's transaction (and the instance's row) for as long as the far side takes. The caller
    runs them with `run_deferred`. The process file still decides which step comes next and what each is given."""
    return AppEngine(library(), registry, catalog=catalog(), deferred=READS_NETWORK)


async def run_deferred(session, moving: Engine, instance_id, *, leave_on_failure: bool = False) -> ProcInstance:
    """Run the steps `moving` (a `reading_engine`) stopped at for this instance, one after another, until it waits at
    none: before each, `session` is released (`db.release`; the instance waits at the step, so what is committed is
    whole), the step's action runs outside the engine's call (so it may release the session again while it reads), and
    its outcome goes back with `complete`, which runs the steps after it as usual. A named error (`ActionError`) goes to
    the engine as one, the step's writes rolled back. Any other failure rolls the step back and is raised, the process
    marked failed, or with `leave_on_failure` left waiting at the step for the next pass to run again. Returns the
    instance."""
    while True:
        where = await moving.where_is(session, instance_id)
        waits = [w for w in where["waiting_for"] if w["kind"] == "deferred"]
        if where["status"] not in (RUNNING, WAITING) or not waits:
            done = await session.get(ProcInstance, instance_id)
            if done is None:
                raise RuntimeError("the process is gone")
            return done
        step, name = waits[0]["step"], waits[0]["payload"]["action"]
        action = moving.registry.get(name)
        if action is None:
            raise RuntimeError(f"the step '{step}' has no action to run")
        await db.release(session)
        instance = await session.get(ProcInstance, instance_id)
        if instance is None:
            raise RuntimeError("the process is gone")
        try:
            result = await action.fn(session, instance, copy.deepcopy(instance.data or {}))
        except ActionError as error:
            await session.rollback()
            await moving.complete(session, instance_id, step, error=error.code, error_message=error.message)
            continue
        except Exception as exc:
            await session.rollback()
            if not leave_on_failure:
                await moving.complete(session, instance_id, step, error="step_failed",
                                      error_message=f"{type(exc).__name__}: {exc}"[:300])
                await session.commit()
            raise
        await moving.complete(session, instance_id, step, result)


def stepped_engine() -> Engine:
    """A live engine that stops at every service step instead of running it (the engine's `deferred` steps, here for
    every action there is), for a process whose driver runs each step in a transaction of its own and hands the outcome
    back with `complete` (the night's read, `boards_night.py`). The engine still reads the file: which step comes next,
    and the data each is given, are the process's. What it never does is run a step inside one session, which would hold
    every row the steps wrote until the last of them ended."""
    loaded = library()  # loading it registers the actions, so the names are all there
    return AppEngine(loaded, registry, catalog=catalog(), deferred=frozenset(registry.names()))
