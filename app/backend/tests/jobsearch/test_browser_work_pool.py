"""The browser's workers in the session pool (the browser work spec): no fixed number of them (a free session takes
browser work while some domain with waiting items is unworked), the pool's order (the owner's asks, then map, then the
worker's source, read and scan, then the rest as before), a restart's drain, the Agents page and the strip, and Run now
and Wake up. The queue itself is test_browser_work.py. Every name and address is made up; these tests run on the real
clock, as the pool does."""

import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app import db
from app.agents import roster
from app.jobsearch.browser import pace as bp
from app.jobsearch.browser import reader as br
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.missions import core as missions
from app.jobsearch.models import HandOut, MailMessage, Opportunity
from app.jobsearch.pool import core as pool
from app.jobsearch.research import analysis
from app.jobsearch.work import availability, session_kinds, session_lifecycle
from app.jobsearch.work import hours as night
from app.models.agent import AgentRunner
from app.sites.models import Site
from tests.builders import (
    ONE,
    WORKER,
    WORKER_B,
    browser_world,
    hand_back,
    items,
    make_the_queue,
    mission_of,
    next_item,
    settle_after_the_queue,
)
from tests.support import DEFAULT_OWNER as OWNER
from tests.support import committed, forget_asks, learn_sites

pytestmark = pytest.mark.usefixtures("the_queue_is_made_before_a_hand_out")

KINDS = "/api/v1/agent-kinds"


@pytest.fixture
async def client(js, monkeypatch):
    """The app over ASGI, acting for the owner of the browser world (`browser_world`)."""
    from app.jobsearch.main import app
    from app.settings import settings

    monkeypatch.setattr(settings, "owner", OWNER)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _role(n: int, link: str | None = None, **fields) -> uuid.UUID:
    """A Found role found on the board `ONE`; with `link`, a Sourced one whose own posting is there, text unread."""
    sourced = {"url": link, "source_url": link, "source_kind": "employer", "sourced_at": datetime.now(UTC),
               "analysis_state": analysis.NO_TEXT} if link else {}
    async with db.session_factory()() as session:
        role = Opportunity(**{
            "owner": OWNER, "slug": f"role-{n}", "company": f"Initech {n}", "title": f"Programs Director {n}",
            "match_key": match_key(f"Initech {n}", f"Programs Director {n}"), "origin": "scout",
                "status": "second_impression",
            "found": date(2026, 10, 9), "url": f"https://www.{ONE}/jobs/view/{1000 + n}", "sourced_at": None,
            **sourced, **fields})
        session.add(role)
        await session.commit()
        return role.id


async def _next(kind: str) -> bool:
    """Whether a free session of the pool is handed a browser job of this kind now. The ask claims the job's item (path
    47): the claim holds the item and its domain until the session it was made for leases it."""
    async with db.session_factory()() as session:
        await settle_after_the_queue(session, OWNER)  # the queue as `browser.reconcile` makes it, then the app's pass
        taken = await session_kinds.next_kind(session, OWNER) == kind
        if taken:
            await session_kinds.hand_out(session, OWNER)  # the ask claims the job's item
        await session.commit()
        return taken


async def _work() -> str | None:
    """The kind of the next hand-out for a free session, claimed as the ask claims it."""
    async with db.session_factory()() as session:
        await settle_after_the_queue(session, OWNER)  # the queue as `browser.reconcile` makes it, then the app's pass
        kind = (await session_kinds.hand_out(session, OWNER))["next"]
        await session.commit()
        return kind


async def _beat(body: dict) -> bool:
    async with db.session_factory()() as session:
        stop = await session_lifecycle.settle(session, OWNER, body)
        await session.commit()
        return stop


# --- how many workers -------------------------------------------------------------------------------------------------


async def test_a_free_session_takes_browser_work_while_a_domain_with_waiting_items_is_unworked(js):
    await browser_world(extra_sites=False)
    await _role(1)  # the board's domain
    await _role(2, link="https://careers.northwind2.test/jobs/2")  # a domain of its own
    await _role(3, link="https://careers.northwind3.test/jobs/3")  # and another
    taken = [await _next(pool.BROWSER_READER) for _ in range(4)]
    assert taken == [True, True, True, False], "three domains with work: three sessions, no fixed number"


async def test_a_claimed_item_holds_its_domain_and_the_session_it_was_claimed_for_is_handed_it_first(js):
    await browser_world(extra_sites=False)
    await _role(1)
    assert await _next(pool.BROWSER_READER) is True
    assert await _next(pool.BROWSER_READER) is False, "one domain, one session handed out: the claim holds it"
    assert (await next_item(WORKER_B))["stop"] is True, "a session that names no claim is not handed the claimed item"
    async with db.session_factory()() as session:
        row = (await session.scalars(select(HandOut))).one()
    got = await next_item(WORKER, claim=str(row.id))
    assert got["item"]["item_id"] == row.items[0], "the session it was claimed for is handed that item"
    assert await _next(pool.BROWSER_READER) is False, "its domain is worked now"


async def test_a_paused_kind_or_a_paused_mission_takes_nothing(js):
    await browser_world(resumed=False, extra_sites=False)
    await _role(1)
    assert await _next(pool.BROWSER_READER) is False, "the mission is paused"
    async with db.session_factory()() as session:
        await missions.resume(session, await mission_of(session, OWNER, missions.BROWSER), by="Tester")
        await session.commit()
    assert await _next(pool.BROWSER_READER) is True
    async with db.session_factory()() as session:
        await session.execute(delete(HandOut))  # the session it was claimed for never came
        await session.commit()
    async with db.session_factory()() as session:
        await pool.pause(session, OWNER, pool.BROWSER_READER)
        await session.commit()
    assert await _next(pool.BROWSER_READER) is False


async def test_the_pathfinder_is_handed_out_only_for_map_items(js):
    await browser_world(extra_sites=False)
    assert await _next(pool.PATHFINDER) is False, "every site has its runbook"
    async with db.session_factory()() as session:
        session.add(Site(owner=OWNER, url="https://jobs.otherboard.test/", kind="job_board", read_by="browser",
                         created_at=datetime.now(UTC)))
        await session.commit()
    assert await _next(pool.PATHFINDER) is True


# --- the pool's order -------------------------------------------------------------------------------------------------


async def test_map_comes_before_the_workers_items_and_both_before_site_learning_and_packets(js):
    await browser_world(learned=False, extra_sites=False)
    await _role(1, description="A director of programs leads the delivery of the portfolio. " * 12,
                    analysis_state=analysis.WAITING)  # on deck: waiting for its research
    await _role(2, link="https://careers.northwind2.test/jobs/2")  # a posting to read, on a domain of its own
    assert await _work() == session_kinds.PATHFINDER, "map first"
    assert await _work() == session_kinds.BROWSER_READER, (
        "then the worker's item: the map's claim holds the board's domain")


async def test_the_owners_asks_come_before_the_browser(js, monkeypatch):
    from app.jobsearch.jobs import parse as role_parse

    await browser_world(extra_sites=False)
    await _role(1)

    async def asked(session, owner):
        return SimpleNamespace(id=uuid.uuid4())  # a job waits; the ask claims it

    monkeypatch.setattr(role_parse, "waiting", asked)
    assert await _work() == session_kinds.PARSER


async def _mail(count: int) -> None:
    """Messages the rules could not settle, waiting for the mail reader."""
    async with db.session_factory()() as session:
        now = datetime.now(UTC)
        session.add_all([MailMessage(
            owner=OWNER, message_key=f"msg-{i:03d}", sender="Recruiter <hr@example.test>", sender_key="example.test",
            subject=f"A note {i}", day=date.today(), state="waiting", read_at=now + timedelta(seconds=i))
            for i in range(count)])
        await session.commit()


@pytest.mark.parametrize("by_day", [False, True], ids=["off hours", "by day"])
async def test_the_owners_mail_comes_before_a_browser_backlog_at_every_check_in(js, monkeypatch, by_day):
    """A Read the mail run left two messages waiting, and for hours every lane
    that finished a session was handed the browser (a Followup-needed chase had queued many items), because
    the browser's rung stood above the mail's. The mail is the owner's and takes a short session: it is handed out
    ahead of the browser at any hour, and the backlog is worked on once it is claimed. By day the backlog is not handed
    out at all (PRD 52.9), so the browser's work then is the owner's own: roles they starred."""
    if by_day:
        monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: False)
    await browser_world(extra_sites=False)
    for n in range(1, 4):  # each role's own posting is to be read, on three domains
        await _role(n, link=f"https://careers.northwind{n}.test/jobs/{n}",
                    **({"favorited_at": datetime.now(UTC)} if by_day else {}))
    assert await _next(pool.BROWSER_READER) is True, "the browser has work for a free session"
    async with db.session_factory()() as session:
        await session.execute(delete(HandOut))  # the session it was claimed for never came
        await session.commit()
    await _mail(2)
    for check_in in range(3):  # lane after lane, check-in after check-in: the mail is offered until it is claimed
        async with db.session_factory()() as session:
            assert await session_kinds.next_kind(session, OWNER) == session_kinds.MAIL, check_in
    assert await _work() == session_kinds.MAIL, "claimed for a session"
    assert await _work() == session_kinds.BROWSER_READER, "and the backlog goes on once the mail is claimed"


# --- a restart drains the workers ---------------------------------------------------------------------------------


async def test_a_drain_hands_out_nothing_and_each_worker_finishes_the_item_it_holds(js, client, tmp_path, monkeypatch):
    from app.settings import settings

    monkeypatch.setattr(settings, "agent_token_file", str(tmp_path / "secrets" / "agent-token"))
    await browser_world(extra_sites=False)
    await _role(1)
    await _role(2, link="https://careers.northwind2.test/jobs/2")
    got = await next_item(WORKER)
    token = "drain-token"
    path = settings.token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    drained = (await client.post("/api/v1/agent/browser/drain", json={"on": True},
                                 headers={"Authorization": f"Bearer {token}"})).json()
    assert drained == {"ok": True, "draining": True, "holding": 1}
    assert await _work() is None, "nothing new starts"
    assert await _next(pool.BROWSER_READER) is False
    other = await next_item(WORKER_B)
    assert other["stop"] is True and "restarting" in other["why"]
    still = await next_item(WORKER)
    assert still["stop"] is True and "Hand back the item you hold first" in still["next"]
    reply = await hand_back(WORKER, got["item"]["item_id"], "not_found", note="no posting on the careers site")
    assert "restarting" in reply["next"]
    state = (await client.get("/api/v1/agent/browser/drain", headers={"Authorization": f"Bearer {token}"})).json()
    assert state["holding"] == 0
    await _beat({"session_id": "runner-start", "state": "boot", "runner": "session-1", "boot": "abcd1234"})
    assert not availability.draining(OWNER), "the lanes' next start ends it"


async def test_the_drain_route_needs_the_agent_token(js, client):
    assert (await client.post("/api/v1/agent/browser/drain", json={"on": True})).status_code == 401


# --- what keeps the app's Chrome open (docs/PRD.md 52.9: the bridge asks before it closes Chrome) --------------------


async def _busy(client, headers: dict) -> dict:
    """What the bridge is told the browser queue holds, after the queue was made and the app's own pass ran."""
    async with db.session_factory()() as session:
        await settle_after_the_queue(session, OWNER)
        await session.commit()
    reply = await client.get("/api/v1/agent/browser/busy", headers=headers)
    assert reply.status_code == 200
    return reply.json()


async def test_the_busy_route_needs_the_agent_token(js, client):
    assert (await client.get("/api/v1/agent/browser/busy")).status_code == 401


async def test_nothing_waits_or_is_leased_when_the_queue_is_empty(js, client, scout_token, monkeypatch):
    from app.jobsearch.work import hours as night

    await browser_world(extra_sites=False)
    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: False)  # by day, only the scheduled scan exists
    assert await _busy(client, scout_token) == {"ok": True, "waiting": 0, "leased": 0}


async def test_a_scan_waiting_for_the_night_is_not_work_for_chrome_but_is_in_the_off_hours(
        js, client, scout_token, monkeypatch):
    from app.jobsearch.work import hours as night

    await browser_world(extra_sites=False)
    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: False)
    assert (await _busy(client, scout_token))["waiting"] == 0, "scheduled: taken only in the owner's off hours"
    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: True)
    assert (await _busy(client, scout_token))["waiting"] == 1, "the night's scan of the board"


async def test_a_starred_roles_posting_counts_at_any_hour_and_an_item_a_worker_holds_is_leased(
        js, client, scout_token, monkeypatch):
    from app.jobsearch.work import hours as night

    await browser_world(extra_sites=False)
    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: False)
    await _role(1, favorited_at=datetime.now(UTC))
    assert await _busy(client, scout_token) == {"ok": True, "waiting": 1, "leased": 0}
    await next_item(WORKER)
    assert await _busy(client, scout_token) == {"ok": True, "waiting": 0, "leased": 1}, "held, no longer waiting"


async def test_a_role_the_app_queued_by_itself_is_not_work_for_chrome_by_day_but_is_in_the_off_hours(
        js, client, scout_token, monkeypatch):
    """The backlog waits for 1 to 7 am (the owner, 2026-11-04). Items queued by the
    morning's chase were once handed out at midday and kept Chrome open; now nothing is handed out by day (PRD 52.9) and
    the bridge's idle close shuts Chrome. The roles on deck, the Followup-needed chase and the reads wait for the
    night."""
    from app.jobsearch.work import hours as night

    await browser_world(extra_sites=False)
    await _role(1)  # on deck: its original posting is to be found
    await _role(2, status="followup_needed")  # the chase's backlog
    await _role(3, link="https://careers.northwind3.test/jobs/3")  # a posting to read
    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: False)
    assert await _busy(client, scout_token) == {"ok": True, "waiting": 0, "leased": 0}, "by day: nothing for Chrome"
    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: True)
    # Old: 4 (three roles and the board's scan). New (PRD 32.6, the owner, 2026-11-13): the role in Followup needed is
    # the code's alone, so no session waits for it; the code worker (not running here) is its taker.
    assert (await _busy(client, scout_token))["waiting"] == 3, "the night drains it: two roles and the board's scan"
    bp.code_running(OWNER, True)
    try:
        assert (await _busy(client, scout_token))["waiting"] == 4, "and the code's try of the backlog role"
    finally:
        bp.code_running(OWNER, False)


async def test_a_site_to_map_counts_at_any_hour(js, client, scout_token, monkeypatch):
    """Training new sites can happen anytime (the owner, 2026-11-04): a map is no part of the backlog."""
    from app.jobsearch.work import hours as night

    await browser_world(extra_sites=False)
    async with db.session_factory()() as session:
        session.add(Site(owner=OWNER, url="https://jobs.otherboard.test/", kind="job_board", read_by="browser",
                         created_at=datetime.now(UTC)))
        await session.commit()
    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: False)
    assert await _busy(client, scout_token) == {"ok": True, "waiting": 1, "leased": 0}, "the map, not the board's scan"


async def test_a_drain_or_a_paused_mission_leaves_nothing_waiting_as_nothing_is_handed_out(
        js, client, scout_token, monkeypatch):
    from app.jobsearch.work import hours as night

    await browser_world(extra_sites=False)
    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: False)
    await _role(1, favorited_at=datetime.now(UTC))
    assert (await _busy(client, scout_token))["waiting"] == 1
    availability.drain(OWNER, True)
    try:
        assert (await _busy(client, scout_token))["waiting"] == 0, "a restart's drain hands out nothing"
    finally:
        availability.drain(OWNER, False)
    async with db.session_factory()() as session:
        await missions.pause(session, await mission_of(session, OWNER, missions.BROWSER), by="Tester")
        await session.commit()
    assert (await _busy(client, scout_token))["waiting"] == 0, "the owner paused the mission"


async def test_an_item_claimed_for_a_session_that_is_starting_counts_as_waiting_until_the_session_leases_it(
        js, client, scout_token, monkeypatch):
    """Found by review: the ask claims the item (path 47) and the runner then starts Claude and its tools, twenty
    seconds or more before the session's first `next_item` leases it. The claim drops the item and its domain from what
    `takeable.ask` offers, and it is not leased yet, so the bridge was told the queue held nothing and could end Chrome
    under
    the session that was starting."""
    from app.jobsearch.work import hours as night

    await browser_world(extra_sites=False)
    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: False)
    await _role(1, favorited_at=datetime.now(UTC))
    await _role(2, link="https://careers.northwind2.test/jobs/2", favorited_at=datetime.now(UTC))  # a domain of its own
    assert await _next(pool.BROWSER_READER) is True and await _next(pool.BROWSER_READER) is True, "two claims made"
    assert await _busy(client, scout_token) == {"ok": True, "waiting": 2, "leased": 0}, "two sessions are starting"
    async with db.session_factory()() as session:
        claim = (await session.scalars(select(HandOut).order_by(HandOut.created_at))).first()
    await next_item(WORKER, claim=str(claim.id))
    assert await _busy(client, scout_token) == {"ok": True, "waiting": 1, "leased": 1}, "one leased, one still starting"


async def test_a_claim_that_lapsed_without_its_session_is_the_items_own_again_and_counts_once(
        js, client, scout_token, monkeypatch):
    from app.jobsearch.work import hours as night

    await browser_world(extra_sites=False)
    monkeypatch.setattr(night, "in_off_hours", lambda s, now=None: False)
    await _role(1, favorited_at=datetime.now(UTC))
    assert await _next(pool.BROWSER_READER) is True
    async with db.session_factory()() as session:
        for row in (await session.scalars(select(HandOut))).all():
            row.lease_until = datetime.now(UTC) - timedelta(seconds=1)  # the session never came
        await session.commit()
    assert await _busy(client, scout_token) == {"ok": True, "waiting": 1, "leased": 0}, "free again: the item, once"


async def test_peak_hours_hold_the_sessions_items_and_not_the_codes_so_each_item_counts_once(
        js, client, scout_token, monkeypatch):
    from app.jobsearch.work import hours as night

    await browser_world(extra_sites=False)  # in the off hours (the tests' clock): the board's scan, a session's alone
    await _role(2, link="https://careers.northwind2.test/jobs/2")  # a posting to read: the app's code tries it first
    bp.code_running(OWNER, True)
    try:
        assert (await _busy(client, scout_token))["waiting"] == 2, "the session's item, and the code's, once each"
        monkeypatch.setattr(night, "in_peak", lambda s, now=None: True)  # the pool's hold: no session starts
        assert (await _busy(client, scout_token))["waiting"] == 1, "no session starts at peak, the code still reads"
    finally:
        bp.code_running(OWNER, False)


# --- the runner's heartbeats ----------------------------------------------------------------------------------------


async def test_a_browser_session_that_ends_gives_its_item_back_and_a_quiet_one_is_told_to_stop(js):
    await browser_world(extra_sites=False)
    await _role(1)
    await next_item(WORKER)
    await _beat({"session_id": WORKER, "state": "ended", "lane": "any", "runner": "session-2"})
    assert (await items(kind="source"))[0].state == "waiting"
    await next_item(WORKER_B)
    async with db.session_factory()() as session:
        entry = next(r for r in await br.open_runs(session, OWNER) if r.started["session_id"] == WORKER_B)
        entry.started = {**entry.started, "last_call": (datetime.now(UTC) - timedelta(minutes=11)).isoformat()}
        await session.commit()
    assert await _beat({"session_id": WORKER_B, "state": "alive", "lane": "any", "runner": "session-3"}) is True
    assert (await items(kind="source"))[0].state == "waiting"


# --- the page: the kinds, the strip, Run now, Wake up -----------------------------------------------------------------


async def test_the_agents_page_lists_both_kinds_with_their_files_waiting_work_and_health(js, client):
    await browser_world(learned=False)
    await _role(1)
    await make_the_queue()
    rows = {r["kind"]: r for r in (await client.get(KINDS)).json()}
    assert rows["pathfinder"]["waiting"] == 2, "two sites wait for a runbook"
    assert rows["browser_reader"]["waiting"] == 1
    assert {"kind_of_file": "instructions", "name": "browser.reader"} in rows["browser_reader"]["instructions"]
    assert {"kind_of_file": "templates", "name": "browser.pathfinder.runbook"} in rows["pathfinder"]["instructions"]
    assert rows["browser_reader"]["health"].startswith("Done in the last hour: 0 source, 0 read, 0 scan. 1 waiting")
    assert rows["writer"]["health"] is None


async def test_the_strip_shows_each_browser_worker_and_its_item(js):
    await browser_world(extra_sites=False)
    await _role(1)
    await next_item(WORKER)
    now = datetime.now(UTC)
    async with db.session_factory()() as session:
        session.add(AgentRunner(owner=OWNER, name="session-2", kind=roster.BROWSER_READER, state="working",
                                session_id=WORKER, since=now, seen_at=now))
        await session.commit()
    async with db.session_factory()() as session:
        view = await roster.runners_view(session, OWNER, await roster.runner_rows(session, OWNER), now)
    task = next(r["task"] for r in view if r["name"] == "session-2")
    assert task == ("Working in the browser: example.test: finding the original posting of Programs Director 1 at "
                    "Initech 1")


async def test_run_now_queues_the_browser_when_items_wait_and_says_nothing_otherwise(js, client):
    await browser_world(extra_sites=False)
    assert (await client.post(f"{KINDS}/browser_reader/run")).json()["queued"] == 1, "the site's scan"
    assert (await client.post(f"{KINDS}/pathfinder/run")).json()["queued"] == 0, "nothing to map"


async def test_wake_up_on_a_stage_held_by_the_browser_asks_for_it_and_says_so(js, client):
    await browser_world(extra_sites=False)
    await _role(4, link="https://careers.northwind4.test/jobs/4")
    await make_the_queue()
    ran = (await client.post(f"{KINDS}/researcher/run")).json()
    assert ran["blocked"] == "1 is waiting for the posting's text: browser read started"
    await _role(5)
    async with db.session_factory()() as session:
        await pool.pause(session, OWNER, pool.BROWSER_READER)
        await session.commit()
    await forget_asks(OWNER)
    ran = (await client.post(f"{KINDS}/title_checker/run")).json()
    assert not ran["blocked"].endswith("browser read started"), "a paused browser kind is not asked"


# --- the owner's own Chrome, when it is the setting: Run and Wake up say why it cannot run ---------------------------

CLOSED = "Open Chrome to let the browser jobs run"


async def test_a_runner_saying_the_chrome_cannot_be_had_holds_the_browser_jobs_until_it_says_it_is_back(js):
    await browser_world(extra_sites=False)
    await _role(1)
    await _beat({"session_id": "runner-idle", "state": "idle", "lane": "any", "runner": "session-1", "browser": CLOSED})
    assert await _next(pool.BROWSER_READER) is False, "no session is started only to fail"
    await _beat({"session_id": "runner-idle", "state": "idle", "lane": "any", "runner": "session-1", "browser": ""})
    assert await _next(pool.BROWSER_READER) is True


async def test_run_now_says_in_one_line_why_the_browser_cannot_run_and_tries_again(js, client):
    await browser_world(extra_sites=False)
    await committed(pool.note_browser, OWNER, CLOSED)
    ran = (await client.post(f"{KINDS}/browser_reader/run")).json()
    assert ran == {"kind": "browser_reader", "queued": 0, "held": CLOSED}
    assert await _next(pool.BROWSER_READER) is True, "asked: tried again"


async def test_the_claim_of_a_browser_job_answers_the_owners_ask_and_a_look_does_not(js, client):
    """The ask is a row (`asked:<kind>`). The hand-out removes it in the transaction of its claim; the walk, which reads
    on a reader, and a look (`next_kind`) leave it."""
    await browser_world(extra_sites=False)
    assert (await client.post(f"{KINDS}/browser_reader/run")).json()["queued"] == 1, "the site's scan"
    async with db.session_factory()() as session:
        await settle_after_the_queue(session, OWNER)
        assert await session_kinds.next_kind(session, OWNER) == pool.BROWSER_READER
        await session.commit()
    async with db.read_session_factory()() as reader:
        assert await pool.browser_asked(reader, OWNER) is True, "a look leaves the ask"
    assert await _next(pool.BROWSER_READER) is True
    async with db.read_session_factory()() as reader:
        assert await pool.browser_asked(reader, OWNER) is False, "the claim answered it"


async def test_a_site_with_a_runbook_on_a_domain_of_its_own_draws_its_own_worker(js):
    await browser_world(extra_sites=False)
    async with db.session_factory()() as session:
        site = Site(owner=OWNER, url="https://jobs.otherboard.test/", kind="job_board", read_by="browser",
                    created_at=datetime.now(UTC))
        session.add(site)
        await session.flush()
        await learn_sites(session, site)
        await session.commit()
    assert [await _next(pool.BROWSER_READER) for _ in range(3)] == [True, True, False]
    assert await _next(pool.PATHFINDER) is False
