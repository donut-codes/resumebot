"""The quiet pass reads in slices and apart from the writer's turn, and takes the turn for its moves only (SQLite port
round 4; docs/PRD.md 14.7, 15.1.8). Found on a large database: the pass held the file's one turn for a long time
over many statements (the reads of the roles and of every unlinked posting among them) and stalled the loop
building the postings at once. Now the reads are made in a reader's session in slices, the decision is made in memory,
and the writer's turn is taken for each rule's moves, after the cheap evidence is read again under it. Synthetic data
only: every employer, title and address here is made up (reserved example domains)."""

import asyncio
import itertools
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update

from app import db
from app.jobsearch import actions, stages
from app.jobsearch.employers import core as employers
from app.jobsearch.mail import kinds as mail_kinds
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import JobPosting, MailMessage, Opportunity, StageMove
from app.jobsearch.pipeline import quiet
from app.jobsearch.postings import coop as cooperating
from app.jobsearch.postings.coop import EVERY
from app.models.work import TrailEvent
from app.settings import settings
from tests.support import EXAMPLE_SYSTEM, OWNER, PROFILE, load_profile

OTHER = "second-owner"
SEEN = datetime(2026, 10, 9, 5, 0, tzinfo=UTC)
CLOSED = datetime(2026, 10, 11, 5, 0, tzinfo=UTC)
READ = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)
TODAY = READ.date()


@pytest.fixture(autouse=True)
def owner_days_are_utc(monkeypatch):
    monkeypatch.setattr(settings, "timezone", "UTC")


def _url(slug: str) -> str:
    return f"https://boards.example.test/acme/jobs/{slug}"


#: The order the roles were made in, which is the order a rule moves them (by `created_at`, then id).
MADE = ("went", "tie", "hand", "open", "unlinked", "silent", "heard", "keep", "go")


def _role(owner: str, slug: str, status: str, employer_id, **more) -> Opportunity:
    return Opportunity(
        owner=owner, slug=slug, company="Acme", title=f"Analyst {slug}", status=status, match_key=f"acme|{slug}",
        origin="scout", fit=3, employer_id=employer_id, created_at=SEEN + timedelta(minutes=MADE.index(slug)), **more)


def _posting(owner: str, employer_id, ext_id: str, *, role: Opportunity | None = None, closed: datetime | None = None,
             url: str | None = None) -> JobPosting:
    title = f"Analyst {ext_id}"
    return JobPosting(
        owner=owner, employer_id=employer_id, ats=EXAMPLE_SYSTEM, ext_id=ext_id, company="Acme", title=title,
        match_key=match_key("Acme", title), first_seen=SEEN, last_seen=SEEN, links=[], closed_at=closed,
        opportunity_id=role.id if role else None, url=url or _url(ext_id))


async def _employer(session, owner: str):
    employer = await employers.find_or_make(session, owner, "Acme")
    employer.ats, employer.board = EXAMPLE_SYSTEM, {"slug": "acme"}
    await session.flush()
    return employer


async def _applied_ago(session, role: Opportunity, days: int) -> None:
    await session.flush()
    at = datetime.combine(TODAY - timedelta(days=days), datetime.min.time(), tzinfo=UTC)
    await session.execute(update(StageMove).where(StageMove.role_id == role.id).values(at=at))


async def _world(owner: str, *, unlinked: int = 50) -> dict[str, Opportunity]:
    """The same roles for any owner: two postings of one role closing at one instant (a tie), a role the owner moved by
    hand after the closing, one with a posting still open, one whose closed posting is an unlinked one among many, one
    that heard nothing, and one that heard recently."""
    async with db.session_factory()() as session:
        await load_profile(session, owner, settings={**PROFILE["settings"], "no_reply_days": 30})
        acme = await _employer(session, owner)
        roles = {slug: _role(owner, slug, status, acme.id, url=_url(f"role-{slug}")) for slug, status in {
            "went": stages.FOLLOWUP_NEEDED, "tie": stages.APPLIED, "hand": stages.SECOND_IMPRESSION,
            "open": stages.READY_TO_LAUNCH, "unlinked": stages.LANDING_ZONE, "silent": stages.APPLIED,
            "heard": stages.APPLIED}.items()}
        session.add_all(roles.values())
        await session.flush()
        session.add_all([
            _posting(owner, acme.id, "went-1", role=roles["went"], closed=CLOSED),
            _posting(owner, acme.id, "tie-1", role=roles["tie"], closed=CLOSED),
            _posting(owner, acme.id, "tie-2", role=roles["tie"], closed=CLOSED),
            _posting(owner, acme.id, "hand-1", role=roles["hand"], closed=CLOSED),
            _posting(owner, acme.id, "open-1", role=roles["open"], closed=CLOSED),
            _posting(owner, acme.id, "open-2", role=roles["open"]),
            _posting(owner, acme.id, "carries", closed=CLOSED, url=_url("role-unlinked")),
            *(_posting(owner, acme.id, f"other-{n}", closed=CLOSED) for n in range(unlinked))])
        session.add(TrailEvent(owner=owner, at=CLOSED + timedelta(hours=1), actor="person", who="Tester",
                               event="role_moved", sentence="You moved a role.", link_kind="opportunity",
                               link_id=roles["hand"].id))
        await _applied_ago(session, roles["tie"], 60)
        await _applied_ago(session, roles["silent"], 40)
        await _applied_ago(session, roles["heard"], 40)
        session.add(MailMessage(
            owner=owner, message_key="m1", sender="Acme", sender_key="acme.example", subject="Hello", state="acted",
            day=TODAY - timedelta(days=3), kind=mail_kinds.CONFIRMATION, role_id=roles["heard"].id))
        await session.commit()
        return roles


async def _snapshot(owner: str) -> tuple[dict, list]:
    """The roles' stages and reasons, and the history lines in the order the table holds them (the rows that were
    written and their order), by role slug."""
    async with db.session_factory()() as session:
        roles = {r.id: r for r in (await session.scalars(select(Opportunity).where(Opportunity.owner == owner))).all()}
        states = {r.slug: (r.status, r.ended_reason) for r in roles.values()}
        lines = [(t.event, roles[t.link_id].slug, t.sentence, t.detail, t.actor)
                 for t in (await session.scalars(select(TrailEvent).where(
                     TrailEvent.owner == owner, TrailEvent.link_id.in_(list(roles)))
                     .order_by(text(f"{TrailEvent.__tablename__}.rowid")))).all()]
        return states, lines


async def _stage_of(slug: str, owner: str = OWNER) -> str:
    async with db.session_factory()() as session:
        return (await session.scalars(select(Opportunity.status).where(
            Opportunity.owner == owner, Opportunity.slug == slug))).one()


@pytest.fixture
def turns():
    """The writer's turns taken from here on: (seconds waited, seconds held) for each."""
    seen: list[tuple[float, float | None]] = []

    def noted(waited: float, held: float | None) -> None:
        seen.append((waited, held))

    db.turn_ends.append(noted)
    try:
        yield seen
    finally:
        db.turn_ends.remove(noted)


# --- the same rows as before --------------------------------------------------------------------------------------


async def test_the_pass_writes_the_same_rows_in_the_same_order_as_inside_a_savepoint(js):
    await _world(OWNER)
    await _world(OTHER)
    async with db.session_factory()() as session:
        mine = await quiet.run(session, OWNER, today=TODAY)
        await session.commit()
    async with db.session_factory()() as session:
        async with session.begin_nested():  # the old path: the caller's transaction is the whole
            theirs = await quiet.run(session, OTHER, today=TODAY)
        await session.commit()
    assert (mine.went, mine.silent, mine.days) == (theirs.went, theirs.silent, theirs.days) == (3, 1, 30)
    states, lines = await _snapshot(OWNER)
    assert states == (await _snapshot(OTHER))[0]
    assert lines == (await _snapshot(OTHER))[1]
    assert {slug: s for slug, (s, _) in states.items()} == {
        "went": "gone_quiet", "tie": "gone_quiet", "hand": "second_impression", "open": "ready_to_launch",
        "unlinked": "gone_quiet", "silent": "gone_quiet", "heard": "applied"}
    moves = [(event, slug) for event, slug, *_ in lines if event in (quiet.POSTING_WENT_EVENT, quiet.NO_REPLY_EVENT)]
    assert moves == [(quiet.POSTING_WENT_EVENT, "went"), (quiet.POSTING_WENT_EVENT, "tie"),
                     (quiet.POSTING_WENT_EVENT, "unlinked"), (quiet.NO_REPLY_EVENT, "silent")]


async def test_two_postings_of_one_role_closing_at_one_instant_tie_as_before(js):
    await _world(OWNER)
    await _world(OTHER)
    async with db.session_factory()() as session:
        await quiet.run(session, OWNER, today=TODAY)
        await session.commit()
    async with db.session_factory()() as session:
        async with session.begin_nested():
            await quiet.run(session, OTHER, today=TODAY)
        await session.commit()
    lines = {owner: [line for line in (await _snapshot(owner))[1] if line[1] == "tie"] for owner in (OWNER, OTHER)}
    assert lines[OWNER] == lines[OTHER] != []


# --- no read on the writer's turn --------------------------------------------------------------------------------


async def test_no_read_of_the_roles_or_the_postings_runs_in_a_writer_transaction(js, transactions):
    await _world(OWNER, unlinked=50)
    transactions.clear()
    async with db.session_factory()() as session:
        result = await quiet.run(session, OWNER, today=TODAY)
        await session.commit()
    assert (result.went, result.silent) == (3, 1)
    writers = [t for t in transactions.ended if transactions.writes(t)]
    readers = [t for t in transactions.ended if not transactions.writes(t)]
    assert writers and readers
    wide = ("opportunity_id IS NULL", "ORDER BY js_opportunities.created_at")
    assert not [s for t in writers for s in t if any(mark in s for mark in wide)], "the wide reads are not on a turn"
    assert [s for t in readers for s in t if "opportunity_id IS NULL" in s], "they ran in a reader's session"
    assert [s for t in readers for s in t if "ORDER BY js_opportunities.created_at" in s]


async def test_a_rule_one_and_a_rule_two_that_both_move_roles_take_at_most_three_turns(js, turns):
    await _world(OWNER)
    turns.clear()
    async with db.session_factory()() as session:
        result = await quiet.run(session, OWNER, today=TODAY)
        await session.commit()
    assert (result.went, result.silent) == (3, 1)
    assert 1 <= len([held for _, held in turns if held is not None]) <= 3


async def test_a_pass_that_moves_nothing_takes_no_turn(js, turns):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={**PROFILE["settings"], "no_reply_days": 30})
        await session.commit()
    turns.clear()
    async with db.session_factory()() as session:
        assert (await quiet.run(session, OWNER, today=TODAY)).went == 0
        await session.commit()
    assert [t for t in turns if t[1] is not None] == [], "a pass with nothing to move reads only"


async def test_a_pass_gives_the_loop_back_while_it_builds_thousands_of_unlinked_postings(js, monkeypatch):
    await _world(OWNER, unlinked=3000)
    ticks = 0
    stop = False
    seen: list[int] = []  # the turns the loop had had when each posting's links were read
    real = quiet.same_role.posting_links

    def links_of(posting):
        seen.append(ticks)
        return real(posting)

    monkeypatch.setattr(quiet.same_role, "posting_links", links_of)

    class Clock:  # the give-back's clock, injected: each read moves it a quarter of EVERY, whatever the machine's load
        now = 0.0

        def perf_counter(self) -> float:
            self.now += EVERY / 4
            return self.now

    monkeypatch.setattr(cooperating, "time", Clock())

    async def ticker() -> None:
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0)

    watching = asyncio.create_task(ticker())
    try:
        async with db.session_factory()() as session:
            result = await quiet.run(session, OWNER, today=TODAY)
            await session.commit()
    finally:
        stop = True
        await watching
    assert result.went == 3
    # The stretch in which the unlinked postings are built (the last 3000 reads of links; the reads before it and the
    # writes after are other work). The pass gives the loop back once EVERY has gone by on its clock; with the clock
    # injected that is every few postings, so the ticker gets a turn at least every ten. A pass that never gave the
    # loop back leaves 0 turns.
    assert len(seen) >= 3000
    stretch = seen[-3000:]
    assert stretch[-1] - stretch[0] >= 1, "the loop got no turn while the postings were built"
    longest = run = 0
    for before, after in itertools.pairwise(stretch):
        run = 0 if after > before else run + 1
        longest = max(longest, run)
    assert longest < 10, f"{longest} postings in a row were built with no turn for the loop"


# --- what changes between the read and the lock -------------------------------------------------------------------


def _before_lock(monkeypatch, stage_list, act) -> None:
    """Run `act` in another session, committed, once, just before the pass locks the roles of this stage list."""
    real, done = quiet._lock_movers, []

    async def lock(session, movers, stage):
        if tuple(stage) == tuple(stage_list) and not done:
            done.append(1)
            async with db.session_factory()() as other:
                await act(other)
                await other.commit()
        return await real(session, movers, stage)

    monkeypatch.setattr(quiet, "_lock_movers", lock)


async def _pair(owner: str = OWNER, status: str = stages.SECOND_IMPRESSION) -> tuple[uuid.UUID, uuid.UUID]:
    """Two roles whose postings went; the first is the one a change between the read and the lock is made to."""
    async with db.session_factory()() as session:
        await load_profile(session, owner, settings={**PROFILE["settings"], "no_reply_days": 30})
        acme = await _employer(session, owner)
        keep, go = _role(owner, "keep", status, acme.id), _role(owner, "go", status, acme.id)
        session.add_all([keep, go])
        await session.flush()
        session.add_all([_posting(owner, acme.id, "keep-1", role=keep, closed=CLOSED),
                         _posting(owner, acme.id, "go-1", role=go, closed=CLOSED)])
        await session.commit()
        return keep.id, go.id


async def _run_with(monkeypatch, stage_list, act) -> quiet.Result:
    _before_lock(monkeypatch, stage_list, act)
    async with db.session_factory()() as session:
        result = await asyncio.wait_for(quiet.run(session, OWNER, today=TODAY), 20)
        await session.commit()
    return result


async def test_a_role_the_owner_moved_by_hand_between_the_read_and_the_lock_stays(js, monkeypatch):
    keep, _ = await _pair()

    async def act(other):
        await other.execute(update(Opportunity).where(Opportunity.id == keep).values(status=stages.ACTIVE_DISCUSSION))

    result = await _run_with(monkeypatch, quiet.RULE_ONE_STAGES, act)
    assert result.went == 1
    assert (await _stage_of("keep"), await _stage_of("go")) == (stages.ACTIVE_DISCUSSION, stages.GONE_QUIET)


async def test_a_posting_that_reopens_between_the_read_and_the_lock_does_not_move_its_role(js, monkeypatch):
    keep, _ = await _pair()

    async def act(other):
        await other.execute(update(JobPosting).where(JobPosting.opportunity_id == keep).values(closed_at=None))

    result = await _run_with(monkeypatch, quiet.RULE_ONE_STAGES, act)
    assert result.went == 1
    assert (await _stage_of("keep"), await _stage_of("go")) == (stages.SECOND_IMPRESSION, stages.GONE_QUIET)


async def test_a_repost_linked_between_the_read_and_the_lock_does_not_move_its_role(js, monkeypatch):
    keep, _ = await _pair()

    async def act(other):
        acme = await _employer(other, OWNER)
        other.add(JobPosting(
            owner=OWNER, employer_id=acme.id, ats=EXAMPLE_SYSTEM, ext_id="repost", company="Acme",
            title="Analyst again",
            match_key=match_key("Acme", "Analyst again"), first_seen=READ, last_seen=READ, links=[],
            opportunity_id=keep, url=_url("repost")))

    result = await _run_with(monkeypatch, quiet.RULE_ONE_STAGES, act)
    assert result.went == 1
    assert (await _stage_of("keep"), await _stage_of("go")) == (stages.SECOND_IMPRESSION, stages.GONE_QUIET)


async def test_an_unlinked_repost_carrying_the_roles_link_between_the_read_and_the_lock_does_not_move_its_role(
        js, monkeypatch):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={**PROFILE["settings"], "no_reply_days": 30})
        acme = await _employer(session, OWNER)
        keep = _role(OWNER, "keep", stages.SECOND_IMPRESSION, acme.id, url=_url("role-keep"))
        go = _role(OWNER, "go", stages.SECOND_IMPRESSION, acme.id, url=_url("role-go"))
        session.add_all([keep, go])
        await session.flush()
        session.add_all([_posting(OWNER, acme.id, "keep-1", role=keep, closed=CLOSED),
                         _posting(OWNER, acme.id, "go-1", role=go, closed=CLOSED)])
        await session.commit()

    async def act(other):
        acme = await _employer(other, OWNER)
        other.add(_posting(OWNER, acme.id, "reposted", url=_url("role-keep")))

    result = await _run_with(monkeypatch, quiet.RULE_ONE_STAGES, act)
    assert result.went == 1
    assert (await _stage_of("keep"), await _stage_of("go")) == (stages.SECOND_IMPRESSION, stages.GONE_QUIET)
    _, lines = await _snapshot(OWNER)
    assert [slug for event, slug, *_ in lines if event == quiet.POSTING_WENT_EVENT] == ["go"]


async def test_a_hand_line_arriving_between_the_read_and_the_lock_keeps_the_role(js, monkeypatch):
    keep, _ = await _pair()

    async def act(other):
        other.add(TrailEvent(owner=OWNER, at=READ, actor="person", who="Tester", event="packet_asked",
                             sentence="You asked for a packet.", link_kind="opportunity", link_id=keep))

    result = await _run_with(monkeypatch, quiet.RULE_ONE_STAGES, act)
    assert result.went == 1
    assert (await _stage_of("keep"), await _stage_of("go")) == (stages.SECOND_IMPRESSION, stages.GONE_QUIET)


async def test_a_matched_mail_arriving_between_the_read_and_the_lock_keeps_the_applied_role(js, monkeypatch):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={**PROFILE["settings"], "no_reply_days": 30})
        acme = await _employer(session, OWNER)
        keep, go = _role(OWNER, "keep", stages.APPLIED, acme.id), _role(OWNER, "go", stages.APPLIED, acme.id)
        session.add_all([keep, go])
        await _applied_ago(session, keep, 40)
        await _applied_ago(session, go, 40)
        await session.commit()
        keep_id = keep.id

    async def act(other):
        other.add(MailMessage(
            owner=OWNER, message_key="late", sender="Acme", sender_key="acme.example", subject="Hello", state="acted",
            day=TODAY - timedelta(days=2), kind=mail_kinds.CONFIRMATION, role_id=keep_id))

    result = await _run_with(monkeypatch, (stages.APPLIED,), act)
    assert (result.went, result.silent) == (0, 1)
    assert (await _stage_of("keep"), await _stage_of("go")) == (stages.APPLIED, stages.GONE_QUIET)


# --- one run at a time, and a failure of rule 2 ------------------------------------------------------------------


async def test_a_second_run_is_told_busy_after_the_lock_wait_and_changes_nothing(js, monkeypatch):
    await _pair()
    monkeypatch.setattr(db, "lock_wait_seconds", lambda: 0.05)
    async with db.named_lock(f"quiet:{OWNER}"):
        async with db.session_factory()() as session:
            with pytest.raises(db.Busy):
                await quiet.run(session, OWNER, today=TODAY)
        async with db.session_factory()() as session:
            answer = await actions.quiet_roles(session, None, {"owner": OWNER})
            await session.commit()
    assert answer == {"went": 0, "silent": 0, "days": None}
    assert (await _stage_of("keep"), await _stage_of("go")) == (stages.SECOND_IMPRESSION, stages.SECOND_IMPRESSION)


async def test_rule_two_failing_after_rule_one_committed_returns_rule_ones_counts(js, monkeypatch, caplog):
    await _pair()

    async def fails(*args, **more):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(quiet, "_rule_two", fails)
    async with db.session_factory()() as session:
        result = await quiet.run(session, OWNER, today=TODAY)
        await session.commit()
    assert (result.went, result.silent) == (2, 0)
    assert "heard nothing failed" in caplog.text
    assert (await _stage_of("keep"), await _stage_of("go")) == (stages.GONE_QUIET, stages.GONE_QUIET)


async def test_an_unlinked_repost_with_the_roles_exact_link_under_another_employer_keeps_the_role(js, monkeypatch):
    keep, _ = await _pair()
    async with db.session_factory()() as session:
        await session.execute(update(Opportunity).where(Opportunity.id == keep).values(url=_url("role-keep")))
        await session.commit()

    async def act(other):
        elsewhere = await employers.find_or_make(other, OWNER, "Elsewhere")
        other.add(JobPosting(
            owner=OWNER, employer_id=elsewhere.id, ats=EXAMPLE_SYSTEM, ext_id="moved", company="Elsewhere",
            title="Analyst moved", match_key=match_key("Elsewhere", "Analyst moved"), first_seen=SEEN, last_seen=SEEN,
            links=[], url=_url("role-keep")))

    result = await _run_with(monkeypatch, quiet.RULE_ONE_STAGES, act)
    assert result.went == 1
    assert (await _stage_of("keep"), await _stage_of("go")) == (stages.SECOND_IMPRESSION, stages.GONE_QUIET)
    _, lines = await _snapshot(OWNER)
    assert [slug for event, slug, *_ in lines if event == quiet.POSTING_WENT_EVENT] == ["go"]


async def test_the_recheck_under_the_turn_does_not_scan_the_postings_table(js, monkeypatch):
    await _pair()
    async with db.session_factory()() as session:  # roles with a link and no employer record: no index by company text
        await session.execute(update(Opportunity).where(Opportunity.owner == OWNER).values(
            employer_id=None, url=_url("role-any")))
        await session.commit()
    statements: list[tuple[str, object]] = []
    real = quiet._evidence_again

    async def watched(session, owner, *args):
        from sqlalchemy import event
        sync = session.sync_session.get_bind()

        def note(conn, cursor, statement, parameters, context, executemany):
            statements.append((statement, parameters))

        event.listen(sync, "before_cursor_execute", note)
        try:
            return await real(session, owner, *args)
        finally:
            event.remove(sync, "before_cursor_execute", note)

    monkeypatch.setattr(quiet, "_evidence_again", watched)

    async def act(other):
        pass

    await _run_with(monkeypatch, quiet.RULE_ONE_STAGES, act)
    assert statements
    async with db.session_factory()() as session:
        connection = await session.connection()
        for statement, parameters in statements:
            plan = (await connection.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, tuple(parameters))).all()
            # no scan of the table, and no search of all of the owner's rows either (what a scan is on a small copy)
            wide = [row for row in plan if str(row[-1]).startswith("SCAN js_postings")
                    or (str(row[-1]).startswith("SEARCH js_postings") and str(row[-1]).endswith("(owner=?)"))]
            assert not wide, (statement, plan)
