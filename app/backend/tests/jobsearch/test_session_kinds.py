"""What a session can be started for (`session_kinds.py`): the table of kinds and its facts, and the pool's one
hand-out of them. This file is the module's test surface: `kind`, `of_runner`, `AGENT_KINDS`, and (as the walk moved
in) `hand_out`, `next_kind`, `heard`, `pull` and `saw`. Everything is made up."""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from process_engine.models import ProcInstance, ProcWait
from sqlalchemy import delete, select, update

from app import db
from app.agents import roster
from app.harness import instructions as files
from app.jobsearch import process, stages
from app.jobsearch.actors import BY_APP, BY_OWNER
from app.jobsearch.browser import reader as browser_reader
from app.jobsearch.doors import tools
from app.jobsearch.gaps import coach as gap_coach
from app.jobsearch.gaps import tools as gap_tools
from app.jobsearch.interviews import coach
from app.jobsearch.interviews import core as interviews
from app.jobsearch.jobs import check as role_check
from app.jobsearch.jobs import parse as role_parse
from app.jobsearch.mail import haiku as mail_haiku
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.missions import core as missions
from app.jobsearch.models import (
    BrowserItem,
    CheckJob,
    GapRun,
    HandOut,
    InterviewPrep,
    MailMessage,
    Opportunity,
    SearchRun,
    SeatRun,
)
from app.jobsearch.pool import core as pool
from app.jobsearch.pool import pull as pool_pull
from app.jobsearch.research import analysis, role_summary
from app.jobsearch.research import employer as employer_research
from app.jobsearch.stages import SECOND_IMPRESSION
from app.jobsearch.work import hand_outs, housekeeping, job_queue, session_kinds, tagging, wake
from app.jobsearch.work import hours as night
from app.jobsearch.work.session_kinds import AGENT_KINDS, SESSION_KINDS, kind, of_runner
from app.jobsearch.writing import packets, seats
from app.sites import projects, where
from app.sites import tools as site_tools
from app.sites.models import Site
from tests.builders import (
    analysis_setup,
    browser_world,
    next_item,
    parse_job,
    parse_runner,
    save_documents,
    settle_after_the_queue,
)
from tests.employer_builders import make_employer, make_role
from tests.support import DEFAULT_OWNER, OWNER, PROFILE, load_profile

pytestmark = pytest.mark.usefixtures("the_queue_is_made_before_a_hand_out")

#: The page's own order of the agent kinds (Agents, `pool.listing`).
PAGE_ORDER = ["learner", "pathfinder", "researcher", "writer", "browser_reader", "title_checker", "search"]


@pytest.mark.parametrize("row", SESSION_KINDS, ids=lambda r: r.name)
def test_every_session_kind_has_its_words_its_wake_and_an_agent_kind_to_pause_it_with(row):
    assert row.busy, "the words of 'The agent is busy ...'"
    keys = {a.key for a in AGENT_KINDS}
    if row.group is None:
        assert row.name in {"parser", "checker", "gaps"}, "only what the owner waits for at the screen is never paused"
    else:
        assert row.group in keys, "paused, run and listed with an agent kind that exists"
    assert kind(row.name) is row and of_runner(row.runner) is row


@pytest.mark.parametrize("row", SESSION_KINDS, ids=lambda r: r.name)
async def test_a_job_queued_behind_a_session_of_any_kind_says_what_it_is_busy_with_and_how_long_it_takes(js, row):
    """The mail reader had no words of its own (the queued line said "on another job")."""
    await parse_runner(seen=timedelta(minutes=1), checked_in=timedelta(minutes=1), kind=row.runner)
    async with db.session_factory()() as session:
        line = await job_queue.queued(session, OWNER)
    assert line is not None and f"the agent is busy {row.busy}" in line, line
    assert ("which takes about 3 minutes" in line) == (row.name == "analysis")


async def test_a_job_queued_behind_a_session_that_is_no_kind_says_it_is_on_another_job(js):
    await parse_runner(seen=timedelta(minutes=1), checked_in=timedelta(minutes=1), kind="reader")
    async with db.session_factory()() as session:
        assert "the agent is busy on another job" in (await job_queue.queued(session, OWNER) or "")


def test_every_session_kind_has_a_rung_in_the_order_and_an_adapter_that_answers_free_and_doing():
    assert {rung.kind for rung in session_kinds.ORDER} == {r.name for r in SESSION_KINDS}
    adapters = session_kinds._wire().adapters
    assert set(adapters) == {r.name for r in SESSION_KINDS}
    assert all(a.free is not None and a.doing is not None and a.batch >= 1 for a in adapters.values())


def test_every_agent_kind_pauses_some_session_kind_and_a_seat_is_paused_with_the_writer():
    assert {r.group for r in SESSION_KINDS if r.group is not None} == {a.key for a in AGENT_KINDS}
    assert kind("seat").group == "writer"


def test_the_kinds_are_told_apart_by_name_and_by_runner_word_and_an_unknown_word_is_none():
    assert len({r.name for r in SESSION_KINDS}) == len({r.runner for r in SESSION_KINDS}) == len(SESSION_KINDS) == 13
    assert kind("seat").runner == "writer" and of_runner("writer").name == "seat"
    assert kind("analysis").runner == "analyst" and of_runner("analyst").name == "analysis"
    assert kind("nonsense") is None and of_runner("nonsense") is None and of_runner("") is None
    assert of_runner("reader") is None, "the retired reader kind is no kind"


def test_the_agent_kinds_are_the_pages_seven_in_its_order_and_each_file_they_send_exists():
    assert [a.key for a in AGENT_KINDS] == PAGE_ORDER
    for a in AGENT_KINDS:
        assert a.label and a.purpose and a.files, a.key
        for folder, name in a.files:
            assert name in files.names(folder), (a.key, folder, name)


async def test_a_waiting_seat_with_no_other_knock_wakes_the_runner_with_the_packet_reason(js):
    """The schedule pass's own table of reasons had no entry for a seat, so a packet
    waiting with the runner between sessions raised a KeyError in the pass instead of knocking."""
    await analysis_setup()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        role = Opportunity(owner=OWNER, slug="globex-lead", company="Globex", title="Programs Lead", fit=5,
                           origin="scout", match_key="globex|programs lead", status="second_impression",
                           found=date.today(), url="https://jobs.example.test/1",
                           description="Lead programs for the board. " * 2)
        session.add(role)
        await session.flush()
        await packets.start(session, OWNER, role.id)
        await session.commit()
    async with db.session_factory()() as session:
        assert await missions.schedule_pass(session, OWNER) == [("waiting", kind("seat").wake)]
    assert kind("seat").wake == wake.PACKET_ASKED


# --- the pool's one order ---------------------------------------------------------------------------------------


async def _all_of_it_waits(*, search: bool = True) -> dict:
    """A search open, a role with the analyst, a packet for the writer and a posting the owner pasted to be split."""
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        await analysis.start(session, OWNER, await session.get(Opportunity, ids["Fabrikam"]))
        drafted = Opportunity(owner=OWNER, slug="globex-lead", company="Globex", title="Programs Lead", fit=5,
                              origin="scout", match_key="globex|programs lead", status="second_impression",
                                  found=date.today(),
                              url="https://jobs.example.test/1", description="Lead programs for the board. " * 2)
        session.add(drafted)
        await session.flush()
        await packets.start(session, OWNER, drafted.id)
        if search:
            session.add(SearchRun(owner=OWNER, status="open", started_by="manual"))
        await session.commit()
    return {"parse": await parse_job(), **ids}


async def _asks(count: int = 1, runner: str | None = None) -> list[str | None]:
    """A free pool session asks `count` times: what the next kind would be (a look, nothing is claimed)."""
    async with db.session_factory()() as session:
        out = [await session_kinds.next_kind(session, OWNER, runner) for _ in range(count)]
        await session.commit()
    return out


@pytest.mark.parametrize("why", ["paused", "peak", "usage"])
async def test_no_session_starts_anything_while_claude_cannot_be_used(js, monkeypatch, why):
    from app.jobsearch.work import availability
    from app.jobsearch.work import hours as night

    await _all_of_it_waits()
    async with db.session_factory()() as session:
        conn = await roster.ensure_roster(session, OWNER)
        conn.paused = why == "paused"
        if why == "usage":  # a session met Claude's usage limit (the heartbeat's)
            lifts = datetime.now(UTC) + timedelta(hours=2)
            await availability.limit_met(session, OWNER, {"resets_at": lifts.isoformat()})
        await session.commit()
    if why == "peak":
        monkeypatch.setattr(night, "in_peak", lambda s, now=None: True)
    assert await _asks(3) == [None] * 3
    async with db.session_factory()() as session:
        assert await session_kinds.hand_out(session, OWNER, "session-1") == {"next": None}


async def test_the_route_serves_the_pool_alone_and_refuses_any_other_name(js, client, scout_token):
    await _all_of_it_waits()
    get = client.get

    async def next_for(**params):
        """The kind the route names; the claim made with the answer is let go, so the next ask is about the same waiting
        work (what the route claims is tested in test_claimed_hand_out.py)."""
        kind_ = (await get("/api/v1/agent/work", params=params, headers=scout_token)).json()["next"]
        async with db.session_factory()() as session:
            await session.execute(delete(HandOut))
            await session.commit()
        return kind_

    assert await next_for() == "parser", "no lane is the pool"
    assert await next_for(only="any") == "parser", "and `any` is the same"
    assert await next_for(only="any", runner="session-1") == "parser"
    for bad in ("reader", "owner", "owner,reader", "search,any", "", " , "):
        refused = await get("/api/v1/agent/work", params={"only": bad}, headers=scout_token)
        assert refused.status_code == 422 and "only lane" in refused.json()["detail"], repr(bad)


async def _clear(*, parse=False, packet=False, search=False) -> None:
    from app.jobsearch.models import Packet, ParseJob

    async with db.session_factory()() as session:
        if parse:
            for job in (await session.scalars(select(ParseJob))).all():
                await session.delete(job)
        if packet:
            for row in (await session.scalars(select(Packet))).all():
                row.status = "ready"
                if row.instance_id:  # its process is what holds the seat
                    await process.engine().cancel(session, row.instance_id, "a test clears it")
        if search:
            for run_ in (await session.scalars(select(SearchRun))).all():
                run_.status = "done"
        await session.commit()


async def test_a_free_session_takes_the_owners_ask_then_the_packet_then_research_then_the_search(js):
    await _all_of_it_waits()
    assert await _asks() == [session_kinds.PARSER], "the owner's own ask first"
    await _clear(parse=True)
    assert await _asks() == [session_kinds.SEAT], "a packet to draft before research and the search"
    await _clear(packet=True)
    assert await _asks() == [session_kinds.ANALYSIS], "research of a sourced role before the search"
    async with db.session_factory()() as session:
        for role in (await session.scalars(select(Opportunity))).all():
            analysis.claim(role)
        await session.commit()
    assert await _asks() == [session_kinds.SEARCH], "the search last"


async def test_research_comes_before_title_checks_and_a_site_before_everything_but_the_owners_ask(js, monkeypatch):
    await _all_of_it_waits(search=False)
    await _clear(parse=True, packet=True)

    async def a_site_waits(session, owner, own=frozenset()):
        return [{"project": SimpleNamespace(id=uuid.uuid4()), "waiting": ["plan"], "open": 1}]

    async with db.session_factory()() as session:
        await tagging.start(session, OWNER)  # the one-off tagging is open, with roles to tag
        await session.commit()
    assert await _asks() == [session_kinds.ANALYSIS], "research before the title checks"
    async with db.session_factory()() as session:
        for role in (await session.scalars(select(Opportunity))).all():
            analysis.claim(role)
        await session.commit()
    assert await _asks() == [session_kinds.TITLES], "then the title checks"
    monkeypatch.setattr(site_tools, "waiting_projects", a_site_waits)
    assert await _asks() == [session_kinds.SITE], "site learning above research and packets"


async def test_no_job_is_handed_twice_and_one_session_searches_at_a_time(js):
    await _all_of_it_waits()
    await _clear(parse=True, packet=True)
    assert await _asks() == [session_kinds.ANALYSIS]
    async with db.session_factory()() as session:
        for role in (await session.scalars(select(Opportunity))).all():
            analysis.claim(role)  # the session took it
        await session.commit()
    assert await _asks() == [session_kinds.SEARCH]
    async with db.session_factory()() as session:
        await roster.note_runner(session, OWNER, {"runner": "session-1", "state": "started", "session_id": "scout-a"},
                                 kind=roster.SCOUT)
        await session.commit()
    assert await _asks() == [None], "a session is on the search: a second is not started beside it"


async def test_the_pool_is_idle_when_nothing_is_due(js):
    await _all_of_it_waits()
    await _clear(parse=True, packet=True, search=True)
    async with db.session_factory()() as session:
        for role in (await session.scalars(select(Opportunity))).all():
            analysis.claim(role)
        await session.commit()
    assert await _asks(2) == [None, None]


# --- Launch in Browser: a free pool session takes its packets first, at any hour (the owner, 2026-10-22) --------------


async def _launch_world(*, launched: bool, written: bool = False, slug: str = "globex-lead",
                        setup: bool = True) -> None:
    """A role with a packet being written (made as a mission makes it); `launched` marks it Launch's."""
    from datetime import UTC, datetime

    if setup:
        await analysis_setup()
    async with db.session_factory()() as session:
        role = Opportunity(owner=OWNER, slug=slug, company=f"Co {slug}", title="Programs Lead", fit=5,
                           origin="scout", match_key=f"{slug}|programs lead", status="ready_to_launch",
                           found=date.today(), url="https://jobs.example.test/1",
                           description="Lead programs for the board. " * 2)
        session.add(role)
        await session.flush()
        packet = await packets.start(session, OWNER, role.id)
        if launched:
            packets.mark_launched(packet)
        if written:
            packet.written_at = datetime.now(UTC)
        await session.commit()


async def test_a_pool_session_takes_a_launch_packet_in_peak_hours_by_day(js, monkeypatch):
    from app.jobsearch.work import hours as night

    monkeypatch.setattr(night, "in_peak", lambda *a, **k: True)  # the owner's busy hours: every model session waits
    monkeypatch.setattr(night, "in_off_hours", lambda *a, **k: False)  # and it is the day
    await _launch_world(launched=True)
    assert await _asks() == [session_kinds.SEAT], (
        "Launch is the owner's own action: a pool session takes it ahead of the busy hours")


async def test_the_pool_ignores_a_packet_nobody_asked_launch_for_in_peak_hours(js, monkeypatch):
    from app.jobsearch.work import hours as night

    monkeypatch.setattr(night, "in_peak", lambda *a, **k: True)
    await _launch_world(launched=False)
    assert await _asks() == [None], "the nightly packets keep to the pool's rules"
    async with db.session_factory()() as session:
        packet = (await session.scalars(select(packets.Packet))).one()
        packets.mark_launched(packet)
        await session.commit()
    assert await _asks() == [session_kinds.SEAT]


async def test_launch_waits_while_the_agents_are_paused_and_for_a_session_past_the_owners_limit(js, monkeypatch):
    from app.agents import roster
    from app.jobsearch.work import hours as night

    monkeypatch.setattr(night, "in_off_hours", lambda *a, **k: False)  # by day the limit holds a session back

    await _launch_world(launched=True)
    async with db.session_factory()() as session:
        await pool.set_active_sessions(session, OWNER, 1)
        await session.commit()
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER, "session-2") is None, "past the limit of one session"
        assert await session_kinds.next_kind(session, OWNER, "session-1") == session_kinds.SEAT
        conn = await roster.connection_by_slug(session, OWNER, roster.SCOUT)
        assert conn is not None
        conn.paused = True
        await session.commit()
    assert await _asks() == [None], "the owner's pause still stops every session"


async def test_launch_waits_for_a_restarts_drain_and_for_claudes_usage_limit(js):
    """Launch is the owner's own action, so peak hours do not hold it; the rest of the pool's hold does (the walk counts
    every reason that stands but the peak hours)."""
    from app.jobsearch.work import availability

    await _launch_world(launched=True)
    assert await _asks() == [session_kinds.SEAT]
    availability.drain(OWNER, True)
    assert await _asks() == [None], "the lanes are draining: not even Launch starts a session"
    availability.drain(OWNER, False)
    assert await _asks() == [session_kinds.SEAT]
    async with db.session_factory()() as session:
        lifts = datetime.now(UTC) + timedelta(hours=2)
        await availability.limit_met(session, OWNER, {"resets_at": lifts.isoformat()})
        await session.commit()
    assert await _asks() == [None], "Claude's usage limit holds Launch too: a session started now would only fail"


async def test_a_session_is_handed_launchs_packet_before_an_older_one_of_the_nightly_mission(js):
    await _launch_world(launched=False, slug="old-one")
    await _launch_world(launched=True, slug="new-one", setup=False)
    async with db.session_factory()() as session:
        ids = await seats.free(session, OWNER)
        assert ids
        taken = await session.get(ProcWait, ids[0])
        instance = await session.get(ProcInstance, taken.instance_id)
        packet = await session.get(packets.Packet, uuid.UUID(instance.data["packet"]))
        assert packet is not None and packets.launched(packet)


async def test_the_walk_reads_every_rung_of_the_order_launchs_too(js, monkeypatch):
    """Launch's rung is a rung like the rest (its gate is what puts it in front of the hold): taking it out of `ORDER`
    takes out Launch's packet and nothing else (the walk once skipped the first rung by its place, so this dropped the
    owner's ask for a posting to split), and putting it last still answers Launch in peak hours."""
    from app.jobsearch.work import hours as night

    launch = next(r for r in session_kinds.ORDER if r.gate == session_kinds.LAUNCH)
    rest = tuple(r for r in session_kinds.ORDER if r is not launch)
    await _all_of_it_waits()
    monkeypatch.setattr(session_kinds, "ORDER", rest)
    assert await _asks() == [session_kinds.PARSER], "the owner's ask is still the first rung left"
    await _clear(parse=True)
    await _launch_world(launched=True, slug="launched", setup=False)
    assert await _asks() == [session_kinds.SEAT], "the nightly packet is still handed out; Launch's rung is gone"
    monkeypatch.setattr(session_kinds, "ORDER", (*rest, launch))
    monkeypatch.setattr(night, "in_peak", lambda *a, **k: True)
    assert await _asks() == [session_kinds.SEAT], "Launch's rung answers in peak hours wherever it stands"
    monkeypatch.setattr(session_kinds, "ORDER", rest)
    assert await _asks() == [None], "and with no rung for it, peak hours hold everything"


async def test_a_summary_alone_is_not_handed_to_two_asks(js):
    """A summary alone (`role_summary`) was claimed as the text "summary", which nothing
    held back, so a second ask was handed it too and the second session found nothing to do."""
    from tests.jobsearch.test_role_summary import _role, _setup

    await _setup()
    role = await _role("ready", stages.READY_TO_LAUNCH)
    async with db.session_factory()() as session:
        assert (await role_summary.ask(session, OWNER, role.id)) == {"asked": True}
        await session.commit()
    async with db.session_factory()() as session:
        first = await session_kinds.hand_out(session, OWNER, "session-1")
        await session.commit()
    async with db.session_factory()() as session:
        second = await session_kinds.hand_out(session, OWNER, "session-2")
        await session.commit()
    assert first["next"] == session_kinds.ANALYSIS and first["item"]["id"] == "summary"
    assert second["next"] is None, "the summary is claimed: a second ask is not handed it too"


# --- the bugs the walk and the claim shared: the decision is the claim ------------------------------------------------


async def _asked_packet(session, slug: str, by: str) -> None:
    role = Opportunity(owner=OWNER, slug=slug, company=slug.split("-")[0].title(), title="Programs Lead", fit=5,
                       origin="scout", match_key=f"{slug}|programs lead", status="second_impression",
                       found=date.today(), url=f"https://jobs.example.test/{slug}",
                       description="Lead programs for the board. " * 2)
    session.add(role)
    await session.flush()
    await packets.start(session, OWNER, role.id, by=by)


async def test_a_seat_claim_is_the_seat_the_decision_saw(js, monkeypatch):
    """The walk chose a seat among the owner's own (by day, outside the nightly room),
    and the claim then read every seat, so with an older nightly seat and a newer one the owner asked for, the claim
    was the nightly one."""
    monkeypatch.setattr(night, "in_off_hours", lambda *a, **k: False)  # the day: only the owner's own seats go
    await analysis_setup()
    async with db.session_factory()() as session:
        await _asked_packet(session, "older-nightly", BY_APP)
        await session.commit()
    async with db.session_factory()() as session:
        await _asked_packet(session, "newer-asked", BY_OWNER)
        await session.commit()
    async with db.session_factory()() as session:
        owners = await seats.free(session, OWNER, scope="owners")
        everything = await seats.free(session, OWNER)
        assert owners and everything and owners[0] != everything[0], "a world of two"
        reply = await session_kinds.hand_out(session, OWNER)
        assert reply["next"] == "seat"
        assert reply["item"]["id"] == str(owners[0]), "the claim is the seat the decision saw: the owner's"


async def test_with_the_nights_budget_spent_a_browser_claim_is_an_item_the_owner_waits_on(js, monkeypatch):
    """In the off hours with the nightly budget spent, the pool hands out only what the owner started, and the claim
    is made on the same terms as the decision (the scope the walk chose), not on the hour alone. A role on deck is the
    backlog (the owner, 2026-11-04, PRD 52.9) and waits; the role the owner starred is theirs."""
    from app.jobsearch.browser import takeable
    from app.jobsearch.pool import budget
    from tests.builders import browser_world
    from tests.support import DEFAULT_OWNER

    await browser_world(extra_sites=False)

    async def spent(session, owner, now=None):
        return "the nightly budget is reached"

    monkeypatch.setattr(budget, "hold", spent)
    async with db.session_factory()() as session:
        role = Opportunity(owner=DEFAULT_OWNER, slug="board-1", company="Initech 1", title="Programs Director 1",
                           match_key="initech 1|programs director 1", origin="scout", status="second_impression",
                           fit=4, found=date.today(), url="https://listings.example.test/jobs/1")
        session.add(role)
        await session.commit()
    async with db.session_factory()() as session:
        await settle_after_the_queue(session, DEFAULT_OWNER)
        assert (await session_kinds.hand_out(session, DEFAULT_OWNER, "session-1"))["next"] is None, "the backlog waits"
        role = await session.get(Opportunity, role.id)
        role.favorited_at = datetime.now(UTC)
        await session.commit()
    async with db.session_factory()() as session:
        await settle_after_the_queue(session, DEFAULT_OWNER)
        reply = await session_kinds.hand_out(session, DEFAULT_OWNER, "session-1")
        assert reply["next"] == "browser_reader"
        row = await session.get(BrowserItem, uuid.UUID(reply["item"]["id"]))
        assert takeable.owners_item(row), "what the owner waits on, not the app's own scan"


async def test_the_one_off_tagging_is_handed_as_titles_only_with_room_and_no_search_open(js, monkeypatch):
    from app.jobsearch.pool import budget

    await _all_of_it_waits(search=False)
    await _clear(parse=True, packet=True)
    async with db.session_factory()() as session:
        for role in (await session.scalars(select(Opportunity))).all():
            analysis.claim(role)
        await tagging.start(session, OWNER)  # the one-off tagging is open, with roles to tag
        await session.commit()
    assert await _asks() == [session_kinds.TITLES], "in the off hours, with nothing else to do"
    async with db.session_factory()() as session:
        session.add(SearchRun(owner=OWNER, status="open", started_by="manual"))
        await session.commit()
    assert await _asks() == [session_kinds.SEARCH], "an open search is the scout's: the tagging waits beside it"
    async with db.session_factory()() as session:
        for run_ in (await session.scalars(select(SearchRun))).all():
            run_.status = "done"
        await session.commit()
    monkeypatch.setattr(night, "in_off_hours", lambda *a, **k: False)
    assert await _asks() == [None], "by day it is not handed out"
    monkeypatch.setattr(night, "in_off_hours", lambda *a, **k: True)

    async def spent(session, owner, now=None):
        return "the nightly budget is reached"

    monkeypatch.setattr(budget, "hold", spent)
    assert await _asks() == [None], "nor with the night's budget spent"


async def test_the_one_off_tagging_is_not_handed_out_when_the_owner_has_no_target_role_left(js):
    await _all_of_it_waits(search=False)
    await _clear(parse=True, packet=True)
    async with db.session_factory()() as session:
        for role in (await session.scalars(select(Opportunity))).all():
            analysis.claim(role)
        await tagging.start(session, OWNER)
        await session.commit()
    assert await _asks() == [session_kinds.TITLES]
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria={"company": "A small agency."})
        await session.commit()
    assert await _asks() == [None], "no target role is left to match the roles to: the app's own pass closes the job"


# --- heard: what a session's heartbeat does for its kind ---------------------------------------------------------


BEAT = "/api/v1/agent/heartbeat"


@pytest.mark.parametrize("word, hook", [("gaps", (gap_tools, "session_ended")),
                                        ("coach", (gap_coach, "session_ended"))])
async def test_a_failing_end_of_a_find_gaps_or_coach_session_does_not_fail_the_heartbeat(
        js, client, scout_token, monkeypatch, word, hook):
    """These two ran unwrapped in the heartbeat, so a raise there answered 500 and the
    runner's word about the session was lost (every other kind's part was in a savepoint and logged)."""
    async def boom(session, owner, now=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(hook[0], hook[1], boom)
    got = await client.post(BEAT, headers=scout_token, json={
        "session_id": f"{word}-aaaaaaaaaaaa", "state": "ended", "lane": "any", "runner": "session-1"})
    assert got.status_code == 200 and got.json() == {"ok": True, "stop": False}


@pytest.fixture
async def browser_client(js, monkeypatch, scout_token):
    """The app over ASGI, acting for the owner of the browser world."""
    from httpx import ASGITransport, AsyncClient

    from app.jobsearch.main import app
    from app.settings import settings

    monkeypatch.setattr(settings, "owner", DEFAULT_OWNER)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_the_heartbeat_tells_a_silent_browser_session_to_stop_and_lets_one_holding_an_item_finish(
        browser_client, scout_token, monkeypatch):
    from tests.builders import WORKER, WORKER_B, hand_back

    await browser_world(extra_sites=False)
    await _board_role(1)
    await _board_role(2, link="https://careers.northwind2.test/jobs/2")  # on a domain of its own
    got = await next_item(WORKER)  # the session holds an item
    beat = {"state": "alive", "lane": "any", "runner": "session-2"}

    async def stop(sid: str) -> bool:
        reply = await browser_client.post(BEAT, headers=scout_token, json={**beat, "session_id": sid})
        assert reply.status_code == 200
        return reply.json()["stop"]

    assert await stop(WORKER) is False, "it holds an item and nothing says stop"
    await next_item(WORKER_B)  # a second session, which then says nothing for longer than a lease
    async with db.session_factory()() as session:
        entry = next(r for r in await browser_reader.open_runs(session, DEFAULT_OWNER)
                     if r.started["session_id"] == WORKER_B)
        entry.started = {**entry.started, "last_call": (datetime.now(UTC) - timedelta(minutes=11)).isoformat()}
        await session.commit()
    assert await stop(WORKER_B) is True, "a session that handed nothing back, nor called, for a lease is told to stop"
    monkeypatch.setattr(night, "blackout", lambda s, now=None: "Claude's peak hours.")
    assert await stop(WORKER) is False, "in the blackout it still finishes the item it holds"
    await hand_back(WORKER, got["item"]["item_id"], "gone", note="the posting is gone")
    assert await stop(WORKER) is True, "and is told to stop once it holds none"


# --- pull: the app's own pass starts the next job for a free session ----------------------------------------------


async def _a_free_session_asks() -> None:
    """A free pool session asks and finds nothing queued (it is the ask that lets the app start a job for it)."""
    async with db.session_factory()() as session:
        assert (await session_kinds.hand_out(session, OWNER, "session-1")) == {"next": None}
        await session.commit()


async def test_pull_starts_one_job_for_a_free_session_that_asked_lately_and_none_without_the_ask(js):
    await analysis_setup()
    async with db.session_factory()() as session:
        assert await session_kinds.pull(session, OWNER) == 0, "no free session has asked: nothing is pulled for one"
    await _a_free_session_asks()
    async with db.session_factory()() as session:
        assert await session_kinds.pull(session, OWNER) == 1, "one job, for the session that is there"
        await session.commit()
        waiting = (await session.scalars(select(Opportunity).where(Opportunity.analysis_state == "waiting"))).all()
        assert len(waiting) == 1, "one at a time: a second is not queued behind it"
        assert await session_kinds.pull(session, OWNER) == 0, "what was started is queued now: nothing more is pulled"
    assert await _asks() == [session_kinds.ANALYSIS]


async def test_pull_starts_nothing_under_the_global_hold_or_a_hold_on_the_kind(js, monkeypatch):
    await analysis_setup()
    await _a_free_session_asks()
    async with db.session_factory()() as session:
        conn = await roster.ensure_roster(session, OWNER)
        conn.paused = True
        await session.commit()
    async with db.session_factory()() as session:
        assert await session_kinds.pull(session, OWNER) == 0, "the agents are paused"
        conn = await roster.ensure_roster(session, OWNER)
        conn.paused = False
        await session.commit()
    async with db.session_factory()() as session:
        # the researcher's own sessions were just handed nothing while research waits: that kind is held, and not
        # started again either
        await session_kinds.saw(session, OWNER, session_kinds.ANALYSIS, empty=True, sid="analyst-new")
        await session_kinds.pull(session, OWNER)
        await session.commit()
        queued = (await session.scalars(select(Opportunity).where(Opportunity.analysis_state == "waiting"))).all()
        assert queued == [], "a kind that was just handed nothing is not queued again"
        await session.execute(delete(SearchRun))  # the pool went on to the search, which the next ask would be told
        await session.commit()
    session_kinds.forget()
    await _a_free_session_asks()
    later = datetime.now(UTC) + session_kinds.POOL_ASK_STANDS + timedelta(seconds=1)
    with monkeypatch.context() as clock:
        clock.setattr(session_kinds, "utc_now", lambda: later)
        async with db.session_factory()() as session:
            assert await session_kinds.pull(session, OWNER) == 0, "the ask has gone stale: no free session is there"
    async with db.session_factory()() as session:
        assert await session_kinds.pull(session, OWNER) == 1, "with the hold gone and a session asking, it starts"


@pytest.mark.parametrize("why", ["past the limit", "paused", "launch"])
async def test_an_ask_the_limit_or_the_hold_turned_away_or_launch_answered_is_no_free_session_pull_starts_a_job_for(
        js, monkeypatch, why):
    """Only an ask that reaches the pool's order is a free session that is there to take what `pull` starts: one the
    owner's limit or the hold turned away, or one that Launch's packet answered, is not (the old `_pool_next` stamped
    the ask after those checks, not before)."""
    monkeypatch.setattr(night, "in_off_hours", lambda *a, **k: False)  # by day the limit holds a session back
    # three sourced roles that a started job would queue for research (and, for Launch, a packet the owner launched)
    await (_launch_world(launched=True) if why == "launch" else analysis_setup())
    async with db.session_factory()() as session:
        await pool.set_active_sessions(session, OWNER, 1)
        conn = await roster.ensure_roster(session, OWNER)
        conn.paused = why == "paused"
        await session.commit()
    async with db.session_factory()() as session:
        reply = await session_kinds.hand_out(session, OWNER, "session-3" if why == "past the limit" else "session-1")
        await session.commit()
    assert reply["next"] == (session_kinds.SEAT if why == "launch" else None), reply
    async with db.session_factory()() as session:
        conn = await roster.ensure_roster(session, OWNER)
        conn.paused = False
        await session.commit()
    async with db.session_factory()() as session:
        assert await session_kinds.pull(session, OWNER) == 0, f"nothing is pulled for a session that was {why}"
        await session.commit()
        assert (await session.scalars(select(Opportunity).where(Opportunity.analysis_state == "waiting"))).all() == []
    if why == "launch":
        return
    await _a_free_session_asks()
    async with db.session_factory()() as session:
        assert await session_kinds.pull(session, OWNER) == 1, "a session that reaches the order is there: it starts"


# --- the hand-out protocol, one test for every kind that has more than one thing to hand out -------------------------
#
# Every kind is handed out the same way (docs/PRD.md path 47): an ask is claimed one item (a batch, for the kinds that
# work in batches) at a time, two asks are never handed the same, and the session started for a claim gets exactly it.
# Each world below has two claimable units for the kind and nothing else waiting; `took` is the kind's own take-next
# call, as its session makes it.


@dataclass
class World:
    """`build` makes two units of work for the kind (and says their ids, in the order they are handed out, or None when
    the ids are only known once handed: batches). `took` is what the session's own take-next call returns, as ids."""

    kind: str
    build: Callable[[], Awaitable[list[set[str]] | None]]
    took: Callable[..., Awaitable[set[str]]]
    owner: str = OWNER
    #: How an id in the claim is told in the session's reply (the titles' items are shown by their short id).
    shown: Callable[[str], str] = str
    #: The kind's take-next call hands the job in progress to whichever session asks (a check, a Find Gaps run: one at a
    #: time), so a session that asks while another's job is working is told that one.
    single_flight: bool = False


async def _w_parser() -> list[set[str]] | None:
    return [{await parse_job(age=timedelta(minutes=2))}, {await parse_job()}]


async def _t_parser(session, claim: str, sid: str) -> set[str]:
    return {(await role_parse.take_next(session, OWNER, session_id=sid, claim=claim))["id"]}


async def _w_checker() -> list[set[str]] | None:
    ids = []
    async with db.session_factory()() as session:
        for age in (timedelta(minutes=2), timedelta(0)):
            job = CheckJob(owner=OWNER, scope="pursued", state=role_check.WAITING, created_at=datetime.now(UTC) - age,
                           looks={}, items=[{"ref": str(uuid.uuid4()), "title": "Title 1", "company": "Fabrikam"}],
                           answers={}, meta={"total": 1})
            session.add(job)
            await session.flush()
            ids.append({str(job.id)})
        await session.commit()
    return ids


async def _t_checker(session, claim: str, sid: str) -> set[str]:
    return {(await role_check.take_next(session, OWNER, session_id=sid, claim=claim))["id"]}


async def _w_gaps() -> list[set[str]] | None:
    ids = []
    async with db.session_factory()() as session:
        for title, age in (("Older", timedelta(minutes=2)), ("Newer", timedelta(0))):
            row = GapRun(owner=OWNER, role_id="builders", title=title, kind="role", state="waiting",
                         created_at=datetime.now(UTC) - age)
            session.add(row)
            await session.flush()
            ids.append({str(row.id)})
        await session.commit()
    return ids


async def _t_gaps(session, claim: str, sid: str) -> set[str]:
    return {(await gap_tools.task_reply(session, OWNER, session_id=sid, claim=claim))["id"]}


async def _w_titles() -> list[set[str]] | None:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={**PROFILE["settings"]})
        now = datetime.now(UTC)
        session.add_all([Opportunity(
            owner=OWNER, slug=f"role-{i}", company=f"Company {i}", title=f"Role Title {i}", status=SECOND_IMPRESSION,
            fit=3, match_key=f"company {i}|role title {i}", origin="scout", created_at=now + timedelta(seconds=i))
            for i in range(150)])
        await session.flush()
        await tagging.start(session, OWNER)
        await session.commit()
    return None  # two batches: the first 100 roles, then the other 50


async def _t_titles(session, claim: str, sid: str) -> set[str]:
    batch = await tools.titles_next_reply(session, OWNER, sid, claim=claim)
    return {p["id"] for p in batch["postings"]}


async def _w_analysis() -> list[set[str]] | None:
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        for name in ("Fabrikam", "Initech"):
            role = await session.get(Opportunity, ids[name])
            role.url = f"https://careers.{name.lower()}.example.test/jobs/1"
            await analysis.start(session, OWNER, role)
        await session.commit()
    return [{str(ids["Fabrikam"])}, {str(ids["Initech"])}]


async def _t_analysis(session, claim: str, sid: str) -> set[str]:
    role = await analysis.claim_next(session, OWNER, session_id=sid, claim_id=claim)
    return {str(role.id)} if role is not None else set()


async def _w_researcher() -> list[set[str]] | None:
    ids = []
    async with db.session_factory()() as session:
        for name in ("Aardvark", "Badger"):
            employer = await make_employer(session, name)
            session.add(make_role(employer, f"Lead {name}"))
            await session.flush()
            await employer_research.start(session, OWNER, employer)
            ids.append({str(employer.id)})
        await session.commit()
    return ids


async def _t_researcher(session, claim: str, sid: str) -> set[str]:
    employer = await employer_research.claim_next(session, OWNER, session_id=sid, claim=claim)
    return {str(employer.id)} if employer is not None else set()


async def _w_seat() -> list[set[str]] | None:
    await analysis_setup()
    async with db.session_factory()() as session:
        for slug in ("globex-lead", "initech-lead"):
            await _asked_packet(session, slug, BY_OWNER)
        await session.commit()
    return None  # the oldest asked first: known once handed


async def _t_seat(session, claim: str, sid: str) -> set[str]:
    taken = await seats.take(session, OWNER, sid, claim=claim)
    run_row = await session.get(SeatRun, uuid.UUID(taken["seat"]["id"])) if taken["seat"] else None
    return {str(run_row.wait_id)} if run_row is not None else set()


async def _w_coach() -> list[set[str]] | None:
    ids = []
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await roster.ensure_roster(session, OWNER)
        await save_documents(session)
        for n in (1, 2):
            role = Opportunity(
                owner=OWNER, slug=f"acme-lead-{n}", company=f"Acme {n}", title="Program Lead", fit=4, origin="scout",
                match_key=f"acme {n}|lead", status=stages.ACTIVE_DISCUSSION, found=date.today(),
                description="Lead the new program office. Plan the grant calendar. Report to the board.")
            session.add(role)
            await session.flush()
            interview = await interviews.add(
                session, OWNER, role.id, when=datetime.now(UTC) + timedelta(days=3), round_kind="hiring_manager",
                label="Team chat", people=[{"name": "Sam Ortiz", "title": "Director"}], notes="")
            prep = (await session.scalars(
                select(InterviewPrep).where(InterviewPrep.interview_id == interview.id))).one()
            prep.asked_at = datetime.now(UTC) - timedelta(minutes=10 - n)
            ids.append({str(prep.id)})
        await session.commit()
    return ids


async def _t_coach(session, claim: str, sid: str) -> set[str]:
    told = await coach.task_reply(session, OWNER, "task", None, session_id=sid, claim=claim)
    return {told["id"]} if told.get("id") else set()


async def _w_mail() -> list[set[str]] | None:
    async with db.session_factory()() as session:
        now = datetime.now(UTC)
        session.add_all([MailMessage(
            owner=OWNER, message_key=f"msg-{i:03d}", sender="Recruiter <hr@example.test>", sender_key="example.test",
            subject=f"A note {i}", day=date.today(), state="waiting", read_at=now + timedelta(seconds=i))
            for i in range(2 * mail_haiku.BATCH)])
        await session.commit()
    return None  # two batches of `mail_haiku.BATCH`


async def _t_mail(session, claim: str, sid: str) -> set[str]:
    return {str(r.id) for r in await mail_haiku.claim_batch(session, OWNER, session_id=sid, claim=claim)}


async def _project(session, url: str):
    site = await where.add(session, OWNER, url, "job_board")
    return await projects.create_project(
        session, OWNER, site.id, "Read it", "Its listings can be read", created_by="you")


async def _w_site() -> list[set[str]] | None:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await roster.ensure_roster(session, OWNER)
        one = await _project(session, "https://one.example.test/jobs")
        two = await _project(session, "https://two.example.test/jobs")
        await session.commit()
    return [{str(one.id)}, {str(two.id)}]


async def _t_site(session, claim: str, sid: str) -> set[str]:
    told = await site_tools.work_next(session, OWNER, claim, sid)
    return {p["project_id"] for p in told["projects"]}


async def _board_role(n: int, link: str | None = None) -> None:
    sourced = {"url": link, "source_url": link, "source_kind": "employer", "sourced_at": datetime.now(UTC),
               "analysis_state": analysis.NO_TEXT} if link else {"url": f"https://listings.example.test/jobs/{n}"}
    async with db.session_factory()() as session:
        session.add(Opportunity(
            owner=DEFAULT_OWNER, slug=f"role-{n}", company=f"Initech {n}", title=f"Programs Director {n}",
            match_key=match_key(f"Initech {n}", f"Programs Director {n}"), origin="scout", status="second_impression",
            fit=4, found=date.today(), **sourced))
        await session.commit()


async def _w_browser_reader() -> list[set[str]] | None:
    await browser_world(extra_sites=False)
    await _board_role(1)  # a posting to find (a source item, on the board's domain)
    await _board_role(2, link="https://careers.northwind2.test/jobs/2")  # a posting to read, on a domain of its own
    return None  # the queue's own order: known once handed


async def _t_browser(session, claim: str, sid: str) -> set[str]:
    got = await next_item(sid, claim=claim)
    return {got["item"]["item_id"]} if got.get("item") else set()


async def _w_pathfinder() -> list[set[str]] | None:
    await browser_world(extra_sites=False)
    async with db.session_factory()() as session:
        for host in ("jobs.otherboard.test", "jobs.thirdboard.test"):
            session.add(Site(owner=DEFAULT_OWNER, url=f"https://{host}/", kind="job_board", read_by="browser",
                             created_at=datetime.now(UTC)))
        await pool.pause(session, DEFAULT_OWNER, pool.BROWSER_READER)  # the board's own scan is not this kind's
        await session.commit()
    return None  # the two sites' map items


#: The kinds that lease what they hand out, whose take-next the ledger runs (`hand_outs.take_next`).
LEASED = (session_kinds.ANALYSIS, session_kinds.RESEARCHER, session_kinds.COACH, session_kinds.MAIL)

WORLDS = [
    World(session_kinds.PARSER, _w_parser, _t_parser),
    World(session_kinds.CHECKER, _w_checker, _t_checker, single_flight=True),
    World(session_kinds.GAPS, _w_gaps, _t_gaps, single_flight=True),
    World(session_kinds.TITLES, _w_titles, _t_titles, shown=lambda i: i[:8]),
    World(session_kinds.ANALYSIS, _w_analysis, _t_analysis),
    World(session_kinds.RESEARCHER, _w_researcher, _t_researcher),
    World(session_kinds.SEAT, _w_seat, _t_seat),
    World(session_kinds.COACH, _w_coach, _t_coach),
    World(session_kinds.MAIL, _w_mail, _t_mail),
    World(session_kinds.SITE, _w_site, _t_site),
    World(session_kinds.BROWSER_READER, _w_browser_reader, _t_browser, owner=DEFAULT_OWNER),
    World(session_kinds.PATHFINDER, _w_pathfinder, _t_browser, owner=DEFAULT_OWNER),
]


@pytest.mark.parametrize("world", WORLDS, ids=lambda w: w.kind)
async def test_work_left_waiting_with_the_runner_between_sessions_knocks_with_the_kinds_own_reason(js, world):
    """The schedule pass's backstop names a reason for each kind next_kind can answer, from the kind's own row (it had a
    table of its own, with no entry for a seat and a dead one for a packet)."""
    await world.build()
    async with db.session_factory()() as session:
        await load_profile(session, world.owner)
        await settle_after_the_queue(session, world.owner)
        await session.commit()
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, world.owner) == world.kind
        assert await missions.schedule_pass(session, world.owner) == [("waiting", kind(world.kind).wake)]


def _forbidden(name: str):
    async def refuse(*args, **kwargs):
        raise AssertionError(f"the ask ran {name}")
    return refuse


@pytest.mark.parametrize("world", WORLDS, ids=lambda w: w.kind)
async def test_every_kind_is_handed_out_by_claim_two_asks_never_get_one_item_and_the_session_gets_its_claim(
        js, monkeypatch, world):
    from app.jobsearch.browser import leases as browser_leases
    from app.jobsearch.browser import queue as browser_queue
    from app.jobsearch.gaps import drafts as gap_drafts
    from app.jobsearch.gaps import runs as find_gaps
    from app.jobsearch.jobs import check as rc
    from app.jobsearch.missions import catch_up

    kind_name, owner = world.kind, world.owner
    row = kind(kind_name)
    assert row is not None
    units = await world.build()
    async with db.session_factory()() as session:
        if kind_name != session_kinds.TITLES:  # the roles these worlds make are matched already: no title job starts
            await session.execute(update(Opportunity).where(Opportunity.owner == owner).values(matched_by="haiku"))
        await settle_after_the_queue(session, owner)  # the queue as browser.reconcile makes it, then the app's pass
        await session.commit()

    def sid(letter: str) -> str:
        return f"{row.runner}-{letter * 12}"

    async def ask(runner: str) -> dict:
        async with db.session_factory()() as session:
            reply = await session_kinds.hand_out(session, owner, runner)
            await session.commit()
            return reply

    def ids(reply: dict) -> set[str]:
        return {world.shown(i) for i in reply["item"]["ids"]}

    async def take(reply: dict, letter: str) -> set[str]:
        async with db.session_factory()() as session:
            got = await world.took(session, reply["claim"], sid(letter))
            await session.commit()
            return got

    # (i) two asks are never handed the same item, and the second is handed the next; (vi) an ask settles, expires and
    # starts nothing
    with monkeypatch.context() as asking:
        for module, name in ((pool_pull, "start_next"),
                             (catch_up, "hand_on"), (tagging, "settle"), (rc, "expire"), (role_parse, "expire"),
                             (browser_leases, "expire"), (browser_queue, "refresh"), (find_gaps, "expire"),
                             (gap_drafts, "expire")):
            asking.setattr(module, name, _forbidden(f"{module.__name__}.{name}"))
        first, second = await ask("session-1"), await ask("session-2")
    assert first["next"] == second["next"] == kind_name, (first, second)
    assert ids(first) and ids(second) and not ids(first) & ids(second), "each ask is handed an item of its own"
    if units is not None:
        assert ids(first) == units[0] and ids(second) == units[1], "the oldest first, then the next"
    # (ii) a third ask finds everything claimed
    assert (await ask("session-5"))["next"] is None, "both are claimed: nothing is left to hand out"
    # (iv) a claimed item goes first even when an older one is free: the first ask's claim is given back, so the
    # older item is free again, and the second ask's session still gets its own
    async with db.session_factory()() as session:
        assert await hand_outs.release(session, owner, first["claim"])
        await session.commit()
    assert await take(second, "b") == ids(second), "its own claim, not the older item that is free"
    # (iii) and (v): the given-back item is handed again, a claim nobody takes lapses with its lease and is handed
    # again, and the session that gets it takes exactly it
    third = await ask("session-3")
    assert third["next"] == kind_name and ids(third) == ids(first)
    async with db.session_factory()() as session:
        lapsed = await session.get(HandOut, uuid.UUID(third["claim"]))
        lapsed.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    fourth = await ask("session-4")
    assert fourth["next"] == kind_name and ids(fourth) == ids(first), "a lapsed claim is handed again"
    if not world.single_flight:
        assert await take(fourth, "c") == ids(first), "its session takes exactly its claim"


async def test_a_site_session_with_no_claim_is_told_no_project_while_every_project_is_claimed_for_another_session(js):
    """What a site session's own take-next call (`site_tools.work_next`) is told: only the project claimed for it, and
    nothing at all when it names no claim and both projects are claimed for the other sessions (the claim is the
    reservation: a session that is not the one it was made for is not handed the item)."""
    await _w_site()
    async with db.session_factory()() as session:
        first, second = await session_kinds.hand_out(session, OWNER, "session-1"), await session_kinds.hand_out(
            session, OWNER, "session-2")
        await session.commit()
        assert [first["next"], second["next"]] == [session_kinds.SITE, session_kinds.SITE]
        assert (await session_kinds.hand_out(session, OWNER, "session-3"))["next"] is None, "both are claimed"
        assert (await site_tools.work_next(session, OWNER))["projects"] == [], "a session with no claim is told none"
        told = await site_tools.work_next(session, OWNER, second["claim"], "scout-b")
        assert [p["project_id"] for p in told["projects"]] == [second["item"]["id"]], "its own, and only that"


@pytest.mark.parametrize("world", [w for w in WORLDS if w.kind in LEASED], ids=lambda w: w.kind)
async def test_a_session_decides_what_it_takes_after_an_ask_that_is_claiming_its_kind_has_committed(
        js, world, monkeypatch):
    """An ask and a session's own take-next call never choose side by side (the ledger's `decide`): the database has
    one writer, so while an ask has claimed the first item and not committed, a session that names no claim is not given
    the turn in time and is told the app is busy (`Busy`), taking nothing; once the ask has committed, it takes an item
    the ask did not."""
    import asyncio

    from app.errors import Busy

    kind_name, owner = world.kind, world.owner
    await world.build()
    async with db.session_factory()() as session:
        await housekeeping.settle(session, owner)
        await session.commit()
    monkeypatch.setattr(db, "LOCK_TIMEOUT_MS", 100)

    async def take(tag: str):
        async with db.session_factory()() as taking:
            got = await world.took(taking, "", "any-" + tag * 12)
            await taking.commit()
            return got

    async with db.session_factory()() as asking:
        first = await session_kinds.hand_out(asking, owner, "session-1")  # claimed, and not committed yet
        assert first["next"] == kind_name, first
        with pytest.raises(Busy):
            await asyncio.wait_for(asyncio.ensure_future(take("a")), 3)
        await asking.commit()
    got = await take("b")
    claimed = {world.shown(i) for i in first["item"]["ids"]}
    assert got and not got & claimed, "with the ask committed it takes an item that is not the ask's"
