"""A refusal or a bot check stops a host for good (the project rules: "a refusal is a stop"; docs/PRD.md path 28): the
browser's
queue ends the domain's items with the reason instead of resting them, and the app's own fetcher skips the host in later
runs. A 429 or a rate-limit page is still only a rest. Boardone is the one exception (2026-11-05, docs/PRD.md
48.14 d): its stop lasts until the owner's next off hours start and then it is read again. No network; made-up names."""

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.browser import queue as bq
from app.jobsearch.fetch import host_stops, polite
from app.jobsearch.models import BrowserDomain, BrowserItem, Opportunity
from tests.builders import ONE, WORKER, WORKER_B, browser_world, hand_back, in_session, items, lease, next_item
from tests.jobsearch.made_up_sites_b import made_up_boards  # noqa: F401 (used through pytestmark)
from tests.jobsearch.test_browser_work import NOW, _role
from tests.support import DEFAULT_OWNER as OWNER
from tests.support import before_the_lock

pytestmark = pytest.mark.usefixtures("the_queue_is_made_before_a_hand_out", "made_up_boards")

BOARDONE = "boardone.test"
#: The made-up owner's off hours are 1 to 7 AM (UTC in the tests): a refusal met by day, and one met inside the window.
BY_DAY = datetime(2026, 11, 5, 14, 0, tzinfo=UTC)
IN_THE_NIGHT = datetime(2026, 11, 6, 3, 0, tzinfo=UTC)
TONIGHT = datetime(2026, 11, 6, 1, 0, tzinfo=UTC)
THE_NIGHT_AFTER = TONIGHT + timedelta(days=1)
#: What a refusal met at `NOW` (22:00 UTC on 10-09, the time the queue tests keep) is stopped until.
NOW_STARTS = datetime(2026, 10, 10, 1, 0, tzinfo=UTC)


async def _pace(domain: str) -> BrowserDomain:
    async with db.session_factory()() as session:
        return await session.scalar(select(BrowserDomain).where(BrowserDomain.domain == domain))


async def test_a_bot_check_stops_the_domain_and_ends_its_items_with_the_reason(js):
    await browser_world(extra_sites=False)
    role_id = await _role(1)
    await _role(2)
    got = await next_item(WORKER, NOW)
    reply = await hand_back(WORKER, got["item"]["item_id"], "blocked", now=NOW, note="a Cloudflare bot check")
    pace = await _pace(ONE)  # the host that refused, not its domain
    assert pace.stopped_at == NOW and "Cloudflare" in pace.stopped_for
    assert pace.cool_until is None, "nothing rests: a stop is not a rest"
    assert "stopped" in reply["note"]
    rows = await items(kind="source")
    assert {r.state for r in rows} == {"stuck"}, "the one it held and the one waiting both end"
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
    assert "Cloudflare" in role.browser_note
    assert (await next_item(WORKER, NOW + timedelta(days=30)))["stop"] is True


@pytest.mark.parametrize("note", [
    "Browser could not open any tab (Target.createTarget: the tab did not open); page never loaded",
    "the browser's tab did not open twice",
    "DevTools websocket connection lost while loading the page",
    "the browser timed out before the page finished loading"])
async def test_our_own_browser_failing_is_no_stop_and_no_rest_and_the_item_is_tried_again(js, note):
    await browser_world(extra_sites=False)
    await _role(1)
    got = await next_item(WORKER, NOW)
    reply = await hand_back(WORKER, got["item"]["item_id"], "blocked", now=NOW, note=note)
    pace = await _pace("example.test")
    assert pace is None or (pace.stopped_at is None and pace.cool_until is None and not pace.blocks)
    assert "stopped" not in reply.get("note", "") and reply["item"] == "waiting"


async def test_a_scan_the_browser_failed_to_load_ends_as_a_plain_failure_not_a_stop(js):
    await browser_world()
    item_id = await lease("scan", ONE)
    note = "DevTools connection refused: Chrome is not running"
    reply = await hand_back(WORKER, item_id, "blocked", now=NOW, note=note)
    assert reply["ok"] and reply["result"] == "failed"
    domain = (await items(kind="scan"))[0].domain
    pace = await _pace(domain)
    assert pace is None or (pace.stopped_at is None and pace.cool_until is None)


async def test_a_rate_limit_page_is_still_only_a_rest(js):
    await browser_world(extra_sites=False)
    await _role(1)
    got = await next_item(WORKER, NOW)
    await hand_back(WORKER, got["item"]["item_id"], "blocked", now=NOW, note="a rate-limit page, too many requests")
    pace = await _pace("example.test")
    assert pace.stopped_at is None and pace.cool_until == NOW + timedelta(hours=1)
    assert (await items(kind="source"))[0].state == "waiting"


async def test_waiting_items_of_a_stopped_domain_are_ended_by_the_next_look(js):
    await browser_world(extra_sites=False)
    await _role(1)
    await in_session(WORKER, lambda s: bq.refresh(s, OWNER, NOW))
    assert (await items(kind="source"))[0].state == "waiting"
    await in_session(WORKER, lambda s: _stop(s, ONE, NOW))
    ended = await in_session(WORKER, lambda s: host_stops.end_waiting(s, OWNER, NOW))
    assert ended >= 1 and (await items(kind="source"))[0].state == "stuck"
    assert await in_session(WORKER, lambda s: host_stops.end_waiting(s, OWNER, NOW)) == 0, "once"


async def _stop(session, domain, now):
    await host_stops.stop(session, OWNER, domain, now, "answered 403")


async def _stuck_by_a_stop(role_number: int = 1):
    """A role with its source item ended by a stop of its site, as a refusal does (the note is the stop's)."""
    await browser_world(extra_sites=False)
    role_id = await _role(role_number)
    await in_session(WORKER, lambda s: bq.refresh(s, OWNER, NOW))
    await in_session(WORKER, lambda s: _stop(s, ONE, NOW))
    await in_session(WORKER, lambda s: host_stops.end_waiting(s, OWNER, NOW))
    (row,) = await items(kind="source")
    assert row.state == "stuck" and row.last_result == "blocked"
    return role_id


async def _lift(domain: str) -> None:
    """What the owner does in the data to read a site again: the stop's two columns cleared."""
    async with db.session_factory()() as session:
        pace = await session.scalar(select(BrowserDomain).where(BrowserDomain.domain == domain))
        pace.stopped_at = pace.stopped_for = None
        await session.commit()


async def test_an_item_a_stop_ended_comes_back_when_the_stop_is_gone_and_its_role_loses_the_stops_note(js):
    """The owner, 2026-11-15: a stop that is lifted brings back the items it ended."""
    role_id = await _stuck_by_a_stop()
    await _lift(ONE)
    await in_session(WORKER, lambda s: bq.refresh(s, OWNER, NOW))
    (row,) = await items(kind="source")
    assert (row.state, row.tries, row.last_result, row.last_note, row.done_at) == ("waiting", 0, None, None, None)
    async with db.session_factory()() as session:
        assert (await session.get(Opportunity, role_id)).browser_note is None


async def test_an_item_a_stop_ended_stays_stuck_while_its_host_is_still_stopped(js):
    role_id = await _stuck_by_a_stop()
    await in_session(WORKER, lambda s: bq.refresh(s, OWNER, NOW + timedelta(hours=1)))
    (row,) = await items(kind="source")
    assert row.state == "stuck" and row.last_result == "blocked"
    async with db.session_factory()() as session:
        assert "is stopped" in (await session.get(Opportunity, role_id)).browser_note


async def test_an_item_that_ended_unreadable_after_its_tries_stays_stuck_when_no_stop_is_in_force(js):
    await browser_world(extra_sites=False)
    await _role(1)
    await in_session(WORKER, lambda s: bq.refresh(s, OWNER, NOW))
    async with db.session_factory()() as session:
        row = (await session.scalars(select(BrowserItem).where(BrowserItem.kind == "source"))).one()
        row.state, row.tries, row.last_result = "stuck", 3, "unreadable"
        await session.commit()
    await in_session(WORKER, lambda s: bq.refresh(s, OWNER, NOW))
    (row,) = await items(kind="source")
    assert (row.state, row.tries, row.last_result) == ("stuck", 3, "unreadable")


def _fetcher(handle) -> polite.Fetcher:
    return polite.Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(handle)), per_host_seconds=0)


async def test_a_403_stops_the_host_in_later_runs_too(js):
    async with db.session_factory()() as session:
        await host_stops.load(session, OWNER)
    seen: list = []

    def handle(request):
        seen.append(request)
        return httpx.Response(403 if request.url.path == "/jobs" else 404)

    try:
        first = _fetcher(handle)
        try:
            await first.get("https://careers.pfizer-like.test/jobs")
            raise AssertionError("a 403 is a stop")
        except polite.Refused:
            pass
        pace = await _pace("careers.pfizer-like.test")
        assert pace.stopped_at is not None and "403" in pace.stopped_for
        host_stops.reset()  # a new run: the app's start loads what is stored
        async with db.session_factory()() as session:
            await host_stops.load(session, OWNER)
        asked = len(seen)
        try:
            await _fetcher(handle).get("https://careers.pfizer-like.test/other")
            raise AssertionError("the host stays stopped")
        except polite.Refused as exc:
            assert "stopped" in str(exc)
        assert len(seen) == asked, "nothing was sent"
    finally:
        host_stops.reset()


async def test_a_429_does_not_stop_the_host(js):
    async with db.session_factory()() as session:
        await host_stops.load(session, OWNER)
    try:
        fetch = _fetcher(lambda r: httpx.Response(429, headers={"Retry-After": "9999"}) if r.url.path == "/jobs"
                         else httpx.Response(404))
        assert (await fetch.get("https://slow.example/jobs")).status_code == 429
        assert await _pace("slow.example") is None
    finally:
        host_stops.reset()




async def test_a_403_stops_only_the_host_that_refused_not_its_sister_hosts(js):
    """One board page's 403 stopped the registrable domain, and with it the job system's API host and
    every other employer's board on the shared platform. A stop applies to the host that refused (PRD path 28)."""
    async with db.session_factory()() as session:
        await host_stops.load(session, OWNER)
    seen: list = []

    def handle(request):
        seen.append(request.url.host)
        return httpx.Response(403 if request.url.host == "boards.shared.test" else 200, text="ok")

    try:
        try:
            await _fetcher(handle).get("https://boards.shared.test/one")
            raise AssertionError("a 403 is a stop")
        except polite.Refused:
            pass
        assert host_stops.stopped_reason("boards.shared.test")
        assert host_stops.stopped_reason("api.shared.test") is None
        assert (await _fetcher(handle).get("https://api.shared.test/v1/boards")).status_code == 200
        domain = await _pace("shared.test")
        assert domain is None or domain.stopped_at is None
    finally:
        host_stops.reset()


async def test_a_bot_check_on_one_host_leaves_the_items_on_a_sister_host_of_the_domain_waiting(js):
    await browser_world(extra_sites=False)
    async with db.session_factory()() as session:
        for name, host in (("a", "boards.shared.test"), ("b", "jobs.shared.test")):
            session.add(BrowserItem(owner=OWNER, kind="read", target=name, domain="shared.test",
                                    link=f"https://{host}/x", state="waiting", next_at=NOW))
        await session.commit()
        await host_stops.stop(session, OWNER, "boards.shared.test", NOW, "answered 403")
        await session.commit()
    await in_session(WORKER, lambda s: host_stops.end_waiting(s, OWNER, NOW))
    states = {r.target: r.state for r in await items(kind="read")}
    assert states == {"a": "stuck", "b": "waiting"}


@contextlib.asynccontextmanager
async def _the_turn_held_by_another_writer():
    """Another writer holds the database's one writer turn (it has read, and has not yet ended its transaction) until
    the block ends."""
    holds, let_go = asyncio.Event(), asyncio.Event()

    async def the_other_writer() -> None:
        async with db.session_factory()() as other:
            await other.execute(select(BrowserItem.id).limit(1))
            holds.set()
            await let_go.wait()
            await other.rollback()

    holder = asyncio.ensure_future(the_other_writer())
    await holds.wait()
    try:
        yield let_go
    finally:
        let_go.set()
        await holder


async def _a_stopped_domains_waiting_item() -> BrowserItem:
    await browser_world(extra_sites=False)
    await _role(1)
    await in_session(WORKER, lambda s: bq.refresh(s, OWNER, NOW))
    (item, *_) = await items(kind="source")
    await in_session(WORKER, lambda s: _stop(s, ONE, NOW))
    return item


async def test_the_end_of_a_stopped_domains_items_waits_for_the_writer_turn_and_then_ends_every_item(js):
    """The database has one writer: while another writer holds the turn the end waits for it (it is not refused and
    leaves nothing out), and once the turn is free it ends every waiting item of the stopped domain."""
    await _a_stopped_domains_waiting_item()
    async with _the_turn_held_by_another_writer() as let_go:
        ending = asyncio.ensure_future(
            in_session(WORKER, lambda s: host_stops.end_waiting(s, OWNER, NOW)))
        await asyncio.sleep(0.2)
        assert not ending.done(), "it waits for the turn"
        let_go.set()
        assert await asyncio.wait_for(ending, 3) >= 1
    assert {r.state for r in await items(kind="source")} == {"stuck"}


async def test_the_end_of_a_stopped_domains_items_that_is_not_given_the_turn_in_time_is_told_busy_and_changes_nothing(
        js, monkeypatch):
    from app.errors import Busy

    await _a_stopped_domains_waiting_item()
    monkeypatch.setattr(db, "LOCK_TIMEOUT_MS", 100)
    async with _the_turn_held_by_another_writer():
        with pytest.raises(Busy):
            await asyncio.wait_for(
                in_session(WORKER, lambda s: host_stops.end_waiting(s, OWNER, NOW)), 3)
    assert {r.state for r in await items(kind="source")} == {"waiting"}, "nothing changed"
    assert await in_session(WORKER, lambda s: host_stops.end_waiting(s, OWNER, NOW)) >= 1, "the next look ends it"
    assert {r.state for r in await items(kind="source")} == {"stuck"}


async def _one_item_on_a_stopped_host() -> BrowserItem:
    """A waiting item on `boards.shared.test`, which has refused: the one `end_waiting` finds."""
    await browser_world(extra_sites=False)
    async with db.session_factory()() as session:
        session.add(BrowserItem(owner=OWNER, kind="read", target="a", domain="shared.test",
                                link="https://boards.shared.test/x", state="waiting", next_at=NOW))
        await session.commit()
        await host_stops.stop(session, OWNER, "boards.shared.test", NOW, "answered 403")
        await session.commit()
    (item,) = await items(kind="read")
    return item


async def test_an_item_whose_link_moved_to_a_host_that_did_not_refuse_since_the_read_is_not_ended(js, monkeypatch):
    """The end reads the items with no lock and names the host of each from its link (`item_key`): a refresh that
    rewrote the link between the read and the lock leaves an item for a host nobody stopped, which the end leaves
    waiting (it once failed with a KeyError on that host)."""
    item = await _one_item_on_a_stopped_host()

    async def a_refresh_moves_the_link():
        async with db.session_factory()() as other:
            (await other.get(BrowserItem, item.id)).link = "https://jobs.shared.test/x"
            await other.commit()

    before_the_lock(monkeypatch, a_refresh_moves_the_link)
    assert await in_session(WORKER, lambda s: host_stops.end_waiting(s, OWNER, NOW)) == 0
    (after,) = await items(kind="read")
    assert (after.state, after.link) == ("waiting", "https://jobs.shared.test/x")


async def test_a_waiting_item_a_session_leased_since_the_read_is_not_ended_with_its_worker_at_it(js, monkeypatch):
    """A leased item finishes with its worker: one that left Waiting between the read and the lock is left as it is
    (the lock's `where` on its state)."""
    item = await _one_item_on_a_stopped_host()

    async def a_session_leases_it():
        async with db.session_factory()() as other:
            row = await other.get(BrowserItem, item.id)
            row.state, row.leased_by, row.lease_until = "leased", WORKER_B, NOW + timedelta(minutes=5)
            await other.commit()

    before_the_lock(monkeypatch, a_session_leases_it)
    assert await in_session(WORKER, lambda s: host_stops.end_waiting(s, OWNER, NOW)) == 0
    (after,) = await items(kind="read")
    assert (after.state, after.leased_by, after.last_result) == ("leased", WORKER_B, None)


# --- Boardone's stop ends at the owner's next off hours start (2026-11-05; docs/PRD.md 48.14 d) --------------------


async def _stop_at(host: str, now: datetime, why: str = "a security check") -> None:
    async with db.session_factory()() as session:
        await host_stops.stop(session, OWNER, host, now, why)
        await session.commit()


async def test_a_refusal_at_boardone_stops_it_until_the_next_off_hours_start_and_every_other_host_for_good(js):
    """Old: a stop for boardone.test was for good, kept until someone edited the data, and ended the waiting items of
    every role found there. New: it ends at the next start of the owner's off hours; any other host is unchanged."""
    await _stop_at(BOARDONE, IN_THE_NIGHT)
    await _stop_at("boards.shared.test", IN_THE_NIGHT, "answered 403")
    li, other = await _pace(BOARDONE), await _pace("boards.shared.test")
    assert (li.stopped_at, li.stopped_until) == (IN_THE_NIGHT, THE_NIGHT_AFTER), "met in the night: the night after"
    assert li.stopped_for == "a security check" and li.cool_until is None
    assert (other.stopped_at, other.stopped_until) == (IN_THE_NIGHT, None), "any other host: for good"


async def test_a_refusal_at_boardone_met_by_day_stops_it_until_that_nights_start(js):
    await _stop_at("www.boardone.test", BY_DAY)  # as the fetcher names it: one record for the domain
    assert (await _pace(BOARDONE)).stopped_until == TONIGHT


async def test_a_stop_in_force_keeps_its_first_time_and_reason_and_a_lapsed_one_is_made_again(js):
    await _stop_at(BOARDONE, IN_THE_NIGHT, "the first check")
    await _stop_at(BOARDONE, IN_THE_NIGHT + timedelta(hours=1), "a second one, met by a page already on its way")
    first = await _pace(BOARDONE)
    assert (first.stopped_at, first.stopped_for, first.stopped_until) == (
        IN_THE_NIGHT, "the first check", THE_NIGHT_AFTER)
    again = THE_NIGHT_AFTER + timedelta(hours=2)
    await _stop_at(BOARDONE, again, "a check, a night later")
    later = await _pace(BOARDONE)
    assert (later.stopped_at, later.stopped_for, later.stopped_until) == (
        again, "a check, a night later", THE_NIGHT_AFTER + timedelta(days=1))


async def test_a_boardone_stop_that_was_for_good_before_stays_for_good(js):
    """A stop already on record (made before the change, or by hand in the data) is not given an end by a later one."""
    async with db.session_factory()() as session:
        session.add(BrowserDomain(owner=OWNER, domain=BOARDONE, stopped_at=BY_DAY - timedelta(days=3),
                                  stopped_for="www.boardone.test answered 403"))
        await session.commit()
    await _stop_at(BOARDONE, IN_THE_NIGHT)
    row = await _pace(BOARDONE)
    assert row.stopped_until is None and row.stopped_for == "www.boardone.test answered 403"
    assert host_stops.in_force(row, IN_THE_NIGHT + timedelta(days=400))


async def test_the_stop_is_in_force_until_the_start_and_then_boardone_is_read_again_by_itself(js, monkeypatch):
    async with db.session_factory()() as session:
        await host_stops.load(session, OWNER, IN_THE_NIGHT)
    await _stop_at(BOARDONE, IN_THE_NIGHT)
    row = await _pace(BOARDONE)
    assert host_stops.in_force(row, THE_NIGHT_AFTER - timedelta(seconds=1))
    assert not host_stops.in_force(row, THE_NIGHT_AFTER), "at the start it is read again"
    for instant, stopped in ((IN_THE_NIGHT + timedelta(hours=1), True), (THE_NIGHT_AFTER - timedelta(seconds=1), True),
                             (THE_NIGHT_AFTER, False), (THE_NIGHT_AFTER + timedelta(days=30), False)):
        monkeypatch.setattr(host_stops, "utc_now", lambda at=instant: at)
        assert bool(host_stops.stopped_reason("www.boardone.test")) is stopped, instant


async def test_after_a_restart_only_a_stop_still_in_force_is_loaded(js):
    await _stop_at(BOARDONE, IN_THE_NIGHT)
    await _stop_at("boards.shared.test", IN_THE_NIGHT, "answered 403")
    for restart, boardone_stopped in ((IN_THE_NIGHT + timedelta(hours=2), True), (THE_NIGHT_AFTER, False)):
        host_stops.reset()
        async with db.session_factory()() as session:
            assert await host_stops.load(session, OWNER, restart) == (2 if boardone_stopped else 1)
        assert bool(host_stops.stopped_reason(BOARDONE, restart)) is boardone_stopped
        assert host_stops.stopped_reason("boards.shared.test", restart + timedelta(days=90)), "for good"


async def test_waiting_items_of_a_boardone_stopped_until_the_night_wait_and_no_role_is_given_a_note(js):
    """Old: the stop ended every waiting item stuck with the reason, and the reason went on up to 400 roles' cards.
    New: they wait for the start, with nothing written onto a role."""
    await browser_world(extra_sites=False)
    role_id = await _role(1)
    async with db.session_factory()() as session:
        session.add(BrowserItem(owner=OWNER, kind="listing", target="a", domain=BOARDONE, state="waiting",
                                link="https://www.boardone.test/jobs/view/7001/", next_at=NOW, role_id=role_id))
        await session.commit()
    await _stop_at(BOARDONE, NOW)
    assert await in_session(WORKER, lambda s: host_stops.end_waiting(s, OWNER, NOW)) == 0
    [item] = await items(kind="listing")
    assert (item.state, item.last_note) == ("waiting", None)
    async with db.session_factory()() as session:
        assert (await session.get(Opportunity, role_id)).browser_note is None


async def test_a_session_that_meets_a_refusal_at_boardone_stops_it_until_the_night_and_its_item_waits_for_it(js):
    """The session's hand-back is the same stop as the app's own code's: the item goes back to waiting for the start
    with no try used, no note is written onto the role, and at the start it is handed out again."""
    await browser_world(extra_sites=False)
    link = "https://www.boardone.test/jobs/view/7001/"
    role_id = await _role(1, url=link, board_url=link)
    got = await next_item(WORKER, NOW)
    assert got["item"]["kind"] == "source" and got["item"]["domain"] == BOARDONE
    reply = await hand_back(WORKER, got["item"]["item_id"], "blocked", now=NOW, note="a security check page")
    pace = await _pace(BOARDONE)
    assert (pace.stopped_at, pace.stopped_until) == (NOW, NOW_STARTS), "NOW is 22:00: the window opens at 1 AM"
    [item] = await items(kind="source")
    assert (item.state, item.tries, item.next_at) == ("waiting", 0, NOW_STARTS)
    async with db.session_factory()() as session:
        assert (await session.get(Opportunity, role_id)).browser_note is None
    assert "for good" not in reply["note"] and "stopped until" in reply["note"] and reply["item"] == "waiting"
    again = await next_item(WORKER, NOW_STARTS)
    assert again["item"]["item_id"] == got["item"]["item_id"], "handed out again at the start"
    await hand_back(WORKER, again["item"]["item_id"], "blocked", now=NOW_STARTS,
                    note="a rate-limit page, too many requests")
    assert (await _pace(BOARDONE)).cool_until == NOW_STARTS + timedelta(hours=1), (
        "a slow-down after the stop rests an hour, as the first one does: the stop was no block in a row")


async def test_a_403_from_linkedins_guest_search_stops_it_until_the_next_start_and_the_next_night_reads_again(
        js, monkeypatch):
    """The night's guest reader reads through the polite fetcher, which keeps a refusal as a stop (`host_stops`): a 403
    in the night stops it for the rest of that night and the day, and the night after it is read again."""
    monkeypatch.setitem(polite.HOST_SECONDS, "www.boardone.test", 0.0)
    async with db.session_factory()() as session:
        await host_stops.load(session, OWNER, IN_THE_NIGHT)
    at, sent = [IN_THE_NIGHT], []
    monkeypatch.setattr(host_stops, "utc_now", lambda: at[0])

    def handle(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        sent.append(at[0])
        return httpx.Response(403 if len(sent) == 1 else 200, text="ok")

    search = "https://www.boardone.test/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=made-up"
    try:
        with pytest.raises(polite.Refused):
            await _fetcher(handle).get(search)
        assert (await _pace(BOARDONE)).stopped_until == THE_NIGHT_AFTER
        at[0] = IN_THE_NIGHT + timedelta(hours=3)  # the same day, a fresh run of the fetcher
        with pytest.raises(polite.Refused, match="stopped"):
            await _fetcher(handle).get(search)
        assert sent == [IN_THE_NIGHT], "nothing was sent while it was stopped"
        at[0] = THE_NIGHT_AFTER
        assert (await _fetcher(handle).get(search)).status_code == 200
        assert sent == [IN_THE_NIGHT, THE_NIGHT_AFTER], "read again"
    finally:
        host_stops.reset()


async def test_the_owners_list_of_stops_shows_a_stop_only_while_it_is_in_force_and_says_when_it_ends(js, monkeypatch):
    from app.jobsearch.owner_actions import reads, runner

    await _stop_at(BOARDONE, IN_THE_NIGHT)
    await _stop_at("boards.shared.test", IN_THE_NIGHT, "answered 403")
    hosts = {}
    for instant in (IN_THE_NIGHT + timedelta(hours=1), THE_NIGHT_AFTER):
        monkeypatch.setattr(reads, "utc_now", lambda at=instant: at)
        async with db.session_factory()() as session:
            hosts[instant] = (await runner.read(session, OWNER, "stops.list", {}))["result"]["hosts"]
    during = {h["host"]: h["until"] for h in hosts[IN_THE_NIGHT + timedelta(hours=1)]}
    assert during == {BOARDONE: THE_NIGHT_AFTER.isoformat(), "boards.shared.test": None}
    assert [h["host"] for h in hosts[THE_NIGHT_AFTER]] == ["boards.shared.test"], "Boardone is read again"


# --- one stop for Boardone, whichever of its hosts refused, and every reader asks the record (review, 2026-11-05) -----


async def test_a_stop_at_any_host_of_boardone_is_the_one_stop_of_boardone_and_the_queue_holds_it(js):
    """Found by review (2026-11-05): `ends_with_the_night` took any boardone.test host, but the record was kept under
    the host that refused (`uk.boardone.test`), so an item on `www.boardone.test` was still handed out and the rest of
    Boardone went on being read. A stop at a Boardone host is kept under the domain, which is what every reader asks;
    every other host stays a stop of its own host, for good (docs/PRD.md 48.14 d)."""
    await browser_world(extra_sites=False)
    link = "https://www.boardone.test/jobs/view/7001/"
    await _role(1, url=link, board_url=link)
    await _stop_at("uk.boardone.test", NOW)
    await _stop_at("jobs.shared.test", NOW, "answered 403")
    assert await _pace("uk.boardone.test") is None, "one record for Boardone"
    assert (await _pace(BOARDONE)).stopped_until == NOW_STARTS
    other = await _pace("jobs.shared.test")
    assert (other.stopped_at, other.stopped_until) == (NOW, None), "any other host: its own, for good"
    first = await next_item(WORKER, NOW)  # the Boardone item would have been handed out first: it is held
    assert first["item"]["domain"] != BOARDONE, "the item on www.boardone.test is held by a stop at uk.boardone.test"
    again = await next_item(WORKER_B, NOW_STARTS)
    assert again["item"]["domain"] == BOARDONE, "and is handed out again at the start"


async def test_a_stop_at_a_sub_host_of_boardone_is_held_by_the_fetcher_and_the_deck_on_every_host(js):
    async with db.session_factory()() as session:
        await host_stops.load(session, OWNER, IN_THE_NIGHT)
        await host_stops.stop(session, OWNER, "uk.boardone.test", IN_THE_NIGHT, "a security check")
        await session.commit()
    try:
        for host in ("www.boardone.test", "uk.boardone.test", "boardone.test"):
            assert host_stops.stopped_reason(host, IN_THE_NIGHT + timedelta(hours=1)), host
        assert host_stops.stopped_reason("www.boardone.test", THE_NIGHT_AFTER) is None, "and it ends for all of them"
        assert host_stops.stopped_reason("jobs.shared.test") is None
    finally:
        host_stops.reset()


async def test_a_403_the_fetcher_meets_at_a_sub_host_of_boardone_is_the_same_one_stop(js, monkeypatch):
    async with db.session_factory()() as session:
        await host_stops.load(session, OWNER, IN_THE_NIGHT)
    monkeypatch.setattr(host_stops, "utc_now", lambda: IN_THE_NIGHT)
    try:
        await host_stops.record_fetch_refusal("uk.boardone.test", 403)
        assert await _pace("uk.boardone.test") is None and (await _pace(BOARDONE)).stopped_until == THE_NIGHT_AFTER
        assert host_stops.stopped_reason("www.boardone.test", IN_THE_NIGHT + timedelta(hours=1))
    finally:
        host_stops.reset()


async def test_a_stop_another_process_made_is_taken_up_when_a_reader_asks_the_record(js):
    """Found by review (2026-11-05): the stopped hosts were loaded at the app's start and added to by this process, so
    the hand-run command's stop (a separate process, written to the record) was invisible to the fetcher and the deck
    until a restart. `host_stops.refresh` reads the record and takes a stop in force into this process's snapshot; it
    never clears one, and a stop this process holds that has no record (the snapshot's own) is answered as it was."""
    async with db.session_factory()() as session:
        await host_stops.load(session, OWNER, IN_THE_NIGHT)
        assert host_stops.stopped_reason("www.boardone.test", IN_THE_NIGHT) is None
        session.add(BrowserDomain(owner=OWNER, domain=BOARDONE, stopped_at=IN_THE_NIGHT, stopped_until=THE_NIGHT_AFTER,
                                  stopped_for="www.boardone.test answered 403"))
        await session.commit()
        later = IN_THE_NIGHT + timedelta(hours=1)
        assert await host_stops.refresh(session, OWNER, "www.boardone.test", later) == "www.boardone.test answered 403"
        assert host_stops.stopped_reason("www.boardone.test", later), "the fetcher holds off now"
        assert host_stops.stopped_reason("www.boardone.test", THE_NIGHT_AFTER) is None, "with the record's own end"
        assert await host_stops.refresh(session, OWNER, "www.boardone.test", THE_NIGHT_AFTER) is None, "run out"
        assert await host_stops.refresh(session, OWNER, "jobs.shared.test", later) is None
        host_stops._stopped["held.shared.test"] = "answered 403"  # a stop only this process holds
        assert await host_stops.refresh(session, OWNER, "held.shared.test", later) == "answered 403"
    host_stops.reset()
