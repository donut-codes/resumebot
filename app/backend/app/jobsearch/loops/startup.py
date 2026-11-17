"""What the app does as it starts (`main.lifespan`): the log set up, and the fix-ups to the data every start runs.

Only what must run at every start is here: filling in what a release added (the roster, the employers, the sites, the
missions, the process files) and settling what an interrupted run left. A repair of what an earlier release left behind,
done once, is a migration (`migrations/versions`), never a step here."""

import gc
import logging

from process_engine import versions as proc_versions
from process_engine.engine import keep_files
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.agents import roster, shipped_rule
from app.jobsearch import process
from app.jobsearch.browser import go as apply_go
from app.jobsearch.browser import queue as browser_queue
from app.jobsearch.employers import core as employers
from app.jobsearch.fetch import host_stops
from app.jobsearch.fit import classifier
from app.jobsearch.missions import core as missions
from app.jobsearch.owner_actions import runner as owner_runner
from app.jobsearch.pipeline import stored_order
from app.jobsearch.postings import same_role
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import keep_current, settle
from app.jobsearch.work import instruction_files, stuck
from app.jobsearch.writing import seats
from app.log_setup import configure_logging as configure_logging
from app.sites import process as site_process
from app.sites import where

log = logging.getLogger(__name__)


def tune_gc_threshold() -> None:
    """Make full garbage collections rare. With a large heap each full collection stops the one event loop for
    a noticeable time, and the default third number (10) runs many of them. 1000 makes them rare, at
    the cost of cyclic garbage that outlived two young collections waiting longer to be freed."""
    gc.set_threshold(700, 10, 1000)


async def build_lookups(owner: str) -> None:
    """Build what the work asks for first, before any work starts: the index of the owner's roles and the classifier.
    Each is made on a reader's session (no writer's turn) and kept in memory, so the first pass of the night finds it
    kept and holds no turn while it builds (the roles index is seconds of the loop for a large account). A lookup that
    fails is logged with its traceback and the engine starts without it: the pass that needs it builds it itself."""
    lookups = (
        ("the roles index", same_role.known_roles),
        ("the classifier", classifier.train),
    )
    for name, build in lookups:
        try:
            async with db.read_session_factory()() as reader:
                await build(reader, owner)
        except Exception:
            log.exception("start-up lookup %s failed; the engine starts without it", name)


async def run_startup_fixups(session: AsyncSession, owner: str) -> None:
    """What every start does to the data before the app serves: fills in what a release added and settles what an
    earlier release left. Each step is safe to run again (tests/test_startup.py runs the start twice).

    Each step is a transaction of its own (committed when it is done, so the writer's turn is let go between steps), and
    not a savepoint: inside a savepoint a step cannot end its transaction (`db.can_release`), and the start held the
    file's one turn for the whole pass, so every other writer waited. Here a
    step that works in batches (the stored order, the browser's queue) commits between them. A step that fails is rolled
    back to where it last committed and logged with its traceback, and the start goes on without it, so a bad row never
    turns into a crash loop and one step's database error never poisons the next (each step is safe to run again, so
    what it committed before it failed stands)."""
    steps = [
        ("the roster", lambda: roster.ensure_roster(session, owner)),
        # A new install's Rules for ATS screening start with four plain starter rules, saved once as version 1 of the
        # writing rules; an install that has any version of them gets nothing (docs/PRD.md 33.6).
        ("the starter writing rules", lambda: profile_store.ensure_starter_writing(session, owner)),
        # Every role has its employer record (docs/PRD.md 21.2).
        ("employer records", lambda: employers.settle(session, owner)),
        # Each employer with an address has a site, and the ones on a known job system have their reader as a skill
        # (docs/PRD.md 29.1). The switches are the owner's: this fills in what is missing, and settles the watch of
        # an employer with a role in play when the owner asked for that (29.9; it does nothing while that is off).
        ("employer sites", lambda: where.seed_employer_sites(session, owner)),
        # A release that ships a different instruction or template file than the one the owner changed becomes the next
        # version, with theirs kept in the history; nothing is asked (docs/PRD.md 16.1).
        ("shipped instruction files", lambda: instruction_files.apply_shipped(session, owner)),
        # The expectation of a researched role is worked out again when the method that made it has changed (every
        # release that changes `comp.METHOD_VERSION`), and a role whose expectation moved is settled again: its score
        # and its gates read it. This one stays, since it runs again for each new method.
        ("old research asks", lambda: settle.old_asks(session, owner)),
        # Stops judged under an older Hard pass than the one in force (or none), and scores worked out under an older
        # Prioritization (or none), are judged and worked out again: a save that changes nothing makes no new version,
        # so a release's stops and scores would otherwise wait for the owner to change the setting (docs/PRD.md 39.14,
        # 39.21, 39.23). Nothing to do once every stop and score is current; the night's read does the same.
        ("stale stops and scores", lambda: keep_current.run(session, owner)),
        # What the lanes of the board are read by (`stored_order`, docs/PRD.md 24.19) is worked out again for every role
        # and written where it differs: the roles a release found with nothing stored (migration 0151), a release that
        # changed a rule of the number, and a write no trigger saw.
        ("the stored order", lambda: stored_order.settle(session, owner, everything=True)),
        # The standing work is missions; any missing are made (docs/PRD.md 18.5).
        ("the missions", lambda: missions.ensure(session, owner)),
        # A board read lives in the app's own process, so one marked running before this start was cut short by it.
        ("interrupted reads", lambda: missions.settle_after_restart(session, owner)),
        # A job of the owner's action door lives in the app's own process, so one marked running was cut short by it.
        ("interrupted action jobs", lambda: owner_runner.fail_interrupted(session, owner)),
        # A stop of the app ends the lanes' sessions with no word of the end: the seat runs still open are noted, and
        # each gives its seat back once its runner is heard again, not when its lease is out (docs/PRD.md 43.6).
        ("seat runs a stop left open", lambda: seats.note_restart(session, owner)),
        # Every stopped domain is loaded for the fetcher; a refusal or bot check is a stop for good (docs/PRD.md path
        # 28), at the board until the owner's next off hours start (48.14 d).
        ("stopped hosts", lambda: host_stops.load(session, owner)),
        # The browser's queue of small items, seeded from the roles and sites as they stand:
        # source items for the roles to source, read items for the postings to read, scan and map items for the sites.
        ("the browser work queue", lambda: browser_queue.refresh(session, owner)),
        # The owner's saved versions of the process files are handed to the engine; a shipped file is kept as version
        # 1 the first time, and a release that changed one is followed (or the owner is asked, when their own version
        # is in force): docs/PRD.md path 16.2.
        ("the process files' versions", lambda: proc_versions.refresh(session)),
        ("the shipped process rule", lambda: shipped_rule.settle(session)),
        # The shipped process files are kept by their text, so a run started before a release finishes on its own
        # file after the release changes it (the vendored process-engine).
        ("the process files' texts", lambda: keep_files(session, process.library())),
        # A run whose own file can no longer load (a release removed a step or an instruction file it names) is moved
        # onto the current file, at its step or the nearest one, instead of failing when it is answered.
        ("runs on files that no longer load", lambda: process.engine().move_stranded(session)),
        ("site project runs on files that no longer load", lambda: site_process.engine().move_stranded(session)),
        # A role's analysis or an employer's research still waiting while nothing holds it (what earlier releases
        # and migrations left) is cancelled, so no run waits on work no one will hand out.
        ("orphaned research runs", lambda: stuck.cancel_orphans(session, owner)),
        # Launch in Browser runs in the engine's own process and the page reads its row (`js_go_runs`): a run the last
        # stop cut short is closed as stopped, and one the page asked for that nothing began is begun. Last, so a run
        # it begins finds everything above settled.
        ("interrupted Launch in Browser runs", lambda: apply_go.settle_runs(session, owner)),
    ]
    for name, step in steps:
        try:
            await step()
            await session.commit()
        except Exception:
            log.exception("start-up step %s failed; the app starts without it", name)
            await session.rollback()
