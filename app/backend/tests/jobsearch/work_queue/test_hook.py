"""The hook (the work queue spec C3, D1, H): every ordinary save of a watched model posts what the watch
list says, a write that changes nothing posts nothing, and every column of a watched model is on the list or ignored.
Everything is made up."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.models import (
    BrowserItem,
    Employer,
    GapRun,
    HandOut,
    InstructionFile,
    JobPosting,
    Mission,
    Opportunity,
    ProfileVersion,
    Todo,
)
from app.jobsearch.profile import base as profile_base
from app.jobsearch.profile import documents
from app.jobsearch.research import analysis, line
from app.jobsearch.work import queue
from app.jobsearch.work.queue import topics, watch
from app.models.agent import AgentConnection, AgentKindPause, AgentRunner
from app.models.work import TrailEvent
from app.sites.models import Site
from tests.jobsearch.work_queue.support import (
    OWNER,
    a_value,
    blank,
    make_employer,
    make_posting,
    make_role,
    posted,
    saved,
    without_links,
)

WATCHED = list(watch.WATCH.values())


def test_every_column_of_a_watched_model_is_on_the_watch_list_or_the_ignore_list():
    for entry in WATCHED:
        columns = {c.name for c in entry.model.__table__.columns}
        watched = entry.watched_columns()
        name = entry.model.__name__
        assert not watched & entry.ignore, f"{name}: on a rule and ignored: {sorted(watched & entry.ignore)}"
        neither = columns - watched - entry.ignore
        assert neither == set(), f"{name}: on neither: {sorted(neither)}"
        unknown = (watched | entry.ignore) - columns
        assert unknown == set(), f"{name}: not a column: {sorted(unknown)}"


def test_the_ready_topics_name_the_kinds_the_walk_knows():
    from app.jobsearch.work.session_kinds import SESSION_KINDS

    assert set(topics.READY_KINDS) == {k.name for k in SESSION_KINDS}


# --- what a change to a column posts ----------------------------------------------------------------------------------

#: A column's value to start from, where changing it posts only from one state (a rule with a `when`).
STARTS = {
    ("JobPosting", "filtered_at"): datetime(2026, 1, 1, tzinfo=UTC),
    ("JobPosting", "company"): "Added link",  # a job the owner added by its link, until something names it
    ("AgentRunner", "state"): "working",
    ("AgentConnection", "paused"): True,
    ("BrowserItem", "state"): "done",
    ("Project", "stopped_at"): datetime(2026, 1, 1, tzinfo=UTC),  # a paused project: resuming it is what posts
}
#: The value to change it to, where any other value would post nothing.
FLIPS = {
    ("JobPosting", "filtered_at"): None,
    ("AgentRunner", "state"): "idle",
    ("AgentConnection", "paused"): False,
    ("BrowserItem", "state"): "waiting",
    ("Project", "stopped_at"): None,
}
#: What else a row needs for the change to count.
ALSO = {
    ("BrowserItem", "next_at"): {"state": "waiting"},
    # a posting asks for the browser's look only through the role it became
    **{("JobPosting", c): {"opportunity_id": uuid.UUID(int=5)}
       for c in ("closed_at", "not_checked_at", "url", "apply_url")},
}

COLUMNS = [pytest.param(e.model, c, id=f"{e.model.__name__}.{c}") for e in WATCHED for c in sorted(e.watched_columns())]
IGNORED = [pytest.param(e.model, c, id=f"{e.model.__name__}.{c}")
           for e in WATCHED for c in sorted(e.ignore) if c not in ("id",)]


@pytest.mark.parametrize(("model", "column"), COLUMNS)
async def test_every_watched_column_posts_when_it_changes(js, model, column):
    key = (model.__name__, column)
    async with db.session_factory()() as session:
        await without_links(session)
        start = {column: STARTS[key]} if key in STARTS else {}
        row = blank(model, **ALSO.get(key, {}), **start)
        await saved(session, row)
        current = getattr(row, column)
        new = FLIPS[key] if key in FLIPS else a_value(model, column, current, different=True)
        setattr(row, column, new)
        await session.flush()
        assert await posted(session), f"changing {model.__name__}.{column} posted nothing"
        causes = set((await session.scalars(select(Todo.cause))).all())
        assert any(c.startswith(f"hook:{model.__name__}.") for c in causes)
        await session.rollback()


@pytest.mark.parametrize(("model", "column"), IGNORED)
async def test_changes_to_unwatched_columns_post_nothing(js, model, column):
    async with db.session_factory()() as session:
        await without_links(session)
        row = blank(model)
        await saved(session, row)
        setattr(row, column, a_value(model, column, getattr(row, column), different=True))
        await session.flush()
        assert await posted(session) == [], f"changing {model.__name__}.{column} posted"
        await session.rollback()


CREATED = [pytest.param(e.model, id=e.model.__name__) for e in WATCHED if e.created]
#: What a new row must say for its creation to post (a creation that posts only for some rows).
MADE_AS = {
    "ProcWait": {"kind": "agent"},
    "TrailEvent": {"event": "role_moved", "actor": "person", "link_id": uuid.UUID(int=7)},
    "MailMessage": {"state": "waiting"},
    "BrowserItem": {"state": "waiting", "kind": "read"},
    "HandOut": {"kind": "analysis", "lease_until": datetime(2026, 1, 1, tzinfo=UTC)},
}


@pytest.mark.parametrize("model", CREATED)
async def test_creating_a_watched_row_posts(js, model):
    async with db.session_factory()() as session:
        await without_links(session)
        session.add(blank(model, **MADE_AS.get(model.__name__, {})))
        await session.flush()
        assert await posted(session), f"creating {model.__name__} posted nothing"
        await session.rollback()


@pytest.mark.parametrize("model", [Employer, Site, JobPosting])
async def test_deleting_a_watched_row_posts_what_the_gates_and_the_browser_read(js, model):
    async with db.session_factory()() as session:
        await without_links(session)
        row = blank(model)
        await saved(session, row)
        await session.delete(row)
        await session.flush()
        assert ("browser.reconcile", "*") in set(await posted(session))
        await session.rollback()


async def test_a_write_that_changes_nothing_posts_nothing(js):
    async with db.session_factory()() as session:
        role = make_role(gate_facts={"state": "done", "read": ["a"]}, fit_breakdown={"asks": []})
        await saved(session, role)
        role.status = role.status
        role.gate_facts = {"state": "done", "read": ["a"]}  # an equal value of a JSON column is no change
        role.fit_breakdown = {"asks": []}
        role.company = role.company
        await session.flush()
        assert await posted(session) == []
        await session.rollback()


async def test_a_loaded_row_changed_in_a_later_transaction_posts_in_that_one(js):
    async with db.session_factory()() as session:
        role = make_role()
        session.add(role)
        await session.commit()
        role_id = role.id
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        role.description = "the posting's whole text, longer than before"
        await session.commit()
    async with db.session_factory()() as session:
        assert (queue.ROLE_SETTLE, str(role_id)) in await posted(session)


# --- the posts of the statuses and the employer -----------------------------------------------------------------------


async def _after(session, role, **changes):
    for name, value in changes.items():
        setattr(role, name, value)
    await session.flush()
    return set(await posted(session))


async def test_a_role_entering_the_deck_asks_for_its_employer_limit_its_browser_work_and_its_research(js):
    employer = uuid.uuid4()
    async with db.session_factory()() as session:
        await without_links(session)
        role = make_role(status=stages.FIRST_IMPRESSION, employer_id=employer)
        await saved(session, role)
        got = await _after(session, role, status=stages.SECOND_IMPRESSION)
        assert got >= {("role.settle", str(role.id)), ("deck.dirty", ""), ("browser.reconcile", str(role.id)),
                       ("employer.research", str(employer))}
        await session.rollback()


async def test_a_role_leaving_an_application_asks_for_its_employer_limit_and_not_its_browser_work(js):
    employer = uuid.uuid4()
    async with db.session_factory()() as session:
        await without_links(session)
        role = make_role(status=stages.APPLIED, employer_id=employer)
        await saved(session, role)
        got = await _after(session, role, status=stages.REJECTED)
        assert ("employer.limit", str(employer)) in got
        assert ("browser.reconcile", str(role.id)) not in got
        await session.rollback()


async def test_a_role_moving_between_stages_the_limit_never_reads_does_not_ask_for_it(js):
    async with db.session_factory()() as session:
        await without_links(session)
        role = make_role(status=stages.LANDING_ZONE, employer_id=uuid.uuid4())
        await saved(session, role)
        got = await _after(session, role, status=stages.CLEANSED_DATA)
        # Changed on purpose in phase 4: the browser's queue reads the stage inside the pre-apply stages too (a role in
        # Cleansed Data with no text is read in full, one in the Landing zone is not: `reader.to_read_in_full`), so a
        # move there asks for it as well. The limit still never reads it.
        assert {t for t, _ in got} == {"role.settle", "deck.dirty", "browser.reconcile"}
        await session.rollback()


@pytest.mark.parametrize("before", [stages.GATED_OUT, stages.READY_TO_LAUNCH, stages.REJECTED])
async def test_a_role_that_comes_back_into_play_asks_for_the_details_its_postings_lack(js, before):
    """`index.fill_role_details` takes the postings of roles before Ready to Launch, so a role that re-enters those
    statuses (a gate that no longer stops it, the owner's move, the employer limit giving it back) may have text owed
    that the old turn read on its next pass."""
    async with db.session_factory()() as session:
        await without_links(session)
        role = make_role(status=before, description="a long enough description " * 20)
        await saved(session, role)
        got = await _after(session, role, status=stages.FIRST_IMPRESSION)
        assert ("role.details", "") in got
        await session.rollback()


@pytest.mark.parametrize(("before", "after"), [
    (stages.SECOND_IMPRESSION, stages.APPLIED), (stages.SECOND_IMPRESSION, stages.GATED_OUT),
    (stages.LANDING_ZONE, stages.CLEANSED_DATA), (stages.FIRST_IMPRESSION, stages.SECOND_IMPRESSION)])
async def test_a_role_that_leaves_play_or_moves_within_it_asks_for_no_details(js, before, after):
    async with db.session_factory()() as session:
        await without_links(session)
        role = make_role(status=before, description="a long enough description " * 20)
        await saved(session, role)
        assert ("role.details", "") not in await _after(session, role, status=after)
        await session.rollback()


async def test_a_role_that_changes_employer_asks_for_the_limit_of_both(js):
    old, new = uuid.uuid4(), uuid.uuid4()
    async with db.session_factory()() as session:
        await without_links(session)
        role = make_role(employer_id=old)
        await saved(session, role)
        got = await _after(session, role, employer_id=new)
        assert {("employer.limit", str(old)), ("employer.limit", str(new)), ("employer.research", str(new))} <= got
        await session.rollback()


async def test_a_claim_renewed_posts_nothing_and_a_claim_taken_or_given_up_posts(js):
    async with db.session_factory()() as session:
        role = make_role(analysis_state="waiting")
        await saved(session, role)
        assert await _after(session, role, analysis_claimed_at=datetime.now(UTC)) >= {("role.settle", str(role.id))}
        await session.execute(Todo.__table__.delete())
        assert await _after(session, role, analysis_claimed_at=datetime.now(UTC) + timedelta(minutes=1)) == set()
        assert ("role.settle", str(role.id)) in await _after(session, role, analysis_claimed_at=None)
        await session.rollback()


async def test_a_role_that_becomes_waiting_and_unclaimed_is_ready_for_the_analyst(js):
    async with db.session_factory()() as session:
        role = make_role()
        await saved(session, role)
        assert ("ready.analysis", "") in await _after(session, role, analysis_state="waiting")
        await session.rollback()


async def test_a_role_whose_gate_facts_become_pending_is_ready_for_a_title_session(js):
    async with db.session_factory()() as session:
        role = make_role(gate_facts={"state": "done"})
        await saved(session, role)
        got = await _after(session, role, gate_facts={"state": "pending"})
        assert ("ready.titles", "") in got and ("role.settle", str(role.id)) in got
        await session.rollback()


async def test_a_new_role_with_thin_text_asks_for_its_details_and_one_in_an_application_for_its_limit(js):
    employer = uuid.uuid4()
    async with db.session_factory()() as session:
        await without_links(session)
        session.add(make_role(status=stages.APPLIED, employer_id=employer, description=None))
        await session.flush()
        assert {("role.details", ""), ("employer.limit", str(employer)), ("deck.dirty", "")} <= set(
            await posted(session))
        await session.rollback()


# --- postings ---------------------------------------------------------------------------------------------------------


async def test_a_new_posting_is_sifted_and_a_text_that_arrives_settles_its_role(js):
    role_id = uuid.uuid4()
    async with db.session_factory()() as session:
        await without_links(session)
        session.add(make_posting(opportunity_id=role_id))
        await session.flush()
        assert await posted(session) == [("posting.sift", "")]
        posting = (await session.scalars(select(JobPosting))).one()
        await session.execute(Todo.__table__.delete())
        posting.text = "the whole text of the posting"
        await session.flush()
        # and, since phase 4, the browser's queue: a role's own read text is no browser read
        assert set(await posted(session)) == {("role.settle", str(role_id)), ("role.details", ""),
                                              ("browser.reconcile", str(role_id))}
        await session.rollback()


async def test_a_posting_with_no_role_whose_text_arrives_settles_no_role(js):
    async with db.session_factory()() as session:
        posting = make_posting()
        await saved(session, posting)
        posting.text = "the whole text of the posting"
        await session.flush()
        assert await posted(session) == [("role.details", "")]
        await session.rollback()


async def test_a_posting_the_filter_lets_go_is_sifted_again_and_a_route_asks_for_roles(js):
    async with db.session_factory()() as session:
        posting = make_posting(filtered_at=datetime.now(UTC), filtered_by="a gate")
        await saved(session, posting)
        posting.filtered_at, posting.filtered_by = None, None
        await session.flush()
        assert set(await posted(session)) == {("posting.sift", ""), ("posting.roles", "")}
        await session.rollback()


@pytest.mark.parametrize("column", ["closed_at", "not_checked_at"])
async def test_a_posting_with_no_role_that_opens_again_is_sifted_and_asked_for_roles(js, column):
    """The old turn reads a posting again once it is open (`sift` takes the unfiltered, `make_roles` the filtered, both
    the ones with no role), and a posting with no role has none for the browser to reconcile."""
    async with db.session_factory()() as session:
        posting = make_posting(**{column: datetime(2026, 1, 1, tzinfo=UTC)})
        await saved(session, posting)
        setattr(posting, column, None)
        await session.flush()
        assert set(await posted(session)) == {("posting.sift", ""), ("posting.roles", "")}
        await session.rollback()


async def test_a_filtered_posting_with_no_role_that_opens_again_is_asked_for_roles_and_not_sifted(js):
    async with db.session_factory()() as session:
        posting = make_posting(closed_at=datetime(2026, 1, 1, tzinfo=UTC), filtered_at=datetime(2026, 1, 1, tzinfo=UTC))
        await saved(session, posting)
        posting.closed_at = None
        await session.flush()
        assert set(await posted(session)) == {("posting.roles", "")}
        await session.rollback()


@pytest.mark.parametrize("column", ["closed_at", "not_checked_at"])
async def test_a_posting_with_a_role_that_opens_again_asks_for_the_role_s_browser_look_and_for_its_details(js, column):
    """Changed on purpose in phase 2 (the work queue spec D1): it asked only for the browser's look, and
    now it asks for the details too. A posting that is open again may hold the text its role lacks
    (`index.fill_role_details` takes the open postings of roles in play), and the old turn read it on its next pass."""
    role_id = uuid.uuid4()
    async with db.session_factory()() as session:
        await without_links(session)
        posting = make_posting(opportunity_id=role_id, **{column: datetime(2026, 1, 1, tzinfo=UTC)})
        await saved(session, posting)
        setattr(posting, column, None)
        await session.flush()
        assert set(await posted(session)) == {("browser.reconcile", str(role_id)), ("role.details", "")}
        await session.rollback()


async def test_a_posting_with_a_role_that_is_still_not_checked_asks_for_no_details(js):
    role_id = uuid.uuid4()
    async with db.session_factory()() as session:
        await without_links(session)
        posting = make_posting(opportunity_id=role_id, closed_at=datetime(2026, 1, 1, tzinfo=UTC),
                               not_checked_at=datetime(2026, 1, 1, tzinfo=UTC))
        await saved(session, posting)
        posting.closed_at = None  # still Not checked: no stage reads its text (`is_open`)
        await session.flush()
        assert ("role.details", "") not in set(await posted(session))
        await session.rollback()


async def test_a_posting_linked_to_a_role_asks_for_the_details_that_role_lacks(js):
    """The filter's "already a role", `make_roles`, the scout's dedupe and an employer merge all link a posting to a
    role that is already there: that role's creation posted its details while no posting held text for it, and the
    link is the change that makes one owed (`index.fill_role_details` joins the posting to its role)."""
    role_id = uuid.uuid4()
    async with db.session_factory()() as session:
        await without_links(session)
        posting = make_posting()
        await saved(session, posting)
        posting.opportunity_id = role_id
        await session.flush()
        assert set(await posted(session)) == {("posting.roles", ""), ("role.details", ""),
                                              ("browser.reconcile", ""), ("pool.owed", "")}, (
            "and the browser's queue and the pool's pull, since phase 4 (the role may now have a copy to rank)")
        await session.rollback()


async def test_a_closed_posting_linked_to_a_role_asks_for_no_details(js):
    """Nothing reads a closed posting's text; if it opens again, that change posts (above)."""
    async with db.session_factory()() as session:
        await without_links(session)
        posting = make_posting(closed_at=datetime(2026, 1, 1, tzinfo=UTC))
        await saved(session, posting)
        posting.opportunity_id = uuid.uuid4()
        await session.flush()
        assert set(await posted(session)) == {("posting.roles", ""), ("browser.reconcile", ""), ("pool.owed", "")}, (
            "a closed posting's copy is the role's current one all the same (`cleansed._CURRENT`)")
        await session.rollback()


async def test_a_posting_loaded_without_its_text_and_linked_to_a_role_still_posts(js):
    """A board's read loads the known postings with their text deferred (`index.store`): the hook must not read it."""
    from sqlalchemy.orm import defer

    role_id = uuid.uuid4()
    async with db.session_factory()() as session:
        await without_links(session)
        posting = make_posting(text="x" * 400)
        await saved(session, posting)
        await session.commit()
        posting_id = posting.id
    async with db.session_factory()() as session:
        await without_links(session)  # before the read: the keys are set for a connection before its transaction
        posting = (await session.scalars(select(JobPosting).where(JobPosting.id == posting_id).options(
            defer(JobPosting.text)))).one()
        posting.opportunity_id = role_id
        await session.commit()
        assert ("role.details", "") in await posted(session)


async def test_a_posting_that_opens_but_is_still_not_checked_asks_for_no_sift_and_no_roles(js):
    async with db.session_factory()() as session:
        posting = make_posting(closed_at=datetime(2026, 1, 1, tzinfo=UTC),
                               not_checked_at=datetime(2026, 1, 1, tzinfo=UTC))
        await saved(session, posting)
        posting.closed_at = None  # still Not checked: no stage works it (`is_open`)
        await session.flush()
        assert await posted(session) == []
        await session.rollback()


async def test_a_posting_that_closes_asks_nobody_to_sift_it_or_make_it_a_role(js):
    async with db.session_factory()() as session:
        posting = make_posting()
        await saved(session, posting)
        posting.closed_at = datetime(2026, 1, 1, tzinfo=UTC)
        await session.flush()
        assert await posted(session) == []
        await session.rollback()


# --- the rest of D1 ---------------------------------------------------------------------------------------------------


async def test_a_role_the_owner_moved_by_hand_settles_that_role(js):
    role_id = uuid.uuid4()
    async with db.session_factory()() as session:
        await without_links(session)
        session.add(TrailEvent(owner=OWNER, actor="person", who="You", event="role_moved", sentence="moved",
                               link_kind="role", link_id=role_id))
        session.add(TrailEvent(owner=OWNER, actor="app", who="resumebot", event="role_moved", sentence="moved",
                               link_kind="role", link_id=uuid.uuid4()))
        session.add(TrailEvent(owner=OWNER, actor="person", who="You", event="note", sentence="a note"))
        await session.flush()
        assert set(await posted(session)) == {("role.settle", str(role_id)), ("deck.dirty", "")}
        await session.rollback()


async def test_a_saved_version_of_the_rules_or_an_instruction_file_says_which_kind_changed_and_asks_for_a_turn(js):
    """Changed on purpose (phase 3, the review of 2026-11-02): `rules.changed` is not posted (no handler), and the
    turn's steps read these documents (`mark_for_reads` the versions a research was made under, `rebalance` the
    Settings, the gate facts the pay floor), so the save also asks for the turn: the old loop picked a save up within 30
    seconds, and nothing else asks for a turn after one."""
    async with db.session_factory()() as session:
        session.add(ProfileVersion(owner=OWNER, kind="gates", version=1, data={}, saved_by="you"))
        session.add(InstructionFile(owner=OWNER, kind="instructions", name="jobsearch.scout", version=1, text="x",
                                    shipped_sha="s", saved_by="you"))
        await session.flush()
        assert set(await posted(session)) == {("role.settle", "*"), ("deck.dirty", ""), ("pool.owed", "")}
        await session.rollback()


@pytest.mark.parametrize("kind", [*profile_base.KINDS, *documents.DOCUMENT_KINDS])
async def test_a_saved_version_of_any_document_of_the_profile_asks_the_turn_to_look(js, kind):
    async with db.session_factory()() as session:
        session.add(ProfileVersion(owner=OWNER, kind=kind, version=1, data={}, saved_by="you"))
        await session.flush()
        got = set(await posted(session))
        await session.rollback()
    assert {("role.settle", "*"), ("deck.dirty", "")} <= got, "every kind of document is read by a step of the turn"


@pytest.mark.parametrize("kind", ["instructions", "templates"])
async def test_a_saved_version_of_an_instruction_file_asks_the_deck_to_look(js, kind):
    async with db.session_factory()() as session:
        session.add(InstructionFile(owner=OWNER, kind=kind, name="jobsearch.analyst", version=1, text="x",
                                    shipped_sha="s", saved_by="you"))
        await session.flush()
        got = set(await posted(session))
        await session.rollback()
    assert ("deck.dirty", "") in got, "the analyst's file is one a research was made under"


async def test_a_runner_going_idle_asks_the_dispatcher_and_a_heartbeat_does_not(js):
    async with db.session_factory()() as session:
        runner = AgentRunner(owner=OWNER, name="session-1", state="working")
        await saved(session, runner)
        runner.seen_at = datetime.now(UTC)
        await session.flush()
        assert await posted(session) == []
        runner.state = "idle"
        await session.flush()
        assert await posted(session) == [("dispatch.idle", "session-1")]
        await session.rollback()


async def test_a_resume_makes_every_kind_ready_and_a_pause_only_asks_the_pool(js):
    async with db.session_factory()() as session:
        connection = AgentConnection(owner=OWNER, slug="scout", name="Scout", paused=True)
        await saved(session, connection)
        connection.paused = False
        await session.flush()
        got = set(await posted(session))
        assert ("pool.owed", "") in got and {f"ready.{k}" for k in topics.READY_KINDS} <= {t for t, _ in got}
        await session.execute(Todo.__table__.delete())
        session.add(AgentKindPause(owner=OWNER, kind="writer"))
        await session.flush()
        assert await posted(session) == [("pool.owed", "")]
        await session.rollback()


async def test_a_hand_out_asks_for_its_kind_when_its_lease_runs_out(js):
    async with db.session_factory()() as session:
        session.add(HandOut(owner=OWNER, kind="analysis", items=["a"],
                            lease_until=datetime.now(UTC) + timedelta(hours=2)))
        await session.flush()
        row = (await session.scalars(select(Todo))).one()
        assert (row.topic, row.key) == ("ready.analysis", "")
        assert timedelta(hours=1, minutes=59) < row.not_before - row.posted_at <= timedelta(hours=2)
        await session.rollback()


async def test_a_browser_item_waiting_is_work_for_a_reader_and_a_map_for_a_pathfinder(js):
    async with db.session_factory()() as session:
        await without_links(session)
        session.add(blank(BrowserItem, kind="read", state="waiting", target="r1"))
        session.add(blank(BrowserItem, kind="map", state="waiting", target="m1"))
        session.add(blank(BrowserItem, kind="record", state="waiting", target="c1"))
        session.add(blank(BrowserItem, kind="read", state="done", target="r2"))
        await session.flush()
        assert sorted(await posted(session)) == [("ready.browser_reader", ""), ("ready.pathfinder", "")]
        await session.rollback()


async def test_a_job_made_asks_for_a_session_of_its_kind(js):
    async with db.session_factory()() as session:
        await without_links(session)
        session.add(blank(GapRun))
        await session.flush()
        assert await posted(session) == [("ready.gaps", "")]
        await session.rollback()


async def test_the_employer_a_person_gates_asks_for_research_the_gates_and_the_deck(js):
    async with db.session_factory()() as session:
        employer = make_employer()
        await saved(session, employer)
        employer.gated = True
        await session.flush()
        assert set(await posted(session)) == {("employer.research", str(employer.id)), ("deck.dirty", "")}
        await session.rollback()


async def test_a_mission_s_settings_ask_the_deck_and_the_research_missions_ask_the_employers_and_the_pool_too(js):
    """Found in the review of phase 3: `Mission.settings` was ignored, but the deck reads the research mission's
    again-after days (`deck.employers_for_deck`) and the pool reads both missions' targets, so an owner's change waited
    for the hour's clock. A change to the settings is a look at the deck, and at the employers when it is the research
    mission's."""
    from app.jobsearch.missions import record

    async with db.session_factory()() as session:
        research = blank(Mission, key=record.RESEARCH, settings={"again_after_days": 90})
        analyse = blank(Mission, key=record.ANALYSE, settings={"target": 5})
        await saved(session, research, analyse)
        research.settings = {"again_after_days": 30}
        await session.flush()
        assert set(await posted(session)) == {("deck.dirty", ""), ("employer.research", "*"), ("pool.owed", "")}
        await session.execute(Todo.__table__.delete())
        analyse.settings = {"target": 6}
        await session.flush()
        assert set(await posted(session)) == {("deck.dirty", ""), ("pool.owed", "")}, "no employer's research"
        await session.execute(Todo.__table__.delete())
        analyse.checked_at = datetime.now(UTC)  # the schedule's own look every five minutes: nothing to ask
        await session.flush()
        assert await posted(session) == []
        await session.rollback()


# --- phase 4: what the pull, the browser's queue and the walk read --------------------------------------------------


async def _changes(model, *, made=None, changes, start=None):
    """The to-dos of changing `changes` on a saved row of `model` (made with `made`, then `start` set)."""
    async with db.session_factory()() as session:
        await without_links(session)
        row = blank(model, **(made or {}), **(start or {}))
        await saved(session, row)
        for column, value in changes.items():
            setattr(row, column, value)
        await session.flush()
        got = set(await posted(session))
        await session.rollback()
    return got


async def test_a_parse_job_taken_or_ended_asks_the_pool_for_the_next_job_and_the_walk_for_its_kind(js):
    from app.jobsearch.models import ParseJob

    got = await _changes(ParseJob, start={"state": "waiting"}, changes={"state": "reading"})
    assert got == {("ready.parser", ""), ("pool.owed", "")}, "the one-ahead slot is free: the pull may start the next"


@pytest.mark.parametrize(("changes", "start"), [
    ({"analysis_state": "done"}, {"analysis_state": "waiting"}),
    ({"analysis_claimed_at": datetime(2026, 1, 1, tzinfo=UTC)}, {"analysis_state": "waiting"}),
    ({"sourced_at": datetime(2026, 1, 1, tzinfo=UTC)}, {}),
    ({"status": stages.LANDING_ZONE}, {"status": stages.GATED_OUT}),
])
async def test_a_change_to_the_research_line_or_what_can_be_researched_or_ranked_asks_the_pool(js, changes, start):
    got = await _changes(Opportunity, made={"status": stages.FIRST_IMPRESSION, **start}, changes=changes)
    assert ("pool.owed", "") in got, changes


async def test_a_role_moving_inside_the_stages_before_ready_asks_no_pull(js):
    got = await _changes(Opportunity, made={"status": stages.LANDING_ZONE}, changes={"status": stages.CLEANSED_DATA})
    assert ("pool.owed", "") not in got, "it was in play and it is in play: nothing new to rank or research"


async def test_an_employer_with_the_researcher_taken_or_changed_asks_the_pool(js):
    taken = await _changes(Employer, made={"research_state": "waiting"},
                           changes={"research_claimed_at": datetime(2026, 1, 1, tzinfo=UTC)})
    assert taken == {("pool.owed", "")}, "only the pool reads the claim (`queue_employers` one ahead)"
    done = await _changes(Employer, made={"research_state": "waiting"}, changes={"research_state": "done"})
    assert ("pool.owed", "") in done and any(topic == "employer.research" for topic, _ in done)


async def test_a_saved_document_asks_the_pool_since_every_rank_is_made_against_the_profile(js):
    async with db.session_factory()() as session:
        session.add(ProfileVersion(owner=OWNER, kind="resume", version=1, data={}, saved_by="you"))
        await session.flush()
        assert ("pool.owed", "") in await posted(session)
        await session.rollback()


@pytest.mark.parametrize(("column", "value"), [
    ("description", "the whole text of the posting"), ("analysis_state", "waiting"), ("go_check", "browser"),
    ("go_checked_at", datetime(2026, 1, 1, tzinfo=UTC))])
async def test_what_the_browser_reads_of_a_role_asks_for_its_queue(js, column, value):
    got = await _changes(Opportunity, made={"status": stages.READY_TO_LAUNCH}, changes={column: value})
    assert any(topic == "browser.reconcile" for topic, _ in got), column


async def test_a_role_made_in_the_stages_before_the_application_asks_for_its_browser_work(js):
    async with db.session_factory()() as session:
        await without_links(session)
        role = make_role(status=stages.LANDING_ZONE)
        session.add(role)
        await session.flush()
        assert ("browser.reconcile", str(role.id)) in await posted(session)
        await session.rollback()
    async with db.session_factory()() as session:
        await without_links(session)
        session.add(make_role(status=stages.REJECTED))
        await session.flush()
        assert not any(topic == "browser.reconcile" for topic, _ in await posted(session)), "an ended role is no work"
        await session.rollback()


@pytest.mark.parametrize("status", [stages.APPLIED, stages.ACTIVE_DISCUSSION])
async def test_a_role_made_in_applied_or_active_discussion_asks_for_its_browser_work(js, status):
    """Changed on purpose (2026-11-05, docs/PRD.md 32.6): a role the owner applied to or is in talks about that holds no
    text is sourced and read (`postings.unbuilt`), so one made there may be new work for the queue (it was none)."""
    async with db.session_factory()() as session:
        await without_links(session)
        role = make_role(status=status)
        session.add(role)
        await session.flush()
        assert ("browser.reconcile", str(role.id)) in await posted(session)
        await session.rollback()


@pytest.mark.parametrize(("was", "now"), [(stages.APPLIED, stages.ACTIVE_DISCUSSION),
                                          (stages.GONE_QUIET, stages.APPLIED)])
async def test_a_role_coming_into_applied_or_active_discussion_asks_for_its_browser_work(js, was, now):
    async with db.session_factory()() as session:
        await without_links(session)
        role = make_role(status=was)
        await saved(session, role)
        assert ("browser.reconcile", str(role.id)) in await _after(session, role, status=now)
        await session.rollback()


def test_the_stages_the_watch_list_calls_built_out_are_the_ones_the_queue_reads():
    from app.jobsearch.postings import unbuilt

    assert watch.BUILT_OUT == unbuilt.STAGES


async def test_what_the_browser_reads_of_a_posting_asks_for_its_queue(js):
    role_id = uuid.uuid4()
    for column, value in (("text", "the whole text"), ("detail_at", datetime(2026, 1, 1, tzinfo=UTC))):
        got = await _changes(JobPosting, made={"opportunity_id": role_id}, changes={column: value})
        assert ("browser.reconcile", str(role_id)) in got, column
    became = await _changes(JobPosting, changes={"opportunity_id": role_id})
    assert ("browser.reconcile", "") in became, "a posting that was a link's alone is no longer read by itself"


async def test_a_job_added_by_its_link_asks_for_its_read_and_loses_it_when_it_is_named(js):
    from app.jobsearch import ats
    from app.jobsearch.models import ADDED_LINK

    async with db.session_factory()() as session:
        await without_links(session)
        session.add(make_posting(ats=ats.OWNER, company=ADDED_LINK))
        await session.flush()
        assert ("browser.reconcile", "") in await posted(session)
        await session.rollback()
    got = await _changes(JobPosting, made={"ats": ats.OWNER, "company": ADDED_LINK},
                         changes={"company": "Fabrikam"})
    assert ("browser.reconcile", "") in got, "named: its item is no longer called for"
    got = await _changes(JobPosting, made={"company": "Fabrikam"}, changes={"company": "Fabrikam Inc"})
    assert not any(topic == "browser.reconcile" for topic, _ in got), "a name changed is nothing to the browser"


@pytest.mark.parametrize("state", ["done", "dropped", "stuck"])
async def test_an_item_that_leaves_the_work_asks_for_the_queue_to_be_looked_at_again(js, state):
    got = await _changes(BrowserItem, made={"kind": "read", "state": "leased"}, changes={"state": state})
    assert ("browser.reconcile", "") in got


async def test_a_scan_that_is_done_asks_for_the_queue_again_when_the_sites_scan_comes_due(js):
    from app.jobsearch.browser import queue as browser_queue

    async with db.session_factory()() as session:
        await without_links(session)
        row = blank(BrowserItem, kind="scan", state="leased", target="listings.example.test")
        await saved(session, row)
        row.state = "done"
        await session.flush()
        rows = (await session.scalars(select(Todo).where(Todo.topic == "browser.reconcile"))).all()
        due = sorted(r.not_before - r.posted_at for r in rows)
        await session.rollback()
    assert len(due) == 2 and due[0] < timedelta(seconds=1), "one now, for the queue the result changed"
    assert abs(due[1] - browser_queue.SCAN_EVERY) < timedelta(seconds=5), "and one when the scan is owed again"


# --- phase 4, found in review: what the pull and the browser's queue read that nothing asked for ----------------------


async def _made_to_post(session, **fields) -> set[tuple[str, str]]:
    await without_links(session)
    session.add(make_role(**fields))
    await session.flush()
    got = set(await posted(session))
    await session.rollback()
    return got


@pytest.mark.parametrize(("status", "asks"), [
    (stages.LANDING_ZONE, True), (stages.CLEANSED_DATA, True), (stages.FIRST_IMPRESSION, True),
    (stages.SECOND_IMPRESSION, True), (stages.READY_TO_LAUNCH, True), (stages.APPLIED, True),
    (stages.GATED_OUT, False), (stages.REJECTED, False)])
async def test_a_new_role_the_pool_may_rank_or_research_asks_it_and_one_out_of_play_does_not(js, status, asks):
    """`rank.unranked` takes any role before Ready to Launch whose postings have a current copy, and a role made from
    a listing that was copied before the role existed has one the moment it is made (the copy's own post is only for a
    posting that has a role): the role's creation is the change that makes it rankable. One made at Ready to Launch or
    later is one whose employer `employer.picks` may take."""
    async with db.session_factory()() as session:
        got = await _made_to_post(session, status=status)
    assert (("pool.owed", "") in got) is asks


@pytest.mark.parametrize("closed", [False, True])
async def test_a_posting_linked_to_a_role_asks_the_pool_since_the_role_may_now_have_a_current_copy(js, closed):
    """`cleansed._CURRENT` joins the posting's raw and cleansed copies to the role by the link alone, open or closed."""
    async with db.session_factory()() as session:
        await without_links(session)
        posting = make_posting(closed_at=datetime(2026, 1, 1, tzinfo=UTC) if closed else None)
        await saved(session, posting)
        posting.opportunity_id = uuid.uuid4()
        await session.flush()
        assert ("pool.owed", "") in set(await posted(session))
        await session.rollback()


async def test_a_posting_that_is_not_linked_to_a_role_asks_the_pool_for_nothing(js):
    async with db.session_factory()() as session:
        await without_links(session)
        posting = make_posting(opportunity_id=uuid.uuid4())
        await saved(session, posting)
        posting.opportunity_id = None
        await session.flush()
        assert ("pool.owed", "") not in set(await posted(session))
        await session.rollback()


@pytest.mark.parametrize("mark", [line.READ_NEXT, analysis.REANALYSE])
async def test_a_mark_to_read_a_role_next_or_research_it_again_asks_the_pool(js, mark):
    """`line.startable` reads the marks, which are in the `analysis` column that asked only the deck."""
    got = await _changes(Opportunity, made={"status": stages.FIRST_IMPRESSION, "analysis": {"claims": []}},
                         changes={"analysis": {"claims": [], mark: "2026-01-01"}})
    assert ("pool.owed", "") in got and ("deck.dirty", "") in got


async def test_findings_or_a_mark_taken_away_ask_the_pool_for_nothing_the_research_line_does_not(js):
    marked = {"claims": [], line.READ_NEXT: "2026-01-01"}
    got = await _changes(Opportunity, made={"status": stages.FIRST_IMPRESSION, "analysis": marked},
                         changes={"analysis": {"claims": [{"item": "x"}]}})
    assert ("pool.owed", "") not in got and ("deck.dirty", "") in got, "a mark that goes is no new work"


@pytest.mark.parametrize(("before", "after", "asks"), [
    (stages.FIRST_IMPRESSION, stages.SECOND_IMPRESSION, True),  # `line.startable`, `employer.picks`
    (stages.FIRST_IMPRESSION, stages.READY_TO_LAUNCH, True),  # `employer.picks`
    (stages.GATED_OUT, stages.APPLIED, True),
    (stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH, False),  # on deck and on deck
    (stages.APPLIED, stages.ACTIVE_DISCUSSION, False),
    (stages.SECOND_IMPRESSION, stages.FIRST_IMPRESSION, False)])
async def test_a_role_coming_into_the_stages_the_pool_researches_asks_it_and_one_moving_inside_them_does_not(
        js, before, after, asks):
    got = await _changes(Opportunity, made={"status": before}, changes={"status": after})
    assert (("pool.owed", "") in got) is asks


async def test_the_stages_the_pool_researches_are_the_ones_the_employer_research_reads():
    from app.jobsearch.research import employer

    assert watch.WORTH_RESEARCH == employer.WORTH_RESEARCH


async def test_a_role_that_changes_employer_asks_the_pool_which_employers_to_research(js):
    got = await _changes(Opportunity, made={"status": stages.SECOND_IMPRESSION, "employer_id": uuid.uuid4()},
                         changes={"employer_id": uuid.uuid4()})
    assert ("pool.owed", "") in got


async def test_an_employer_the_owner_stops_gating_asks_the_pool_and_one_the_owner_gates_does_not(js):
    freed = await _changes(Employer, made={"gated": True}, changes={"gated": False})
    assert ("pool.owed", "") in freed, "`employer.picks` leaves a gated employer out"
    gated = await _changes(Employer, made={"gated": False}, changes={"gated": True})
    assert ("pool.owed", "") not in gated


async def test_a_recording_asked_or_closed_asks_for_the_browsers_queue(js):
    """Found in the review of phase 4: `recipes/record.py::wanted` makes a `record` item of every recording asked, so
    the owner's ask waited for the hour's sweep (the old pass made it within 30 seconds)."""
    from app.sites.models import SiteRecording

    async with db.session_factory()() as session:
        await without_links(session)
        row = blank(SiteRecording, state="requested", reason="first")
        session.add(row)
        await session.flush()
        assert ("browser.reconcile", "") in set(await posted(session)), "asked"
        await session.rollback()
    got = await _changes(SiteRecording, made={"state": "requested"}, changes={"state": "failed"})
    assert ("browser.reconcile", "") in got, "given up: its item is no longer called for"
    got = await _changes(SiteRecording, made={"state": "requested"}, changes={"responses": 7, "note": "ok"})
    assert got == set(), "what the recording says of itself is read by no loop"


async def test_a_domain_that_is_stopped_asks_for_the_browsers_queue_and_a_pace_kept_does_not(js):
    """`host_stops.end_waiting` is part of every refresh: the waiting items of a stopped domain end with its reason."""
    from app.jobsearch.models import BrowserDomain

    stopped = await _changes(BrowserDomain, changes={"stopped_at": datetime(2026, 1, 1, tzinfo=UTC),
                                                     "stopped_for": "answered 403"})
    assert ("browser.reconcile", "") in stopped
    paced = await _changes(BrowserDomain, changes={"next_page_at": datetime(2026, 1, 1, tzinfo=UTC), "blocks": 2,
                                                   "cool_until": datetime(2026, 1, 2, tzinfo=UTC)})
    assert paced == set(), "the pace is read by the hand-out, never by the refresh"


async def test_a_site_project_the_owner_resumes_asks_for_a_scout_session_and_a_paused_or_finished_one_does_not(js):
    """Found by the reads check: `waiting_projects` reads the open projects, and `completion.resume_project` only clears
    the stop, so a resumed project's work waited for the walk's minute tick."""
    from app.sites.models import Project

    paused = datetime(2026, 1, 1, tzinfo=UTC)
    resumed = await _changes(Project, made={"stopped_at": paused}, changes={"stopped_at": None})
    assert resumed == {("ready.site", "")}
    stopped = await _changes(Project, changes={"stopped_at": paused, "stop_reason": "paused"})
    assert stopped == set(), "a stop takes work away"
    finished = await _changes(Project, changes={"finished_at": paused, "priority": 3})
    assert finished == set(), "a finish takes work away and the priority only orders it"


# --- what the deck reads of a Boardone role asks the deck to look (the lookup at the read-next mark, 2026-11-05) ----


@pytest.mark.parametrize(("column", "value"), [
    ("sourced_at", datetime(2026, 11, 5, tzinfo=UTC)), ("board_url", "https://www.boardone.test/jobs/view/1/"),
    ("url", "https://www.boardone.test/jobs/view/1/"), ("browser_note", "no posting on the careers site")])
async def test_what_decides_whether_a_boardone_role_is_looked_up_or_read_asks_the_deck(js, column, value):
    """A role found sourced is read next, not looked up; the host of its link says whether it is Boardone's; and a role
    the browser gave up on (its note) is neither marked nor counted (`deck._decide`)."""
    got = await _changes(Opportunity, made={"status": stages.FIRST_IMPRESSION}, changes={column: value})
    assert ("deck.dirty", "") in got, column


async def test_a_lookup_mark_put_on_or_taken_off_a_role_asks_for_its_browser_work_and_other_findings_do_not(js):
    """`models.SOURCE_NEXT` in the analysis is what makes a role in First Impression one the browser sources
    (`models.wants_sourcing`): the queue is looked at again when it goes on or off."""
    from app.jobsearch.models import SOURCE_NEXT

    made = {"status": stages.FIRST_IMPRESSION}
    on = await _changes(Opportunity, made={**made, "analysis": {"claims": []}},
                        changes={"analysis": {"claims": [], SOURCE_NEXT: True}})
    off = await _changes(Opportunity, made={**made, "analysis": {SOURCE_NEXT: True}}, changes={"analysis": {}})
    other = await _changes(Opportunity, made={**made, "analysis": {"claims": []}},
                           changes={"analysis": {"claims": [{"item": "x"}]}})
    assert any(topic == "browser.reconcile" for topic, _ in on)
    assert any(topic == "browser.reconcile" for topic, _ in off)
    assert not any(topic == "browser.reconcile" for topic, _ in other), "a finding is no change to the queue's work"
