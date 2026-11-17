"""The writer-by-writer test (the work queue spec E4, a new check the owner approved on 2026-11-02): each
family of writers calls its real entry point, and the to-dos it leaves are the ones its change calls for. It catches a
writer that posts the wrong topic or the wrong key (the tripwire only sees that something was posted), and one that
posts too little for the old loop's passes to find nothing more to do.

"The queue leaves the data where the old passes would have left it" is read as: after the writer, run the old turn
once, and every step of it that changed something is one whose topic a to-do the writer posted names (`TOPIC_OF`). The
to-dos are taken before the old turn runs, so what the turn's own writes post cannot excuse it. Two steps the turn's
counts leave out are asked of the data before it runs, by the predicate their handler goes by (phase 2: `sift` and
`details`, `_owed`): a posting waiting for the filter needs a `posting.sift` to-do, and text owed to a role in play
needs a `role.details` one. Every direct writer of the scan that posts has a row (`FAMILIES`). Everything is made up."""

import ast
import copy
import inspect
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select

from app import db
from app.agents import roster
from app.jobsearch import actions, stages
from app.jobsearch.adapters import _posting
from app.jobsearch.browser import queue as browser_queue
from app.jobsearch.browser import reader as browser_reader
from app.jobsearch.browser.go_checks import _applied as go_applied
from app.jobsearch.employers import not_checked
from app.jobsearch.employers import records as employer_records
from app.jobsearch.gating import regate, sift
from app.jobsearch.imports import tracker
from app.jobsearch.jobs import parse as role_parse
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.missions import search_run
from app.jobsearch.models import (
    BrowserItem,
    CleansedPosting,
    InstructionFile,
    JobPosting,
    Opportunity,
    ParseJob,
    ProfileVersion,
    RawPosting,
    SearchRun,
    Todo,
)
from app.jobsearch.pipeline import board, favorites
from app.jobsearch.pipeline import deck as pipeline
from app.jobsearch.pipeline import from_posting as role_from_posting
from app.jobsearch.pool import core as pool
from app.jobsearch.postings import index as postings_index
from app.jobsearch.postings import raw_copies
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis, rank, staleness
from app.jobsearch.work import claim as claim_work
from app.jobsearch.work import hand_outs, instruction_files, role_match, session_kinds
from app.jobsearch.work.queue import consume, handlers, loop, watch
from app.jobsearch.work.queue import topics as queue_topics
from app.sites import completion
from tests.builders import (
    RESUME,
    analysis_setup,
    browser_world,
    counted,
    criteria,
    findings,
    parse_runner,
    rank_roles,
    run_round,
    stored_by_title,
    tab,
)
from tests.jobsearch.work_queue import scan
from tests.jobsearch.work_queue.support import (
    OWNER,
    blank,
    make_employer,
    make_posting,
    make_role,
    posted,
    without_links,
)
from tests.sites.builders import new_project
from tests.support import (
    DEFAULT_OWNER,
    EXAMPLE_SYSTEM,
    POSTING_WORDS,
    PROFILE,
    load_profile,
    reported,
    tracker_row,
    tracker_workbook,
)

#: The old turn's counts (`deck.run_in_steps`) and the topic of the to-do that tells each step.
TOPIC_OF = {"stopped": "deck.dirty", "made": "posting.roles", "advanced": "role.settle", "gated": "role.settle",
            "starred": "role.settle", "built_out": "role.settle", "board_copy": "role.settle",
            "ready_check": "deck.dirty",
            "employers": "employer.research",
            "marked": "deck.dirty", "held": "employer.limit", "ready": "deck.dirty", "deck": "deck.dirty",
            # asked of the data before the turn (`_owed`), not counted by it
            "sift": "posting.sift", "details": "role.details"}

#: Every writer family: the direct writers of the scan it covers (those that post), by the name of its row.
FAMILIES: dict[str, tuple[str, ...]] = {
    "a text arrives": (),
    "a copy of a posting is made": (),
    "a posting opens again on its board's read": (),
    "a posting opens again by the browser's sighting": (),
    "a role's posting opens again": (),
    "a board read lists a new posting": (),
    "the filter links a posting to a role": (),
    "a gate no longer stops a role": (),
    "a rank is saved": ("app/jobsearch/research/rank.py::keep",),
    "research settles": (),
    "the owner moves a role": (),
    "the owner stars a role": (),
    "an employer link changes": ("app/jobsearch/employers/records.py::_merge",
                                 "app/jobsearch/employers/records.py::_move_postings"),
    "the rules are saved": (),
    "a deleted tab frees its roles": ("app/jobsearch/profile/store.py::release_tabs",),
    "the postings are judged again": ("app/jobsearch/gating/regate.py::_postings",
                                      "app/jobsearch/gating/regate.py::_bulk"),
    "Go opens a role": (),
    "mail says a role was applied to": (),
    "a job's state changes": (),
    "a claim is released": ("app/jobsearch/work/hand_outs.py::release",),
    "a runner goes idle": ("app/agents/roster.py::note_runner",),
    "a kind is resumed": ("app/jobsearch/pool/core.py::resume",),
    "the tracker is imported": ("app/jobsearch/imports/tracker.py::_import_rows",),
    "the scout's report is stored": ("app/jobsearch/actions.py::store", "app/jobsearch/actions.py::dedupe"),
    "an employer's board is no longer read": ("app/jobsearch/employers/not_checked.py::settle",),
    "the browser's queue is made": ("app/jobsearch/browser/queue.py::_apply",),
    "a site project is paused": ("app/sites/completion.py::_stop",),
    "the owner changes a mission's settings": (),
    "a session takes what was queued": (),
    "a job is made for a session": (),
    "a role's stage changes the browser's work": (),
    "a text arrives for a role the browser reads": (),
    "Go could not read a posting": (),
    "the owner adds a job by link": (),
    "a role is made from a listing that has a copy": (),
    "the owner asks a recording": (),
    "a lease is given up": (),
    "a row is taken by a claim": ("app/jobsearch/work/claim.py::claim",),
    "the live roles get the free title match": ("app/jobsearch/work/role_match.py::code_pass",),
}

#: The rows that have a test below.
TESTED: set[str] = set()


def row(family: str):
    """Mark a test as the row of a writer family."""
    def mark(test):
        assert family in FAMILIES, family
        TESTED.add(family)
        return test

    return mark


async def _cleared() -> None:
    async with db.session_factory()() as session:
        await session.execute(delete(Todo))
        await session.commit()


@dataclass(frozen=True)
class Passes:
    """What the three passes of phase 4 would do on the data as it stands, read only: how much work the old pull has to
    start (the roles to rank, the roles and the employers to research: `pool._to_research` and the others, which do not
    count what is queued ahead already), the kind of session the walk would hand out next, and whether the browser
    queue's refresh would change anything."""

    pull: int
    kind: str | None
    refresh: bool


class Told(set):
    """The to-dos a writer left (a set of (topic, key)), and what the passes of phase 4 would do before it wrote and
    after: only a change the writer made is the writer's to tell."""

    def __init__(self, to_dos=(), before: dict[str, Passes] | None = None,
                 after: dict[str, Passes] | None = None) -> None:
        super().__init__(to_dos)
        self.before, self.after = before, after


async def _passes(owner: str) -> Passes:
    """The old passes' verdict on the data as it stands now, in a transaction that is rolled back: the work the pull
    would start is counted from the queue builders' own predicates (the pull itself starts jobs, reaches for the
    posting's page and ends its transaction, none of which belongs in a look), the walk and the refresh are asked as
    they are."""
    from app.jobsearch.research import rank

    async with db.session_factory()() as session:
        try:
            ranks = (await rank.queue(session, owner, dry_run=True))["roles"]
        except rank.RankError:
            ranks = 0
        owed = (ranks + min(await pool._to_research(session, owner), await pool._research_room(session, owner))
                + min(len(await pool._employers_to_research(session, owner)),
                      await pool._employers_room(session, owner)))
        kind = await session_kinds.next_kind(session, owner)
        plan = await browser_queue._plan(session, owner, datetime.now(UTC))
        await session.rollback()
    return Passes(owed, kind, bool(plan and (plan.adds or plan.fills or plan.reopens or plan.drops)))


async def _passes_of_both_owners() -> dict[str, Passes]:
    return {owner: await _passes(owner) for owner in (OWNER, DEFAULT_OWNER)}


async def _told(write) -> Told:
    """Run `write(session)` in a transaction of its own, commit, and return the to-dos it left (those posted before are
    cleared first, so what the setup posted is not counted), with what the passes of phase 4 found before it wrote and
    after. The passes are asked outside the span the to-dos are cleared and read in: a look can write (the refresh's
    first step links the roles that hold their employer's link, the rank's short profile is saved), and what it posts
    is not the writer's."""
    before = await _passes_of_both_owners()
    await _cleared()
    async with db.session_factory()() as session:
        await write(session)
        await session.commit()
    async with db.session_factory()() as session:
        to_dos = await posted(session)
    return Told(to_dos, before, await _passes_of_both_owners())


async def _told_by(call) -> Told:
    """The same for an entry point that makes its own transactions (`call()`)."""
    before = await _passes_of_both_owners()
    await _cleared()
    await call()
    async with db.session_factory()() as session:
        to_dos = await posted(session)
    return Told(to_dos, before, await _passes_of_both_owners())


async def _owed(owner: str) -> dict[str, int]:
    """What the two steps the turn's counts leave out would take now, by the predicate their handler goes by: the
    postings that wait for the filter (`sift.waiting_where`) and the postings of roles in play that lack text
    (`index.details_where`: the old turn read them on its next pass). Read only."""
    async with db.session_factory()() as session:
        owed = {
            "sift": await session.scalar(handlers.owed_statement(sift.waiting_where(owner))),
            "details": await session.scalar(select(func.count()).select_from(
                select(JobPosting.id).join(Opportunity, Opportunity.id == JobPosting.opportunity_id)
                .where(*postings_index.details_where(owner)).subquery())),
        }
        await session.commit()
    return {step: int(n or 0) for step, n in owed.items()}


async def _old_turn_finds_nothing_the_writer_did_not_tell(told: set[tuple[str, str]], owner: str = OWNER, *,
                                                          roles_made_follow: bool = False,
                                                          filtered_follow: bool = False,
                                                          keep_profile: bool = False) -> None:
    """The old passes run once; each step that changed something has a to-do of its topic. With `roles_made_follow`, the
    turn may make roles from the postings the writer told (`posting.roles`), and the later steps that work those new
    roles are not the writer's to tell: the roles' own creation posts their to-dos (the hook), the next pass's. With
    `filtered_follow` the writer told the filter (`posting.sift`) and the same holds for the roles made of the postings
    it lets through: its own writes post `posting.roles` and the roles. What the filter and the read of descriptions
    would take is asked first (`_owed`), and nothing excuses it. The made-up profile is saved first (a no-op when it is
    already the one in force) unless `keep_profile`: a scenario that saved a profile of its own keeps it."""
    if not keep_profile:
        async with db.session_factory()() as session:
            await load_profile(session, owner)
            await session.commit()
    topics = {topic for topic, _key in told}
    owed = await _owed(owner)
    await _the_passes_of_phase_4_find_nothing_the_writer_did_not_tell(told, owner)
    done = await pipeline.run_in_steps(owner)
    untold = {step: n for step, n in done.items() if n and TOPIC_OF.get(step) not in topics}
    followed = (roles_made_follow and TOPIC_OF["made"] in topics) or (filtered_follow and TOPIC_OF["sift"] in topics)
    if followed and done["made"]:
        untold = {}  # `made` is told, and the steps after it work the roles it made
    untold |= {step: n for step, n in owed.items() if n and TOPIC_OF[step] not in topics}
    assert not untold, f"the old turn changed {untold}, which no posted to-do names (posted: {sorted(told)})"


async def _the_passes_of_phase_4_find_nothing_the_writer_did_not_tell(told: set[tuple[str, str]],
                                                                      owner: str) -> None:
    """The pull, the walk and the browser queue's refresh were asked of the data before the writer wrote and after it:
    what each finds that it did not find before is something the writer told (`pool.owed`, `ready.<kind>`,
    `browser.reconcile`). Found in the review of phase 4: the matrix ran only the old turn, so a role made from a
    copied listing, the marks of the deck and a move to Second Impression, all of which the pull reads, were told by
    nothing and no row noticed. A set that did not come from `_told` carries no snapshots and is not asked."""
    before, after = getattr(told, "before", None), getattr(told, "after", None)
    if before is None or after is None:
        return
    was, now, topics = before[owner], after[owner], {topic for topic, _key in told}
    assert now.pull <= was.pull or "pool.owed" in topics, (
        f"the old pull has {now.pull} to start where it had {was.pull}, and no pool.owed was posted ({sorted(told)})")
    assert now.kind is None or now.kind == was.kind or queue_topics.ready(now.kind) in topics, (
        f"the walk would hand out {now.kind} where it handed out {was.kind}, and no ready.{now.kind} was posted "
        f"({sorted(told)})")
    assert not (now.refresh and not was.refresh) or "browser.reconcile" in topics, (
        f"the browser queue's refresh would change something it would not before, and no browser.reconcile was "
        f"posted ({sorted(told)})")


async def _made(*objects) -> list:
    """Save the objects in a transaction of their own (foreign keys are not checked, so a role needs no employer)."""
    async with db.session_factory()() as session:
        await without_links(session)
        session.add_all(objects)
        await session.commit()
    return [o.id for o in objects]


async def _settled() -> None:
    """The old turn has run once on what the scenario's setup made, so the roles are where it leaves a role (a role
    fresh from `make_role` is moved on by it whatever the writer does), and what it changes after the writer is the
    writer's."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()
    await pipeline.run_in_steps(OWNER)


# --- the ordinary saves (the hook) ------------------------------------------------------------------------------------


@row("a text arrives")
async def test_after_a_text_arrives_the_queue_leaves_the_data_where_the_old_passes_would_have_left_it(js):
    role = make_role(description=None)
    posting = make_posting(opportunity_id=role.id, ats="employer")
    role_id, posting_id = await _made(role, posting)

    async def write(session):
        await postings_index.keep_detail(session, await session.get(JobPosting, posting_id), {"text": "the whole text"})

    told = await _told(write)
    assert {("role.settle", str(role_id)), ("role.details", "")} <= told
    await _old_turn_finds_nothing_the_writer_did_not_tell(told)


@row("a copy of a posting is made")
async def test_after_a_copy_of_a_posting_is_made_the_queue_leaves_the_data_where_the_old_passes_would_have_left_it(js):
    role = make_role()
    posting = make_posting(opportunity_id=role.id, ats="employer")
    role_id, posting_id = await _made(role, posting)

    async def write(session):
        await raw_copies._keep_report(session, await session.get(JobPosting, posting_id), {"title": "Lead"},
                                     reader="scout", text="the text of the posting, made a copy of")

    told = await _told(write)
    assert ("role.settle", str(role_id)) in told
    await _old_turn_finds_nothing_the_writer_did_not_tell(told)


async def _a_closed_posting_with_no_role() -> uuid.UUID:
    """A posting the old turn leaves alone while it is closed (no stage works a posting that is not open)."""
    closed = datetime.now(UTC) - timedelta(days=2)
    (posting_id,) = await _made(make_posting(ext_id="1", ats="employer", closed_at=closed))
    return posting_id


@row("a posting opens again on its board's read")
async def test_after_a_board_lists_a_closed_posting_again_the_old_passes_find_nothing_untold(js):
    posting_id = await _a_closed_posting_with_no_role()

    async def write(session):
        posting = await session.get(JobPosting, posting_id)
        listing = SimpleNamespace(postings=[_posting(ext_id="1", title="Lead")], truncated=False, asked=None)
        counts = dict.fromkeys(("seen", "new", "closed", "reopened"), 0)
        await postings_index.store(session, OWNER, listing, source_id=posting.employer_id, ats_name="employer",
                                   company="Fabrikam", now=datetime.now(UTC), counts=counts)
        assert counts["reopened"] == 1

    told = await _told(write)
    assert {("posting.sift", ""), ("posting.roles", "")} <= told
    await _old_turn_finds_nothing_the_writer_did_not_tell(told, roles_made_follow=True)


@row("a posting opens again by the browser's sighting")
async def test_after_the_browser_sees_a_closed_posting_again_the_old_passes_find_nothing_untold(js):
    posting_id = await _a_closed_posting_with_no_role()
    sighting = {"location": None, "work_mode": None, "comp": None, "apply_url": None, "posted_text": "",
                "description": None, "snippet": None, "url": "https://jobs.example.test/1"}

    async def write(session):
        browser_reader._fill(await session.get(JobPosting, posting_id), sighting, datetime.now(UTC), reopen=True)

    told = await _told(write)
    assert {("posting.sift", ""), ("posting.roles", "")} <= told
    await _old_turn_finds_nothing_the_writer_did_not_tell(told, roles_made_follow=True)


@row("a role's posting opens again")
async def test_after_a_board_lists_a_role_s_closed_posting_again_the_old_passes_find_nothing_untold(js):
    """The posting of a role in play comes back open with only a snippet of text: the old turn read it on its next
    pass (`index.fill_role_details`), so a `role.details` to-do is owed."""
    role = make_role(description=None)
    closed = datetime.now(UTC) - timedelta(days=2)
    posting = make_posting(ext_id="1", ats="employer", opportunity_id=role.id, closed_at=closed,
                           filtered_at=closed, text="A snippet.")
    _role_id, posting_id = await _made(role, posting)
    await _settled()

    async def write(session):
        posting = await session.get(JobPosting, posting_id)
        listing = SimpleNamespace(postings=[_posting(ext_id="1", title="Lead")], truncated=False, asked=None)
        counts = dict.fromkeys(("seen", "new", "closed", "reopened"), 0)
        await postings_index.store(session, OWNER, listing, source_id=posting.employer_id, ats_name="employer",
                                   company="Fabrikam", now=datetime.now(UTC), counts=counts)
        assert counts["reopened"] == 1

    told = await _told(write)
    assert ("role.details", "") in told
    assert (await _owed(OWNER))["details"] == 1, "the scenario owes text"
    await _old_turn_finds_nothing_the_writer_did_not_tell(told)


@row("a board read lists a new posting")
async def test_after_a_board_lists_a_new_posting_the_old_passes_find_nothing_untold(js):
    (employer_id,) = await _made(make_employer(name="Fabrikam"))

    async def write(session):
        listing = SimpleNamespace(postings=[_posting(ext_id="1", title="Lead")], truncated=False, asked=None)
        counts = dict.fromkeys(("seen", "new", "closed", "reopened"), 0)
        await postings_index.store(session, OWNER, listing, source_id=employer_id, ats_name="employer",
                                   company="Fabrikam", now=datetime.now(UTC), counts=counts)
        assert counts["new"] == 1

    told = await _told(write)
    assert ("posting.sift", "") in told
    assert (await _owed(OWNER))["sift"] == 1, "the scenario owes a filter"
    await _old_turn_finds_nothing_the_writer_did_not_tell(told, filtered_follow=True)


def _a_role_and_a_posting_of_it(*, routed: bool) -> tuple[Opportunity, JobPosting]:
    """A role in play that lacks its text, and an open posting of the same job with only a snippet that nothing links to
    it yet (`routed`: one the filter has let through, which `make_roles` meets, rather than one nobody has filtered)."""
    key = match_key("Fabrikam", "Lead")
    role = make_role(company="Fabrikam", title="Lead", match_key=key, description=None)
    filtered = {"filtered_at": datetime.now(UTC) - timedelta(days=1), "route": "roles"} if routed else {}
    posting = make_posting(company="Fabrikam", title="Lead", match_key=key, ats="employer", text="A snippet.",
                           **filtered)
    return role, posting


@row("the filter links a posting to a role")
async def test_after_the_filter_links_a_posting_to_a_role_the_old_passes_find_nothing_untold(js):
    role, posting = _a_role_and_a_posting_of_it(routed=False)
    (role_id,) = await _made(role)
    await _settled()  # before the posting: the turn would link it
    (posting_id,) = await _made(posting)

    async def write(session):
        await sift.sift(session, OWNER, await profile_store.current(session, OWNER))

    told = await _told(write)
    async with db.session_factory()() as session:
        assert (await session.get(JobPosting, posting_id)).opportunity_id == role_id, "the filter linked it"
    assert ("role.details", "") in told
    assert (await _owed(OWNER))["details"] == 1, "the scenario owes text"
    await _old_turn_finds_nothing_the_writer_did_not_tell(told)


@row("the filter links a posting to a role")
async def test_after_make_roles_links_a_posting_to_a_role_the_old_passes_find_nothing_untold(js):
    role, posting = _a_role_and_a_posting_of_it(routed=True)
    (role_id,) = await _made(role)
    await _settled()  # before the posting: the turn would link it
    (posting_id,) = await _made(posting)

    async def write(session):
        await role_from_posting.make_roles(session, OWNER)

    told = await _told(write)
    async with db.session_factory()() as session:
        assert (await session.get(JobPosting, posting_id)).opportunity_id == role_id, "the role the owner has"
    assert ("role.details", "") in told
    assert (await _owed(OWNER))["details"] == 1, "the scenario owes text"
    await _old_turn_finds_nothing_the_writer_did_not_tell(told)


@row("a gate no longer stops a role")
async def test_after_a_gate_no_longer_stops_a_role_the_old_passes_find_nothing_untold(js):
    gates = {"location": {"remote": {"on": True}}, "compensation": {"amount": 95_000, "against": "maximum"}}
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates)
        await session.commit()
    role = make_role(status=stages.GATED_OUT, location="Remote", move_type="remote", comp="$190,000-$200,000",
                     screened_out_by="other", screened_out_reason="pay under your amount", origin="scout", fit=3,
                     description="a whole description " * 20)
    posting = make_posting(ext_id="1", ats="employer", opportunity_id=role.id, filtered_at=datetime.now(UTC),
                           text="A snippet.")
    role_id, _posting_id = await _made(role, posting)

    async def write(session):
        report = await regate.run(session, OWNER, cause="test", today=date.today())
        assert report.roles_returned == 1

    told = await _told(write)
    async with db.session_factory()() as session:
        assert (await session.get(Opportunity, role_id)).status == stages.FIRST_IMPRESSION, "the gate let it go"
    assert ("role.details", "") in told
    assert (await _owed(OWNER))["details"] == 1, "the scenario owes text"
    await _old_turn_finds_nothing_the_writer_did_not_tell(told)


@row("a rank is saved")
async def test_after_a_rank_is_saved_the_queue_leaves_the_data_where_the_old_passes_would_have_left_it(js):
    role = make_role(status=stages.FIRST_IMPRESSION)
    raw = RawPosting(id=uuid.uuid4(), owner=OWNER, source_id=uuid.uuid4(), reader="legacy", kind="legacy", ext_id="e1",
                     fetched_at=datetime.now(UTC), body=b"x", body_sha256="a" * 64, body_bytes=1, no_raw=True)
    copy = CleansedPosting(id=uuid.uuid4(), owner=OWNER, raw_id=raw.id, cleaner_version=1)
    job = ParseJob(owner=OWNER, kind="rank", fields={"items": [{"n": 1, "role_id": str(role.id),
                                                                  "cleansed_id": str(copy.id)}],
                                                      "profile_version": 1})
    async with db.session_factory()() as session:
        await without_links(session)
        session.add_all([role, raw])
        await session.flush()
        session.add_all([copy, job])
        await session.commit()
        role_id, job_id = role.id, job.id

    async def write(session):
        await rank.keep(session, OWNER, await session.get(ParseJob, job_id), {1: (80, "a fit")})

    told = await _told(write)
    assert {("role.settle", str(role_id)), ("deck.dirty", "")} <= told
    await _old_turn_finds_nothing_the_writer_did_not_tell(told)


@row("research settles")
async def test_after_research_settles_the_queue_leaves_the_data_where_the_old_passes_would_have_left_it(js):
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        for role in await analysis.picks(session, OWNER, 5):
            await analysis.start(session, OWNER, role)
        await session.commit()

    async def write(session):
        await analysis.submit(session, OWNER, findings(ids["Fabrikam"]))

    told = await _told(write)
    assert {("role.settle", str(ids["Fabrikam"])), ("deck.dirty", "")} <= told
    await _old_turn_finds_nothing_the_writer_did_not_tell(told)


@row("the owner moves a role")
async def test_after_the_owner_moves_a_role_the_queue_leaves_the_data_where_the_old_passes_would_have_left_it(js):
    (role_id,) = await _made(make_role(status=stages.FIRST_IMPRESSION))

    async def write(session):
        await board.move(session, OWNER, role_id, stages.SECOND_IMPRESSION)

    told = await _told(write)
    assert {("role.settle", str(role_id)), ("deck.dirty", ""), ("browser.reconcile", str(role_id))} <= told
    await _old_turn_finds_nothing_the_writer_did_not_tell(told)


@row("the owner stars a role")
async def test_after_the_owner_stars_a_role_the_queue_leaves_the_data_where_the_old_passes_would_have_left_it(js):
    (role_id,) = await _made(make_role(status=stages.SECOND_IMPRESSION))

    async def write(session):
        await favorites.set_favorite(session, OWNER, role_id, True)

    told = await _told(write)
    assert {("role.settle", str(role_id)), ("deck.dirty", ""), ("browser.reconcile", str(role_id))} <= told
    await _old_turn_finds_nothing_the_writer_did_not_tell(told)


@row("an employer link changes")
async def test_after_an_employer_link_changes_the_queue_leaves_the_data_where_the_old_passes_would_have_left_it(js):
    keep, away = make_employer(name="Fabrikam"), make_employer(name="Fabrikam Inc")
    keep_id, away_id = await _made(keep, away)
    await _made(make_role(employer_id=away_id, status=stages.APPLIED, description=POSTING_WORDS))  # built out already

    async def write(session):
        await employer_records.merge(session, OWNER, keep_id, away_id, why="the same employer")

    told = await _told(write)
    assert {("employer.limit", str(keep_id)), ("employer.limit", str(away_id)), ("employer.research", str(keep_id)),
            ("browser.reconcile", "*"), ("deck.dirty", "")} <= told
    await _old_turn_finds_nothing_the_writer_did_not_tell(told)


@row("an employer link changes")
async def test_after_a_merge_folds_a_posting_into_its_twin_the_old_passes_find_nothing_untold(js):
    """Two records read the same posting; the one the role was made from is folded into the other, which takes the
    role (`records._fold`): its text is owed to the role from then on."""
    keep, away = make_employer(name="Fabrikam"), make_employer(name="Fabrikam Inc")
    keep_id, away_id = await _made(keep, away)
    role = make_role(description=None)
    read = datetime.now(UTC) - timedelta(days=1)
    twin = make_posting(employer_id=keep_id, ext_id="1", ats="employer", filtered_at=read, text="A snippet.")
    other = make_posting(employer_id=away_id, ext_id="1", ats="employer", opportunity_id=role.id, filtered_at=read,
                         text="A snippet.")
    role_id, twin_id, _other_id = await _made(role, twin, other)
    await _settled()

    async def write(session):
        await employer_records.merge(session, OWNER, keep_id, away_id, why="the same employer")

    told = await _told(write)
    async with db.session_factory()() as session:
        assert (await session.get(JobPosting, twin_id)).opportunity_id == role_id, "the twin took the role"
    assert ("role.details", "") in told
    assert (await _owed(OWNER))["details"] == 1, "the scenario owes text"
    await _old_turn_finds_nothing_the_writer_did_not_tell(told)


@row("the rules are saved")
async def test_after_the_rules_are_saved_the_queue_leaves_the_data_where_the_old_passes_would_have_left_it(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()

    async def write(session):
        gates = {**PROFILE["gates"], "compensation": {"amount": 80_000, "against": "ask"}}
        await profile_store.save(session, OWNER, "gates", gates, by="test")

    told = await _told(write)
    assert {("role.settle", "*"), ("deck.dirty", "")} <= told, "and the turn is asked to look"
    await _old_turn_finds_nothing_the_writer_did_not_tell(told)


# The scenarios below are not vacuous: each has a role the old turn changes because of the save, so a save that told the
# turn nothing fails them (found in review, 2026-11-02: a saved document posted only `rules.changed`, which no handler
# takes, and the test above had no role for the old turn to change).


async def _a_read_deck_role() -> uuid.UUID:
    """A Second Impression role whose research counts (a Second Impression), cites the risk line of the master resume
    and was made under the versions in force now: `mark_for_reads` leaves it alone until one of them changes."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await profile_store.save(session, OWNER, "resume", RESUME, by="test")
        stamps = await staleness.stamp_analysis(session, OWNER)
        await session.commit()
    cited = [{"item": "Risk", "class": "P", "why": "the master has it", "from": ["b-risk"]}]
    (role_id,) = await _made(make_role(
        status=stages.SECOND_IMPRESSION, **counted(required=cited), analysis_made_under=stamps, fit=80,
        fit_breakdown={"math": 1, "percent": 80, "asks": []}, found=date.today(), posted=date.today(),
        sourced_at=datetime.now(UTC)))
    await _settled()
    async with db.session_factory()() as session:
        assert (await session.get(Opportunity, role_id)).status in (
            stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH), "the read role stays up"
    return role_id


async def _save_a_changed_resume_line(session) -> None:
    resume = copy.deepcopy(RESUME)
    resume["roles"][0]["bullets"][0]["text"] = "Led the risk group"
    await profile_store.save(session, OWNER, "resume", resume, by="test")


async def _save_the_analysts_file(session) -> None:
    text = await instruction_files.read(session, OWNER, "instructions", "jobsearch.analyst")
    await instruction_files.save(session, OWNER, "instructions", "jobsearch.analyst", text + "\nOne more line.", "test")


#: The saves that make a read deck role out of date: what the writer does, by the name of the case.
STALE_MAKING_SAVES = {"a master line the research cites": _save_a_changed_resume_line,
                      "the analyst's instruction file": _save_the_analysts_file}


async def _owes_a_read_again(role_id: uuid.UUID) -> list[str]:
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id, populate_existing=True)
        return await staleness.analysis_reasons(session, role)


async def _marked_to_be_read_again(role_id: uuid.UUID) -> bool:
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id, populate_existing=True)
        return analysis.REANALYSE in (role.analysis or {})


@row("the rules are saved")
@pytest.mark.parametrize("save", STALE_MAKING_SAVES)
async def test_after_a_document_a_read_was_made_under_is_saved_the_old_passes_find_nothing_untold(js, save):
    role_id = await _a_read_deck_role()
    assert not await _owes_a_read_again(role_id) and not await _marked_to_be_read_again(role_id), "it stands"
    told = await _told(STALE_MAKING_SAVES[save])
    assert await _owes_a_read_again(role_id), "the scenario owes a read again"
    assert ("deck.dirty", "") in told
    await _old_turn_finds_nothing_the_writer_did_not_tell(told)
    assert await _marked_to_be_read_again(role_id), "and the old turn did mark it: it is not vacuous"


async def _a_deck_sized_by_the_settings() -> None:
    """Three unread roles and room to read three of them (Settings: `apply_per_day`), settled by one turn: all three are
    marked to be read next."""
    daily = {**PROFILE["settings"], "apply_per_day": 3}
    async with db.session_factory()() as session:
        await without_links(session)  # first: the keys are set for a connection before its transaction
        await load_profile(session, OWNER, settings=daily)
        roles = [make_role(status=stages.FIRST_IMPRESSION, slug=f"unread-{n}") for n in range(3)]
        session.add_all(roles)
        await session.flush()
        await rank_roles(session, {r.id: 90 - 10 * n for n, r in enumerate(roles)})
        await session.commit()
    await pipeline.run_in_steps(OWNER)


async def _marked_to_be_read_next() -> int:
    async with db.session_factory()() as session:
        return len(await pipeline.marked_ids(session, OWNER))


async def _save_a_smaller_number() -> set[tuple[str, str]]:
    async def write(session):
        await profile_store.save(session, OWNER, "settings", {**PROFILE["settings"], "apply_per_day": 1}, by="test")

    return await _told(write)


@row("the rules are saved")
async def test_after_the_settings_are_saved_the_old_passes_find_nothing_untold(js):
    await _a_deck_sized_by_the_settings()
    assert await _marked_to_be_read_next() == 3
    told = await _save_a_smaller_number()
    assert ("deck.dirty", "") in told
    await _old_turn_finds_nothing_the_writer_did_not_tell(told, keep_profile=True)
    assert await _marked_to_be_read_next() == 1, "and the old turn did change the deck: it is not vacuous"


@row("a deleted tab frees its roles")
async def test_after_a_deleted_tab_frees_its_roles_the_queue_leaves_the_data_where_the_old_passes_would_have_left_it(
        js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=criteria(tab("alpha"), tab("beta")))
        await session.commit()
    (role_id,) = await _made(make_role(target_role="beta", status=stages.FIRST_IMPRESSION))

    async def write(session):
        await profile_store.save(session, OWNER, "criteria", criteria(tab("alpha")), by="test")

    told = await _told(write)
    assert {("role.settle", str(role_id)), ("posting.roles", ""), ("deck.dirty", ""), ("ready.titles", "")} <= told
    await _old_turn_finds_nothing_the_writer_did_not_tell(told)


@row("the postings are judged again")
async def test_after_the_postings_are_judged_again_the_queue_leaves_the_data_where_the_old_passes_would_have_left_it(
        js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()
    # a posting that passed the filter, by an employer the owner's gates now avoid
    await _made(make_posting(company="Globex", title="Director of Programs", filtered_at=datetime.now(UTC),
                             route="roles", ats="employer"))

    async def write(session):
        await regate.run(session, OWNER, cause="test")

    told = await _told(write)
    assert {("posting.roles", ""), ("deck.dirty", "")} <= told
    await _old_turn_finds_nothing_the_writer_did_not_tell(told)


@row("Go opens a role")
async def test_after_go_opens_a_role_the_queue_leaves_the_data_where_the_old_passes_would_have_left_it(js):
    (role_id,) = await _made(make_role(status=stages.READY_TO_LAUNCH, **counted()))

    async def write(session):  # what Go's tab confirmation writes on the role (browser/go.py)
        (await session.get(Opportunity, role_id)).go_opened_at = datetime.now(UTC)

    told = await _told(write)
    assert ("deck.dirty", "") in told
    await _old_turn_finds_nothing_the_writer_did_not_tell(told)


@row("mail says a role was applied to")
async def test_after_mail_says_a_role_was_applied_to_the_queue_leaves_the_data_where_the_old_passes_would_have_left_it(
        js):
    (role_id,) = await _made(make_role(status=stages.READY_TO_LAUNCH, go_opened_at=datetime.now(UTC)))
    seen = SimpleNamespace(day=date.today(), sender_name="Fabrikam", subject="Thanks for applying")

    async def write(session):
        await go_applied(session, OWNER, await session.get(Opportunity, role_id), seen)

    told = await _told(write)
    assert {("role.settle", str(role_id)), ("deck.dirty", "")} <= told
    await _old_turn_finds_nothing_the_writer_did_not_tell(told)


@row("a job's state changes")
async def test_after_a_jobs_state_changes_the_queue_leaves_the_data_where_the_old_passes_would_have_left_it(js):
    await parse_runner(seen=timedelta(minutes=1))

    async def write(session):
        await role_parse.start(session, OWNER, "Operations Manager. We need someone to run the programs. " * 4)

    told = await _told(write)
    assert ("ready.parser", "") in told
    await _old_turn_finds_nothing_the_writer_did_not_tell(told)


# --- the direct statements (explicit posts) ---------------------------------------------------------------------------


@row("a claim is released")
async def test_after_a_claim_is_released_the_queue_leaves_the_data_where_the_old_passes_would_have_left_it(js):
    async with db.session_factory()() as session:
        claim = await hand_outs.claim(session, OWNER, hand_outs.ANALYSIS, ["item"], runner="session-1")
        await session.commit()
        claim_id = claim.id

    async def write(session):
        assert await hand_outs.release(session, OWNER, claim_id)

    told = await _told(write)
    assert {("ready.analysis", ""), ("pool.owed", "")} <= told
    await _old_turn_finds_nothing_the_writer_did_not_tell(told)


@row("the live roles get the free title match")
async def test_after_the_live_roles_get_the_free_title_match_the_queue_leaves_the_data_as_the_old_passes_would(js):
    criteria_of = {"company": "A made-up guild.", "target_roles": [
        {"id": "wheels", "name": "Wheelwrighting", "titles": ["Wheelwright"], "description": "Builds wheels."}]}
    await _made(make_role(title="Senior Wheelwright", status=stages.FIRST_IMPRESSION))
    await _settled()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=criteria_of)
        await session.commit()

    async def write(session):
        assert await role_match.code_pass(session, OWNER) == (1, 1)

    told = await _told(write)
    assert {("pool.owed", ""), ("deck.dirty", "")} <= told
    await _old_turn_finds_nothing_the_writer_did_not_tell(told, keep_profile=True)


@row("a runner goes idle")
async def test_after_a_runner_goes_idle_the_queue_leaves_the_data_where_the_old_passes_would_have_left_it(js):
    async def first_word(session):
        await roster.note_runner(session, OWNER, {"runner": "session-1", "state": "idle"})

    assert ("dispatch.idle", "session-1") in await _told(first_word)  # a runner heard for the first time
    async with db.session_factory()() as session:
        await roster.note_runner(session, OWNER, {"runner": "session-1", "state": "alive", "session_id": "s1"},
                                 "analyst")
        await session.commit()

    async def goes_idle(session):
        await roster.note_runner(session, OWNER, {"runner": "session-1", "state": "idle"})

    told = await _told(goes_idle)
    assert told == {("dispatch.idle", "session-1")}
    await _old_turn_finds_nothing_the_writer_did_not_tell(told)


@row("a kind is resumed")
async def test_after_a_kind_is_resumed_the_queue_leaves_the_data_where_the_old_passes_would_have_left_it(js):
    async with db.session_factory()() as session:
        await pool.pause(session, OWNER, "writer")
        await session.commit()

    async def write(session):
        await pool.resume(session, OWNER, "writer")

    told = await _told(write)
    assert {("pool.owed", ""), ("ready.seat", "")} <= told
    await _old_turn_finds_nothing_the_writer_did_not_tell(told)


@row("a row is taken by a claim")
async def test_after_a_row_is_taken_by_a_claim_the_queue_leaves_the_data_where_the_old_passes_would_have_left_it(js):
    (role_id,) = await _made(make_role(status=stages.FIRST_IMPRESSION, sourced_at=None))

    async def write(session):
        taken = await claim_work.claim_one(session, select(Opportunity).where(Opportunity.id == role_id),
                                           sourced_at=datetime.now(UTC))
        assert taken is not None and taken.id == role_id

    told = await _told(write)
    assert ("pool.owed", "") in told, "a statement that is not an ordinary save is answered by the claim itself"
    await _old_turn_finds_nothing_the_writer_did_not_tell(told)


@row("the tracker is imported")
async def test_after_the_tracker_is_imported_the_queue_leaves_the_data_where_the_old_passes_would_have_left_it(js):
    async def write(session):
        workbook = tracker_workbook([tracker_row("Fabrikam", "Lead"), tracker_row("Initech", "Director",
                                                                                   status="applied")])
        await tracker.import_tracker(session, OWNER, workbook)

    told = await _told(write)
    async with db.session_factory()() as session:
        ids = {str(i) for i in (await session.scalars(select(Opportunity.id))).all()}
    assert len(ids) == 2 and ids <= {key for topic, key in told if topic == "role.settle"}
    assert ("deck.dirty", "") in told
    await _old_turn_finds_nothing_the_writer_did_not_tell(told)


@row("the scout's report is stored")
async def test_after_the_scouts_report_is_stored_the_queue_leaves_the_data_where_the_old_passes_would_have_left_it(js):
    holder = {}

    async def call():
        holder["run"] = await run_round([reported("Fabrikam", "Director of Programs"),
                                         reported("Initech", "Programs Lead")])

    told = await _told_by(call)
    stored = await stored_by_title(holder["run"])
    assert stored and {str(r.id) for r in stored.values()} <= {key for topic, key in told if topic == "role.settle"}
    await _old_turn_finds_nothing_the_writer_did_not_tell(told)


@row("the scout's report is stored")
async def test_after_the_scouts_report_sets_a_posting_aside_as_a_known_role_s_the_old_passes_find_nothing_untold(js):
    """`dedupe` links the postings a reader was handed to the role the owner already has, by a direct statement: the
    role's text is owed from them (`index.fill_role_details`)."""
    title = "Director, Programs"
    key = match_key("Northwind Bank", title)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        started = await search_run.start_round(session, OWNER, started_by="manual")
        await session.commit()
        run_id = started.id
    role = make_role(company="Northwind Bank", title=title, match_key=key, description=None)
    posting = make_posting(company="Northwind Bank", title=title, match_key=key, ats="employer", text="A snippet.",
                           filtered_at=datetime.now(UTC) - timedelta(days=1))
    role_id, posting_id = await _made(role, posting)
    await _settled()

    async def write(session):
        staged = await session.get(SearchRun, run_id)
        await search_run.stage_report(session, staged, [reported("Northwind Bank", title)], done=False)
        data = {"owner": OWNER, "run": str(staged.id)}
        taken = await actions.take_reported(session, None, data)
        assert (await actions.dedupe(session, None, {**data, "taken": taken}))["roles"] == []

    told = await _told(write)
    async with db.session_factory()() as session:
        assert (await session.get(JobPosting, posting_id)).opportunity_id == role_id, "dedupe linked it"
    assert ("role.details", "") in told
    assert (await _owed(OWNER))["details"] == 1, "the scenario owes text"
    await _old_turn_finds_nothing_the_writer_did_not_tell(told)


@row("an employer's board is no longer read")
async def test_after_a_board_is_no_longer_read_the_queue_leaves_the_data_where_the_old_passes_would_have_left_it(
        js):
    await _made(make_posting(ats=EXAMPLE_SYSTEM))

    async def write(session):
        assert await not_checked.settle(session, OWNER)

    told = await _told(write)
    assert {("browser.reconcile", "*"), ("deck.dirty", "")} <= told
    await _old_turn_finds_nothing_the_writer_did_not_tell(told)


@row("the browser's queue is made")
async def test_after_the_browsers_queue_is_made_the_queue_leaves_the_data_where_the_old_passes_would_have_left_it(js):
    await browser_world()

    async def write(session):
        await browser_queue.refresh(session, DEFAULT_OWNER)

    told = await _told(write)
    assert ("ready.browser_reader", "") in told
    await _old_turn_finds_nothing_the_writer_did_not_tell(told, DEFAULT_OWNER)


@row("a site project is paused")
async def test_after_a_site_project_is_paused_the_queue_leaves_the_data_where_the_old_passes_would_have_left_it(js):
    async with db.session_factory()() as session:
        project = await new_project(session)
        await session.commit()
        project_id = project.id

    async def write(session):
        from app.sites.models import Project

        await completion.stop_project(session, await session.get(Project, project_id), "paused for a test")

    told = await _told(write)
    assert ("ready.site", "") in told
    await _old_turn_finds_nothing_the_writer_did_not_tell(told)


@row("the owner changes a mission's settings")
async def test_after_the_owner_changes_when_an_employer_is_researched_again_the_old_passes_find_nothing_untold(
        js, monkeypatch):
    """Found in the review of phase 3: the deck's step reads the research mission's again-after days
    (`deck.employers_for_deck`), so the owner's change was waiting for the hour's clock (`Mission.settings` was on the
    ignore list). An employer of a role on deck, researched thirty days ago, is looked at again once the days are
    fewer."""
    from app.jobsearch.missions import core as missions
    from app.jobsearch.missions import record
    from app.jobsearch.models import Mission
    from app.jobsearch.research import employer as employer_research

    monkeypatch.setattr(employer_research, "DECK_RESEARCH", True)
    employer = make_employer(name="Fabrikam", research_state="done",
                             researched_at=datetime.now(UTC) - timedelta(days=30))
    mission = blank(Mission, owner=OWNER, key=record.RESEARCH, name="Research employers",
                    settings={"again_after_days": 90}, state="on")
    employer_id, mission_id = await _made(employer, mission)
    await _made(make_role(status=stages.SECOND_IMPRESSION, employer_id=employer_id, analysis_state="waiting",
                          analysis_asked_at=datetime.now(UTC), sourced_at=datetime.now(UTC), found=date.today(),
                          posted=date.today()))  # in the analyst's line: the deck keeps it where it is
    await _settled()

    async def write(session):
        await missions.change(session, await session.get(Mission, mission_id), by="you",
                              values={"again_after_days": 10})

    told = await _told(write)
    assert {("employer.research", "*"), ("deck.dirty", "")} <= told
    await _old_turn_finds_nothing_the_writer_did_not_tell(told)
    async with db.session_factory()() as session:
        assert (await session.get(type(employer), employer_id)).research_state == "waiting", (
            "the scenario owes a research: the old turn queued it")


# --- the pull, the browser's queue and the walk (phase 4) -------------------------------------------------------------
#
# The old passes of phase 4 are the housekeeping's: the pull (`session_kinds.pull`), the browser queue's refresh and the
# walk the dispatcher's pass makes. After a writer, each one that would find something to do is one a to-do of its topic
# (`pool.owed`, `browser.reconcile`, `ready.<kind>`) was posted for. Each scenario gives the pass something to do.


async def _the_old_pull_starts_a_job_only_if_the_writer_told_the_pool(told: set[tuple[str, str]]) -> int:
    """A free pool session asks, and the old pull runs once: what it starts is what `pool.owed` is for."""
    session_kinds._pool_asked[OWNER] = datetime.now(UTC)
    async with db.session_factory()() as session:
        started = await session_kinds.pull(session, OWNER)
        await session.commit()
    assert not started or "pool.owed" in {topic for topic, _key in told}, (
        f"the old pull started a job the writer did not tell the pool of (posted: {sorted(told)})")
    return started


async def _the_old_refresh_changes_nothing_the_writer_did_not_tell(told: set[tuple[str, str]],
                                                                     owner: str = DEFAULT_OWNER) -> bool:
    """What the browser queue's refresh would change now (its plan, read and thrown away): when it is anything, a
    `browser.reconcile` to-do was posted. Returns whether it would change something, so a row can say it owed."""
    async with db.session_factory()() as session:
        plan = await browser_queue._plan(session, owner, datetime.now(UTC))
        owes = bool(plan and (plan.adds or plan.fills or plan.reopens or plan.drops))
        await session.rollback()
    assert not owes or "browser.reconcile" in {topic for topic, _key in told}, (
        f"the old refresh would change the browser's queue and the writer did not tell it (posted: {sorted(told)})")
    return owes


async def _the_walk_finds_no_kind_the_writer_did_not_tell(told: set[tuple[str, str]]) -> str | None:
    """The kind of session the walk would hand out next, when there is one: its `ready.<kind>` was posted."""
    async with db.session_factory()() as session:
        kind = await session_kinds.next_kind(session, OWNER)
        await session.rollback()
    assert kind is None or queue_topics.ready(kind) in {topic for topic, _key in told}, (
        f"the walk would hand out {kind} and the writer did not tell it (posted: {sorted(told)})")
    return kind


@row("a copy of a posting is made")
async def test_after_a_copy_of_a_posting_is_made_the_old_pull_finds_nothing_to_start_that_was_not_told(js):
    """A role with the text of its posting has a current copy, and so a rank the pull queues: `pool.owed`."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()
    role = make_role(status=stages.FIRST_IMPRESSION)
    posting = make_posting(opportunity_id=role.id, ats="employer")
    _role_id, posting_id = await _made(role, posting)

    async def write(session):
        await raw_copies._keep_report(session, await session.get(JobPosting, posting_id), {"title": "Lead"},
                                     reader="scout", text="The text of the posting, made a copy of. " * 20)

    told = await _told(write)
    assert ("pool.owed", "") in told
    assert await _the_old_pull_starts_a_job_only_if_the_writer_told_the_pool(told) == 1, "the scenario owes a rank"


@row("a session takes what was queued")
async def test_after_a_session_takes_what_was_queued_the_old_pull_finds_nothing_to_start_that_was_not_told(js):
    """The one-ahead slot of the research line is free again once the analyst has the role the pull put in it: the
    next role is the pull's to start, and nothing else says so."""
    ids = await analysis_setup()
    session_kinds._pool_asked[OWNER] = datetime.now(UTC)
    async with db.session_factory()() as session:
        assert await session_kinds.pull(session, OWNER) == 1
        assert await session_kinds.pull(session, OWNER) == 0, "one ahead: a role waits and no session has it"
        await session.commit()
    async with db.session_factory()() as session:
        queued = (await session.scalars(select(Opportunity).where(Opportunity.analysis_state == "waiting"))).one()
        queued_id = queued.id
    assert queued_id in ids.values()

    async def write(session):
        analysis.claim(await session.get(Opportunity, queued_id))

    told = await _told(write)
    assert ("pool.owed", "") in told
    assert await _the_old_pull_starts_a_job_only_if_the_writer_told_the_pool(told) == 1, "the scenario owes the next"


@row("a job is made for a session")
async def test_after_a_job_is_made_for_a_session_the_walk_finds_no_kind_that_was_not_told(js):
    from tests.builders import parse_job

    async def call():
        await parse_job()

    told = await _told_by(call)
    assert ("ready.parser", "") in told
    assert await _the_walk_finds_no_kind_the_writer_did_not_tell(told) == "parser", "the scenario has work for a lane"


async def _the_browsers_world_with_a_site_read_in_the_browser() -> None:
    from tests.builders import browser_world

    await browser_world()
    async with db.session_factory()() as session:
        await browser_queue.refresh(session, DEFAULT_OWNER)
        await session.commit()


def _on_the_site(**fields) -> Opportunity:
    return make_role(owner=DEFAULT_OWNER, url="https://listings.example.test/jobs/7", description=None, **fields)


@row("a role's stage changes the browser's work")
async def test_after_a_role_moves_between_the_stages_before_the_application_the_old_refresh_finds_nothing_untold(js):
    """A role in Cleansed Data with no text is read in full, one in the Landing zone is not (`to_read_in_full`)."""
    (role_id,) = await _made(_on_the_site(status=stages.LANDING_ZONE, sourced_at=None))  # not Sourced: read in full
    await _the_browsers_world_with_a_site_read_in_the_browser()

    async def write(session):  # what the turn's advance writes when the role's text is whole (`lifecycle.settle`)
        (await session.get(Opportunity, role_id)).status = stages.CLEANSED_DATA

    told = await _told(write)
    assert ("browser.reconcile", str(role_id)) in told
    assert await _the_old_refresh_changes_nothing_the_writer_did_not_tell(told), "the scenario owes a read item"


@row("a text arrives for a role the browser reads")
async def test_after_a_text_arrives_for_a_role_the_browser_reads_the_old_refresh_finds_nothing_untold(js):
    role = _on_the_site(status=stages.CLEANSED_DATA)
    posting = make_posting(owner=DEFAULT_OWNER, opportunity_id=role.id, ats="employer")
    role_id, posting_id = await _made(role, posting)
    await _the_browsers_world_with_a_site_read_in_the_browser()
    async with db.session_factory()() as session:
        assert (await session.scalar(select(func.count(BrowserItem.id)).where(BrowserItem.kind == "read"))) == 1

    async def write(session):
        await postings_index.keep_detail(session, await session.get(JobPosting, posting_id),
                                         {"text": "The whole text of the posting. " * 30})

    told = await _told(write)
    assert ("browser.reconcile", str(role_id)) in told
    assert await _the_old_refresh_changes_nothing_the_writer_did_not_tell(told), "the scenario drops its read item"


@row("Go could not read a posting")
async def test_after_go_could_not_read_a_posting_the_old_refresh_finds_nothing_untold(js):
    from app.jobsearch.browser import go_checks

    (role_id,) = await _made(make_role(owner=DEFAULT_OWNER, status=stages.READY_TO_LAUNCH,
                                       url="https://careers.northwind.test/jobs/3", description="x" * 400))
    await _the_browsers_world_with_a_site_read_in_the_browser()

    async def write(session):  # what Go writes on the role when its own check could not tell (`go_checks`)
        role = await session.get(Opportunity, role_id)
        role.go_check, role.go_checked_at = go_checks.BROWSER, datetime.now(UTC)

    told = await _told(write)
    assert ("browser.reconcile", str(role_id)) in told
    assert await _the_old_refresh_changes_nothing_the_writer_did_not_tell(told), "the scenario owes a priority read"


@row("the owner adds a job by link")
async def test_after_the_owner_adds_a_job_by_its_link_the_old_refresh_finds_nothing_untold(js):
    from app.jobsearch import ats
    from app.jobsearch.models import ADDED_LINK

    await _the_browsers_world_with_a_site_read_in_the_browser()

    async def write(session):
        session.add(make_posting(owner=DEFAULT_OWNER, ats=ats.OWNER, company=ADDED_LINK,
                                 url="https://jobs.example.test/added", title="jobs.example.test/added"))

    told = await _told(write)
    assert ("posting.sift", "") in told and ("browser.reconcile", "") in told
    assert await _the_old_refresh_changes_nothing_the_writer_did_not_tell(told), "the scenario owes a read item"


async def _a_listing_with_a_copy_and_no_role(*, routed: bool = True) -> uuid.UUID:
    """An open posting no role was made from, whose raw and cleansed copies exist (a board's read that gave its whole
    text): the pull has nothing to rank for it until a role has it. `routed`: the filter has let it through."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()
    read = datetime.now(UTC) - timedelta(days=1)
    key = match_key("Fabrikam", "Lead")
    routing = {"filtered_at": read, "route": "roles"} if routed else {}
    (posting_id,) = await _made(make_posting(company="Fabrikam", title="Lead", match_key=key, ats="employer",
                                             text="A snippet.", **routing))
    async with db.session_factory()() as session:
        await raw_copies._keep_report(session, await session.get(JobPosting, posting_id), {"title": "Lead"},
                                     reader="scout", text="The text of the posting, made a copy of. " * 20)
        await session.commit()
    return posting_id


def _pull_gained(told: Told, owner: str = OWNER) -> int:
    return told.after[owner].pull - told.before[owner].pull


@row("a role is made from a listing that has a copy")
async def test_after_a_role_is_made_from_a_listing_that_has_a_copy_the_old_pull_finds_nothing_to_rank_untold(js):
    """Found in the review of phase 4: the copy was made while the posting had no role, and the copy's own post is only
    for a posting that has one, so the role made from it was one the pull would rank and nothing said so."""
    await _a_listing_with_a_copy_and_no_role()

    async def write(session):
        assert await role_from_posting.make_roles(session, OWNER) == 1

    told = await _told(write)
    assert ("pool.owed", "") in told
    assert _pull_gained(told) == 1, "the scenario owes a rank"
    await _old_turn_finds_nothing_the_writer_did_not_tell(told, keep_profile=True)


@row("the scout's report is stored")
async def test_after_the_scouts_dedupe_links_a_posting_that_has_a_copy_the_old_pull_finds_nothing_to_rank_untold(js):
    """`dedupe` links the postings a reader was handed to the role the owner already has, by a direct statement: the
    role has a current copy from then on, and is one the pull ranks."""
    title = "Director, Programs"
    key = match_key("Northwind Bank", title)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        started = await search_run.start_round(session, OWNER, started_by="manual")
        await session.commit()
        run_id = started.id
    role = make_role(company="Northwind Bank", title=title, match_key=key, description=None)
    posting = make_posting(company="Northwind Bank", title=title, match_key=key, ats="employer", text="A snippet.",
                           filtered_at=datetime.now(UTC) - timedelta(days=1))
    role_id, posting_id = await _made(role, posting)
    async with db.session_factory()() as session:
        await raw_copies._keep_report(session, await session.get(JobPosting, posting_id), {"title": title},
                                     reader="scout", text="The text of the posting, made a copy of. " * 20)
        await session.commit()
    await _settled()

    async def write(session):
        staged = await session.get(SearchRun, run_id)
        await search_run.stage_report(session, staged, [reported("Northwind Bank", title)], done=False)
        data = {"owner": OWNER, "run": str(staged.id)}
        taken = await actions.take_reported(session, None, data)
        assert (await actions.dedupe(session, None, {**data, "taken": taken}))["roles"] == []

    told = await _told(write)
    async with db.session_factory()() as session:
        assert (await session.get(JobPosting, posting_id)).opportunity_id == role_id, "dedupe linked it"
    assert ("pool.owed", "") in told
    assert _pull_gained(told) == 1, "the scenario owes a rank"
    await _old_turn_finds_nothing_the_writer_did_not_tell(told, keep_profile=True)


@row("an employer link changes")
async def test_after_a_merge_gives_a_role_an_employer_the_pool_researches_the_old_pull_finds_nothing_untold(
        js, monkeypatch):
    """`employer.picks` reads which employer a role worth researching has: a merge moves the role (a direct statement)
    to the employer that was never researched, which the pool then takes."""
    from app.jobsearch.research import employer as employer_research

    monkeypatch.setattr(employer_research, "DECK_RESEARCH", True)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()
    now = datetime.now(UTC)
    keep, away = make_employer(name="Fabrikam"), make_employer(name="Fabrikam Inc", research_state="done",
                                                               researched_at=now)
    keep_id, away_id = await _made(keep, away)
    await _made(make_role(employer_id=away_id, status=stages.APPLIED))
    await _settled()

    async def write(session):
        await employer_records.merge(session, OWNER, keep_id, away_id, why="the same employer")

    told = await _told(write)
    assert ("pool.owed", "") in told
    assert _pull_gained(told) == 1, "the scenario owes an employer's research"
    await _old_turn_finds_nothing_the_writer_did_not_tell(told, keep_profile=True)


@row("the owner asks a recording")
async def test_after_the_owner_asks_a_recording_the_old_refresh_finds_nothing_untold(js):
    """Found in the review of phase 4: the ask made no to-do, so its `record` item waited for the hour's sweep where the
    old pass made it within 30 seconds."""
    from app.jobsearch.browser.recipes import record as recipe_record
    from tests.builders import TWO

    await _the_browsers_world_with_a_site_read_in_the_browser()

    async def write(session):
        site = await recipe_record.find_site(session, DEFAULT_OWNER, f"https://{TWO}/search")
        assert site is not None
        await recipe_record.request(session, DEFAULT_OWNER, site, "first")

    told = await _told(write)
    assert ("browser.reconcile", "") in told
    assert told.after[DEFAULT_OWNER].refresh and not told.before[DEFAULT_OWNER].refresh, "the scenario owes an item"
    await _old_turn_finds_nothing_the_writer_did_not_tell(told, DEFAULT_OWNER)


@row("a lease is given up")
async def test_after_a_session_gives_up_an_item_that_is_no_longer_called_for_the_old_refresh_finds_nothing_untold(js):
    """An item leased when its role got its text is left to its worker by the refresh; when the lease ends the item goes
    back to waiting, and the refresh would drop it (`_apply`): a look owed, which the return to waiting did not ask
    for."""
    from app.jobsearch.browser import leases as browser_leases

    role = _on_the_site(status=stages.CLEANSED_DATA)
    (role_id,) = await _made(role)
    await _the_browsers_world_with_a_site_read_in_the_browser()
    async with db.session_factory()() as session:
        item = (await session.scalars(select(BrowserItem).where(BrowserItem.kind == "read"))).one()
        item.state, item.leased_by, item.lease_until = "leased", "browser_reader-aaaa1111", datetime.now(UTC)
        (await session.get(Opportunity, role_id)).description = "The whole text of the posting. " * 30
        await session.commit()
    await pipeline.run_in_steps(DEFAULT_OWNER)  # the turn moves what the text moves now, not after the write

    async def write(session):
        assert await browser_leases.release_session(session, DEFAULT_OWNER, "browser_reader-aaaa1111",
                                                    "the session ended")

    told = await _told(write)
    assert ("browser.reconcile", "") in told
    assert told.after[DEFAULT_OWNER].refresh and not told.before[DEFAULT_OWNER].refresh, "the scenario drops the item"
    await _old_turn_finds_nothing_the_writer_did_not_tell(told, DEFAULT_OWNER)


# --- the queue's own turn leaves nothing for the old one (phase 3) ----------------------------------------------------


async def _the_queue_leaves_nothing_for_the_old_turn(told: set[tuple[str, str]], monkeypatch, *,
                                                     keep_profile: bool = False) -> None:
    """The phase 3 form of the row: the dispatcher drains what the writer posted (the filter, the roles and the turn),
    and then the old turn, run whole, changes nothing at all. The made-up profile is saved first unless `keep_profile`
    (a scenario that saved a profile of its own keeps it)."""
    if not keep_profile:
        async with db.session_factory()() as session:
            await load_profile(session, OWNER)
            await session.commit()
    assert {topic for topic, _key in told} & set(consume.TURN_TOPICS), "the writer told the turn"
    table = {t: h for t, h in handlers.HANDLERS.items()
             if t in (*consume.TURN_TOPICS, "posting.sift", "posting.roles")}
    await loop.Dispatcher(OWNER, table=table, gaps=False).drain()
    done = await pipeline.run_in_steps(OWNER)
    assert not any(done.values()), f"the old turn still changed {done} after the queue's own"
    assert not any((await _owed(OWNER)).values()), "and nothing is owed the filter or the read of descriptions"


async def test_after_the_owner_moves_a_role_the_queues_own_turn_leaves_nothing_for_the_old_one(js, monkeypatch):
    (role_id,) = await _made(make_role(status=stages.FIRST_IMPRESSION))
    await _settled()

    async def write(session):
        await board.move(session, OWNER, role_id, stages.SECOND_IMPRESSION)

    await _the_queue_leaves_nothing_for_the_old_turn(await _told(write), monkeypatch)


async def test_after_the_owner_stars_a_role_the_queues_own_turn_leaves_nothing_for_the_old_one(js, monkeypatch):
    (role_id,) = await _made(make_role(status=stages.SECOND_IMPRESSION))
    await _settled()

    async def write(session):
        await favorites.set_favorite(session, OWNER, role_id, True)

    await _the_queue_leaves_nothing_for_the_old_turn(await _told(write), monkeypatch)


async def test_after_an_employer_link_changes_the_queues_own_turn_leaves_nothing_for_the_old_one(js, monkeypatch):
    keep, away = make_employer(name="Fabrikam"), make_employer(name="Fabrikam Inc")
    keep_id, away_id = await _made(keep, away)
    await _made(make_role(employer_id=away_id, status=stages.APPLIED))
    await _settled()

    async def write(session):
        await employer_records.merge(session, OWNER, keep_id, away_id, why="the same employer")

    await _the_queue_leaves_nothing_for_the_old_turn(await _told(write), monkeypatch)


async def test_after_a_deleted_tab_frees_its_roles_the_queues_own_turn_leaves_nothing_for_the_old_one(js, monkeypatch):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=criteria(tab("alpha"), tab("beta")))
        await session.commit()
    await _made(make_role(target_role="beta", status=stages.FIRST_IMPRESSION))
    await _settled()

    async def write(session):
        await profile_store.save(session, OWNER, "criteria", criteria(tab("alpha")), by="test")

    await _the_queue_leaves_nothing_for_the_old_turn(await _told(write), monkeypatch)


@pytest.mark.parametrize("save", STALE_MAKING_SAVES)
async def test_after_a_document_a_read_was_made_under_is_saved_the_queues_own_turn_marks_it_and_leaves_nothing(
        js, monkeypatch, save):
    role_id = await _a_read_deck_role()
    told = await _told(STALE_MAKING_SAVES[save])
    assert await _owes_a_read_again(role_id), "the scenario owes a read again"
    await _the_queue_leaves_nothing_for_the_old_turn(told, monkeypatch)
    assert await _marked_to_be_read_again(role_id), "the queue's own turn marked it, not the old one"


async def test_after_the_settings_are_saved_the_queues_own_turn_resizes_the_deck_and_leaves_nothing(js, monkeypatch):
    await _a_deck_sized_by_the_settings()
    assert await _marked_to_be_read_next() == 3
    told = await _save_a_smaller_number()
    await _the_queue_leaves_nothing_for_the_old_turn(told, monkeypatch, keep_profile=True)
    assert await _marked_to_be_read_next() == 1, "the queue's own turn did it"


def _topics_of_the_turns_steps() -> set[str]:
    """The topic each step of the old turn is compared with (`covered(step, topic, ...)` in `deck._stages`), read from
    the code, so a step added with a topic nothing takes fails here."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(pipeline._stages).lstrip())):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "covered" and len(node.args) >= 2:
            topic = node.args[1]
            assert isinstance(topic, ast.Attribute) and isinstance(topic.value, ast.Name) and topic.value.id == "queue"
            found.add(getattr(queue_topics, topic.attr))
    return found


def test_every_topic_of_the_old_turns_steps_is_taken_by_a_handler_that_runs_it():
    taken = {topic for handler in handlers.HANDLERS.values() for topic in handler.takes}
    steps = _topics_of_the_turns_steps()
    assert len(steps) >= 6, "the read found the steps"
    assert not steps - taken, f"a step of the old turn compares itself with a topic nothing takes: {steps - taken}"
    assert set(TOPIC_OF.values()) <= taken | {"role.details", "posting.sift", "posting.roles"}, (
        "and every topic the matrix tells a step by is a topic a handler takes")
    assert {topic for step, topic in TOPIC_OF.items() if step not in ("made", "sift", "details")} <= set(
        consume.TURN_TOPICS), "the turn's own steps are the turn handler's"


#: The topics a writer posts that no handler takes yet, each with what takes it (the work queue spec C4 and
#: the phase table). A list that only shrinks: a phase that gives a topic a handler takes it off, and the test below
#: fails until it does. A topic a writer posts is either taken or here, so a to-do can never be posted for no one by
#: accident: `rules.changed` was (found in review, 2026-11-02), and a saved document waited for the hour's clock.
#: Phase 4 took `browser.reconcile` (`browser`), `pool.owed` (`pull`), the thirteen `ready.<kind>` and, found in its
#: review, `dispatch.idle` (`walk`: a lane that goes idle while work waits is commanded from its own to-do, as the old
#: 10 second loop did, not at the next minute's tick).
NOT_TAKEN_YET = {
    "rules.changed": "its catch-ups (C4: regate.catch_up, prioritize.catch_up, the pay-floor re-read, keep_current); "
                     "until then a saved document asks the turn itself (watch.py)",
}


def _taken_topics() -> set[str]:
    return {topic for handler in handlers.HANDLERS.values() for topic in handler.takes}


def test_every_topic_a_writer_can_post_is_taken_by_a_handler_or_waits_for_a_named_phase():
    taken, waiting = _taken_topics(), set(NOT_TAKEN_YET)
    written = set(queue_topics.WRITE_DRIVEN)
    unclaimed = written - taken - waiting
    assert not unclaimed, f"a topic a writer posts that no one takes and no phase is named for: {sorted(unclaimed)}"
    assert not waiting & taken, f"taken now, so off the list: {sorted(waiting & taken)}"
    assert waiting <= written, f"on the list and not a topic a writer posts: {sorted(waiting - written)}"
    assert all(len(phase) >= 8 for phase in NOT_TAKEN_YET.values()), "each says what takes it"


def test_every_topic_the_watch_list_and_the_direct_writers_post_is_one_a_writer_can_post():
    written = set(queue_topics.WRITE_DRIVEN)
    watched = {post.topic for entry in watch.WATCH.values()
               for post in (*entry.created, *entry.deleted, *(p for rule in entry.rules for p in rule.posts))
               if isinstance(post.topic, str)}
    assert watched <= written, f"the watch list posts a topic WRITE_DRIVEN lacks: {sorted(watched - written)}"
    named = {getattr(queue_topics, name) for entry in scan.baseline().values() for name in entry.get("posts", [])
             if name.isupper() and hasattr(queue_topics, name) and isinstance(getattr(queue_topics, name), str)}
    assert named - {queue_topics.ANY} <= written, (
        f"a direct writer posts a topic WRITE_DRIVEN lacks: {sorted(named - {queue_topics.ANY} - written)}")


def test_a_saved_rules_document_is_one_change_that_asks_a_topic_a_handler_takes():
    """`rules.changed` is waiting for its catch-ups and is not posted by a save, so the saves of the documents the turn
    reads ask the turn as well: the one place the list above would otherwise let a change be told to no one."""
    taken = _taken_topics()
    for model in (ProfileVersion, InstructionFile):
        topics = {post.topic for post in watch.WATCH[model].created}
        assert queue_topics.RULES_CHANGED not in topics, f"{model.__name__} posts a to-do no handler takes"
        assert topics & taken, f"{model.__name__} is saved and no handler takes a to-do it posts: {sorted(topics)}"


# --- the matrix is whole ----------------------------------------------------------------------------------------------


def test_every_writer_family_in_the_scan_has_a_row_in_the_matrix():
    found, listed = scan.scan(), scan.baseline()
    posting = {key for key, site in found.items() if site.calls}
    answered = {key for key, entry in listed.items() if "answered_by" in entry}
    covered = {site for sites in FAMILIES.values() for site in sites}
    unrowed = (posting | answered) - covered
    assert not unrowed, f"a writer that posts and has no row: {sorted(unrowed)}"
    assert not covered - set(found), f"a row names a writer that is gone: {sorted(covered - set(found))}"
    assert set(FAMILIES) == TESTED, f"a family with no test: {sorted(set(FAMILIES) - TESTED)}"
