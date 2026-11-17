"""Go on Ready to Launch (`apply_go.go`, the owner 2026-10-16 and the simplified statuses): the day's roles are checked,
a packet is asked for each that has none, and each role opens as a tab in the app's own Chrome as its packet is done and
is marked opened, staying in Ready to Launch until the mail confirms it or the owner marks it Applied; a closed
posting's
tab is closed and its role ends; a held or failed packet is skipped with its reason; then the deck moves up and
backfills; with no Chrome the pages open in the owner's own. A stand-in window
(tests.support.fake_go_window), fixture pages and packets made straight in the table, or asked for as the app asks."""

import asyncio
import time
from datetime import date

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.browser import code as browser_code
from app.jobsearch.browser import go as apply_go
from app.jobsearch.browser import go_checks, go_status
from app.jobsearch.browser.code import Page
from app.jobsearch.missions import numbers as mission_numbers
from app.jobsearch.models import GoRun, OpenTab, Opportunity, Packet
from app.jobsearch.pipeline import board
from app.jobsearch.pipeline import deck as pipeline
from app.jobsearch.pool import core as pool
from app.jobsearch.writing import packets, seats
from tests.builders import counted, save_documents
from tests.jobsearch.test_go_checks import _add, _packet, _roles, _trail, role
from tests.jobsearch.test_go_posting_match import NOT_AVAILABLE_PAGE, NOT_AVAILABLE_SAYS
from tests.support import OWNER, fake_go_window, go_that_leaves_nothing_running, load_profile

TODAY = date.today()
CLOSED_PAGE = "<html><body><h1>Product Lead</h1><p>No longer accepting applications</p></body></html>"


def _link(slug: str) -> str:
    return f"https://careers.example.test/open/{slug}"


async def _set_packets(slug: str, status: str) -> None:
    async with db.session_factory()() as session:
        found = (await session.scalars(select(Opportunity).where(Opportunity.slug == slug))).one()
        for row in (await session.scalars(select(Packet).where(Packet.role_id == found.id))).all():
            row.status = status
        await session.commit()


async def _packets_of(slug: str) -> list[Packet]:
    async with db.session_factory()() as session:
        found = (await session.scalars(select(Opportunity).where(Opportunity.slug == slug))).one()
        return list((await session.scalars(select(Packet).where(Packet.role_id == found.id)
                                           .order_by(Packet.version))).all())


async def _world(*roles: Opportunity) -> None:
    """The made-up profile and master resume (a packet is written from them) and the roles."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await save_documents(session)
        await session.commit()
    await _add(*roles)


async def _go(**kw) -> dict:
    return await apply_go.go(OWNER, poll=0, wait=0, **kw)


# --- 1. a finished packet opens and the role is marked opened ------------------------------------------------------


async def test_a_role_with_a_finished_packet_opens_and_is_marked_opened_in_ready_to_launch(js, monkeypatch):
    opened: list[str] = []
    kept: list[str] = []
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened, kept=kept))
    await _world(role("a", fit=9), role("b", fit=8))
    await _packet("a", "ready")
    await _packet("b", "ready")
    done = await _go()
    assert opened == [_link("a"), _link("b")], "in the day's order, each at the employer's own address"
    assert [o["title"] for o in done["opened"]] == ["Product Lead a", "Product Lead b"]
    assert done["phase"] == "done" and done["done"] is True and done["total"] == 2 and done["built"] == 2
    assert done["skipped"] == []
    assert kept == [_link("a"), _link("b")], "every tab is handed to the owner: the app never touches them again"
    roles = await _roles()
    assert roles["a"].status == roles["b"].status == stages.READY_TO_LAUNCH
    assert roles["a"].go_opened_at is not None and roles["b"].go_opened_at is not None
    assert roles["a"].go_check == go_checks.OPEN and roles["a"].go_skip is None
    [line, _other] = await _trail("go_opened")
    assert "stays in Ready to Launch" in line.sentence
    assert apply_go.progress(OWNER) == done, "the page reads the same after the run"


# --- 2. a role with no packet gets one asked, and opens only once it is done ----------------------------------------


async def test_a_role_with_no_packet_gets_a_packet_asked_and_opens_only_once_its_packet_is_done(js, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened))
    await _world(role("a", fit=9))
    task = asyncio.create_task(apply_go.go(OWNER, poll=0.01, wait=30))
    try:
        for _ in range(500):  # until the app has asked for the packet (the writer's sessions would take it)
            if await _packets_of("a"):
                break
            await asyncio.sleep(0.01)
        [asked] = await _packets_of("a")
        assert asked.status == "writing" and asked.version == 1
        await asyncio.sleep(0.1)
        assert opened == [], "the tab waits for the packet"
        assert (await _roles())["a"].status == stages.READY_TO_LAUNCH
        assert apply_go.progress(OWNER)["phase"] == "building" and not apply_go.progress(OWNER)["done"]
        await _set_packets("a", "ready")  # the writer sent it and the checks passed it
        done = await asyncio.wait_for(task, 10)
    finally:
        if not task.done():
            task.cancel()
    assert opened == [_link("a")] and [o["title"] for o in done["opened"]] == ["Product Lead a"]
    assert (await _roles())["a"].status == stages.READY_TO_LAUNCH
    [asking] = await _trail("packet_asked")
    assert asking.actor == "app" and asking.link_id == asked.role_id, "the app asked, not the owner"


async def test_a_packet_never_finished_in_time_leaves_the_role_in_ready_to_launch_with_the_reason(js, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened))
    await _world(role("a", fit=9))
    done = await _go()
    assert opened == [] and done["opened"] == []
    assert [s["why"] for s in done["skipped"]] == ["Its packet wasn't finished in time."]
    found = (await _roles())["a"]
    assert found.status == stages.READY_TO_LAUNCH and found.go_skip == "Its packet wasn't finished in time."
    assert len(await _packets_of("a")) == 1, "the packet is still being written: the next Go waits for it, no second"


async def test_a_failed_packet_never_blocks_its_role_the_next_go_asks_for_another(js, monkeypatch):
    monkeypatch.setattr(apply_go, "go_window", fake_go_window([]))
    await _world(role("a", fit=9))
    await _packet("a", "failed", attempts=0)  # an earlier night's packet no session ever began
    await _go()
    versions = await _packets_of("a")
    assert [(p.version, p.status) for p in versions] == [(1, "failed"), (2, "writing")]


async def test_each_launch_tab_is_opened_for_its_role_so_the_register_can_close_it_with_the_role(js, monkeypatch):
    roles_asked: list = []
    plain = fake_go_window([])

    class Recording(plain):
        async def tab(self, url: str, role_id=None) -> str:
            roles_asked.append(role_id)
            return await super().tab(url, role_id)

    monkeypatch.setattr(apply_go, "go_window", Recording)
    await _world(role("a", fit=9))
    await _packet("a", "ready")
    await _go()
    found = (await _roles())["a"]
    assert roles_asked == [found.id]


# --- 3. a closed posting's tab is closed and its role ends ----------------------------------------------------------


@pytest.mark.parametrize("page", [Page(url=_link("a"), status=200, html=CLOSED_PAGE),
                                  Page(url=_link("a"), status=404, html="Not found")], ids=["words", "404"])
async def test_a_closed_postings_tab_is_closed_and_the_role_ends_gone_quiet(js, monkeypatch, page):
    opened: list[str] = []
    kept: list[str] = []
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened, {_link("a"): page}, kept))
    await _world(role("a", fit=9), role("b", fit=8))
    await _packet("a", "ready")
    await _packet("b", "ready")
    done = await _go()
    assert opened == [_link("a"), _link("b")], "a's tab opened to be read"
    assert [o["title"] for o in done["opened"]] == ["Product Lead b"]
    [closed] = done["skipped"]
    assert closed["title"] == "Product Lead a" and "closed" in closed["why"] and "Gone Quiet" in closed["why"]
    assert kept == [_link("b")], "the tabs left are handed to the owner; a's was closed, not kept"
    roles = await _roles()
    assert (roles["a"].status, roles["a"].ended_reason) == (stages.GONE_QUIET, "posting_gone")
    assert roles["b"].status == stages.READY_TO_LAUNCH
    lines = await _trail(go_checks.BROWSER_EVENT)
    assert any("closed" in line.sentence for line in lines) and any("open" in line.sentence for line in lines)


async def test_a_launched_tab_that_says_the_opportunity_is_not_available_is_closed_and_the_role_ends(js, monkeypatch):
    """The launched tab of such a page said "Checked in the launched tab: open." and stayed open."""
    opened: list[str] = []
    kept: list[str] = []
    shown = Page(url=_link("a"), status=200, html=NOT_AVAILABLE_PAGE)
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened, {_link("a"): shown}, kept))
    await _world(role("a", fit=9), role("b", fit=8))
    await _packet("a", "ready")
    await _packet("b", "ready")
    done = await _go()
    assert [o["title"] for o in done["opened"]] == ["Product Lead b"] and kept == [_link("b")]
    assert "Gone Quiet" in done["skipped"][0]["why"]
    roles = await _roles()
    assert (roles["a"].status, roles["a"].ended_reason) == (stages.GONE_QUIET, "posting_gone")
    assert roles["a"].go_opened_at is None and roles["b"].status == stages.READY_TO_LAUNCH
    said = {line.sentence: line.detail for line in await _trail(go_checks.BROWSER_EVENT)}
    assert NOT_AVAILABLE_SAYS in (said["Checked in the launched tab: closed."] or "")


async def test_a_changed_posting_is_flagged_and_still_opens(js, monkeypatch):
    opened: list[str] = []
    changed = Page(url=_link("a"), status=200, html="<html><body><h1>Something Else</h1></body></html>")
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened, {_link("a"): changed}))
    monkeypatch.setattr(go_checks, "page_verdict", lambda *a, **k: (go_checks.CHANGED, "the posting reads differently"))
    await _world(role("a", fit=9))
    await _packet("a", "ready")
    done = await _go()
    assert [o["title"] for o in done["opened"]] == ["Product Lead a"]
    assert (await _roles())["a"].status == stages.READY_TO_LAUNCH
    [flagged] = await _trail(go_checks.CHANGED_EVENT)
    assert "differently" in (flagged.detail or "")


# --- 4. a held or failed packet is skipped --------------------------------------------------------------------------


async def test_trouble_goes_to_the_supervisor_a_refused_packet_and_a_second_failure_not_a_first(js, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened))
    asked: list[tuple[str, str]] = []

    async def supervisor(session, owner, role, problem, dossier):
        asked.append((role.slug, problem))
        return f"Supervisor's reason for {role.slug}."

    monkeypatch.setattr(apply_go, "supervisor", supervisor)
    await _world(role("a", fit=9), role("b", fit=8), role("c", fit=7))
    await _packet("a", "held")  # the checks found problems
    await _packet("b", "writing")  # still being written when Go starts
    await _packet("c", "ready")
    task = asyncio.create_task(apply_go.go(OWNER, poll=0.01, wait=30))
    try:
        await _until(lambda: _opened_n(1))
        assert [o["title"] for o in apply_go.progress(OWNER)["opened"]] == ["Product Lead c"]
        await _until(lambda: _stage_of_b())
        await _set_packets("b", "failed")  # the writer never sent it: the first failure is asked for again by code
        await _until(lambda: _versions("b", 2))
        assert ("b", apply_go.FAILED_TWICE) not in asked
        await _set_packets("b", "failed")  # and the second goes to the supervisor
        done = await asyncio.wait_for(task, 10)
    finally:
        if not task.done():
            task.cancel()
    assert opened == [_link("c")]
    assert sorted(asked) == [("a", apply_go.REFUSED), ("b", apply_go.FAILED_TWICE)]
    why = {s["title"]: s["why"] for s in done["skipped"]}
    assert why == {"Product Lead a": "Supervisor's reason for a.", "Product Lead b": "Supervisor's reason for b."}
    roles = await _roles()
    assert roles["a"].go_skip == why["Product Lead a"] and roles["b"].go_skip == why["Product Lead b"]
    assert roles["a"].status == roles["b"].status == stages.READY_TO_LAUNCH, "they stay for the owner"
    assert roles["c"].go_skip is None and roles["c"].go_opened_at is not None
    assert len(await _packets_of("a")) == 1, "a refused packet is not replaced by Go itself"


async def _opened_n(n: int) -> bool:
    return len(apply_go.progress(OWNER)["opened"]) >= n


async def _versions(slug: str, n: int) -> bool:
    return len(await _packets_of(slug)) >= n


async def _stage_of_b() -> bool:
    return any(b["title"] == "Product Lead b" for b in apply_go.progress(OWNER)["building"])


async def test_the_supervisor_is_not_called_when_every_packet_goes_well(js, monkeypatch):
    monkeypatch.setattr(apply_go, "go_window", fake_go_window([]))
    calls: list = []

    async def supervisor(*args):
        calls.append(args)

    monkeypatch.setattr(apply_go, "supervisor", supervisor)
    await _world(role("a", fit=9), role("b", fit=8))
    await _packet("a", "ready")
    task = asyncio.create_task(apply_go.go(OWNER, poll=0.01, wait=30))
    try:
        await _until(lambda: _versions("b", 1))
        await _set_packets("b", "ready")
        done = await asyncio.wait_for(task, 10)
    finally:
        if not task.done():
            task.cancel()
    assert calls == [] and len(done["opened"]) == 2


async def test_the_supervisor_asks_again_once_with_the_notes_then_gives_the_owner_a_plain_reason(js, monkeypatch):
    monkeypatch.setattr(apply_go, "go_window", fake_go_window([]))
    await _world(role("a", fit=9))
    await _packet("a", "held")
    async with db.session_factory()() as session:
        packet = (await session.scalars(select(Packet))).one()
        packet.checks = [{"check": "Dates", "ok": False, "blocking": True, "detail": "a date is missing",
                          "problems": ["a date is missing"]}]
        await session.commit()
    task = asyncio.create_task(apply_go.go(OWNER, poll=0.01, wait=30))
    try:
        await _until(lambda: _versions("a", 2))
        second = (await _packets_of("a"))[1]
        assert second.feedback.startswith(apply_go.RETRY_WORDS) and "a date is missing" in second.feedback
        assert packets.launched(second)
        await _set_packets("a", "held")
        done = await asyncio.wait_for(task, 10)
    finally:
        if not task.done():
            task.cancel()
    [skipped] = done["skipped"]
    assert skipped["why"].startswith("The packet could not be built:") and "Ask for it from its page" in skipped["why"]
    assert done["done"] is True and done["opened"] == []


async def test_builds_run_side_by_side_up_to_the_owners_limit_and_each_tab_opens_as_its_packet_is_done(js, monkeypatch):

    opened: list[str] = []
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened))
    await _world(role("a", fit=9), role("b", fit=8), role("c", fit=7))
    async with db.session_factory()() as session:
        await pool.set_active_sessions(session, OWNER, 2)
        await session.commit()
    task = asyncio.create_task(apply_go.go(OWNER, poll=0.01, wait=30))
    try:
        await _until(lambda: _versions("b", 1))
        assert await _has_packets("a") and await _packets_of("c") == [], "two at once, the third waits for a free place"
        await _until(lambda: _has_building(2))
        assert len(apply_go.progress(OWNER)["building"]) == 2
        await _set_packets("b", "ready")  # b finishes first: its tab opens and c is asked for at once
        await _until(lambda: _versions("c", 1))
        assert opened == [_link("b")]
        await _set_packets("a", "ready")
        await _set_packets("c", "ready")
        done = await asyncio.wait_for(task, 10)
    finally:
        if not task.done():
            task.cancel()
    assert sorted(opened) == [_link("a"), _link("b"), _link("c")] and done["built"] == 3


async def test_the_run_is_kept_in_the_database_and_a_lost_run_reads_as_stopped_with_what_it_did(js, monkeypatch):
    monkeypatch.setattr(apply_go, "go_window", fake_go_window([]))
    await _world(role("a", fit=9))
    await _packet("a", "ready")
    await _go()
    monkeypatch.setattr(apply_go, "_runs", {})  # the app restarted
    kept = await go_status.stored(OWNER)
    assert kept["phase"] == "done" and [o["title"] for o in kept["opened"]] == ["Product Lead a"]
    async with db.session_factory()() as session:  # a run that was going when the app stopped
        row = await session.get(GoRun, OWNER)
        assert row is not None
        row.state = {**row.state, "phase": "building", "done": False}
        await session.commit()
    going = await go_status.stored(OWNER)
    assert going["phase"] == "building" and going["done"] is False, "the page reads the row as it stands"
    async with db.session_factory()() as session:  # the app starts again, and closes what the stop cut short
        await apply_go.settle_runs(session, OWNER)
        await session.commit()
    lost = await go_status.stored(OWNER)
    assert lost["phase"] == "stopped" and lost["done"] is True and len(lost["opened"]) == 1
    assert any("The app stopped while Launch in Browser was running" in n for n in lost["notes"])


# --- 5. an opened role keeps its place until it is Applied; then the deck moves up ----------------------------------


async def test_an_opened_role_keeps_its_place_until_it_is_applied_and_then_the_deck_moves_up(js, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened))
    # Both have a Second Impression (research made by the findings), so their fit is their score: a 100%, c 40%.
    await _world(role("a", fit=9, **counted()), role("c", fit=2, status=stages.SECOND_IMPRESSION, **counted()),
                 role("d", fit=6, status=stages.FIRST_IMPRESSION))
    async with db.session_factory()() as session:
        await mission_numbers.save(session, OWNER, {"second_impression_size": 1, "ready_to_launch_size": 1}, by="owner")
        await session.commit()
    await _packet("a", "ready")
    done = await _go()
    assert [o["title"] for o in done["opened"]] == ["Product Lead a"] and opened == [_link("a")]
    roles = await _roles()
    assert roles["a"].status == stages.READY_TO_LAUNCH and roles["a"].go_opened_at is not None
    assert roles["c"].status == stages.SECOND_IMPRESSION, "the opened role still holds the one place of the day"
    assert roles["d"].status == stages.FIRST_IMPRESSION, "so nothing backfills"
    async with db.session_factory()() as session:  # the owner marks it Applied
        await board.move(session, OWNER, roles["a"].id, stages.APPLIED)
        await pipeline.run(session, OWNER)
        await session.commit()
    roles = await _roles()
    assert roles["a"].status == stages.APPLIED
    assert roles["c"].status == stages.READY_TO_LAUNCH, "the researched on-deck role moves up into the place"
    # Replaced behaviour (the best-of queue): Second Impression holds only read roles, so d is not backfilled onto it;
    # it stays in First Impression, marked to be read, and goes to Second Impression once Sonnet has read it.
    assert roles["d"].status == stages.FIRST_IMPRESSION, "an unread role stays in First Impression"


# --- 6. no Chrome of the app's own ----------------------------------------------------------------------------------


async def test_go_opens_in_the_owners_own_chrome_when_the_apps_chrome_cannot_start(js, monkeypatch):
    opened: list[str] = []
    launched: list[str] = []

    async def no_chrome() -> str:
        return "Chrome was not found"

    async def open_links(links, pause=None):
        launched.extend(links)

    monkeypatch.setattr(go_checks, "start_browser", no_chrome)
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened))
    monkeypatch.setattr(apply_go, "open_links", open_links)
    await _world(role("a", fit=9), role("b", fit=8))
    await _packet("a", "ready")
    await _packet("b", "ready")
    done = await _go()
    assert opened == [] and launched == [_link("a"), _link("b")], "every packet done, then one new window"
    assert any("couldn't open the tabs (Chrome was not found)" in n for n in done["notes"])
    assert [o["title"] for o in done["opened"]] == ["Product Lead a", "Product Lead b"]
    assert {r.status for r in (await _roles()).values()} == {stages.READY_TO_LAUNCH}


async def test_a_window_that_stops_part_way_sends_the_rest_to_the_owners_own_chrome(js, monkeypatch):
    launched: list[str] = []
    tried: list[str] = []

    class Breaking:
        async def open(self) -> None:
            return None

        async def tab(self, url: str, role_id=None) -> str:
            tried.append(url)
            if len(tried) > 1:
                raise browser_code.BrowserUnavailable("the window closed")
            return "tab-1"

        async def read(self, target, url):
            return Page(url=url, status=200, html="<html><p>Apply</p></html>")

        async def close_tab(self, target) -> None:
            return None

        async def keep(self) -> None:
            return None

    async def open_links(links, pause=None):
        launched.extend(links)

    monkeypatch.setattr(apply_go, "go_window", Breaking)
    monkeypatch.setattr(apply_go, "open_links", open_links)
    await _world(role("a", fit=9), role("b", fit=8))
    await _packet("a", "ready")
    await _packet("b", "ready")
    done = await _go()
    assert tried == [_link("a"), _link("b")] and launched == [_link("b")]
    assert any("stopped opening tabs part way" in n for n in done["notes"])
    assert [o["title"] for o in done["opened"]] == ["Product Lead a", "Product Lead b"]


# --- the page's two routes ------------------------------------------------------------------------------------------



async def test_a_window_that_stops_part_way_is_handed_over_and_let_go_not_left_connected(js, monkeypatch):
    launched: list[str] = []
    tried: list[str] = []
    handed: list[bool] = []

    class Breaking:
        async def open(self) -> None:
            return None

        async def tab(self, url: str, role_id=None) -> str:
            tried.append(url)
            if len(tried) > 1:
                raise browser_code.BrowserUnavailable("the window closed")
            return "tab-1"

        async def read(self, target, url):
            return Page(url=url, status=200, html="<html><p>Apply</p></html>")

        async def close_tab(self, target) -> None:
            return None

        async def keep(self) -> None:
            handed.append(True)

    async def open_links(links, pause=None):
        launched.extend(links)

    monkeypatch.setattr(apply_go, "go_window", Breaking)
    monkeypatch.setattr(apply_go, "open_links", open_links)
    await _world(role("a", fit=9), role("b", fit=8))
    await _packet("a", "ready")
    await _packet("b", "ready")
    await _go()
    assert handed == [True], "the window's connection to the bridge is let go once it stops, not held to the end"


async def test_the_progress_route_is_idle_before_go_has_run_and_reads_the_last_run_after(js, client, monkeypatch):
    monkeypatch.setattr(apply_go, "go_window", fake_go_window([]))
    idle = (await client.get("/api/v1/apply/go")).json()
    assert idle["phase"] == "idle" and idle["done"] is True and idle["opened"] == [] and idle["total"] == 0
    await _world(role("a", fit=9))
    await _packet("a", "ready")
    await apply_go.go(OWNER, poll=0, wait=0)
    body = (await client.get("/api/v1/apply/go")).json()
    assert body["phase"] == "done" and body["done"] is True and set(body) == set(apply_go.progress(OWNER))
    assert [o["title"] for o in body["opened"]] == ["Product Lead a"] and body["skipped"] == []
    assert body["total"] == 1 and body["built"] == 1


async def test_posting_go_starts_it_in_the_background_and_answers_at_once_with_the_progress(client, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened))
    monkeypatch.setattr(apply_go, "MAX_WAIT_SECONDS", 0)
    await _world(role("a", fit=9))
    await _packet("a", "ready")
    started = await client.post("/api/v1/apply/go")
    assert started.status_code == 200
    assert started.json()["done"] is False and started.json()["phase"] == "asked", (
        "it answers before Go is done: the row the press wrote"
    )
    assert apply_go.running(OWNER) or apply_go.progress(OWNER)["done"]
    await asyncio.wait_for(apply_go._tasks[OWNER], 10)
    body = (await client.get("/api/v1/apply/go")).json()
    assert body["done"] is True and [o["title"] for o in body["opened"]] == ["Product Lead a"]
    assert opened == [_link("a")] and (await _roles())["a"].status == stages.READY_TO_LAUNCH


async def test_posting_go_while_it_runs_starts_no_second_run(client, monkeypatch):
    gate = asyncio.Event()
    calls: list[str] = []

    async def go(owner, **kw):
        calls.append(owner)
        await gate.wait()
        return {}

    monkeypatch.setattr(apply_go, "go", go)
    first = await client.post("/api/v1/apply/go")
    await asyncio.sleep(0)
    second = await client.post("/api/v1/apply/go")
    assert first.status_code == second.status_code == 200
    assert calls == [OWNER], "one run at a time"
    gate.set()
    await asyncio.wait_for(apply_go._tasks[OWNER], 5)


async def test_only_a_request_from_this_machine_starts_go(client, monkeypatch):
    monkeypatch.setattr(go_status, "THIS_MACHINE", ("10.0.0.1",))
    refused = await client.post("/api/v1/apply/go")
    assert refused.status_code == 403 and apply_go.running(OWNER) is False


async def test_an_opened_role_keeps_its_place_and_a_second_go_opens_it_again(js, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened))
    await _world(role("a", fit=9), role("b", fit=8))
    await _packet("a", "ready")
    await _packet("b", "ready")
    await _go()
    assert opened == [_link("a"), _link("b")]
    again = await _go()
    assert opened == [_link("a"), _link("b")] * 2, "not yet Applied: its earlier tab may be gone, so it opens again"
    assert [o["title"] for o in again["opened"]] == ["Product Lead a", "Product Lead b"] and again["skipped"] == []
    roles = await _roles()
    # nothing makes them Applied but the mail or the owner
    assert roles["a"].status == roles["b"].status == stages.READY_TO_LAUNCH
    assert roles["a"].go_opened_at is not None


async def test_a_role_whose_tab_never_opened_is_not_marked_opened_and_opens_on_the_next_go(js, monkeypatch):
    launched: list[str] = []
    opened: list[str] = []

    class Refusing:
        async def open(self) -> None:
            return None

        async def tab(self, url: str, role_id=None) -> str:
            raise browser_code.BrowserUnavailable("the tab did not open")

        async def close_tab(self, target) -> None:
            return None

        async def keep(self) -> None:
            return None

    async def open_links(links, pause=None):
        launched.extend(links)

    monkeypatch.setattr(apply_go, "go_window", Refusing)
    monkeypatch.setattr(apply_go, "open_links", open_links)
    await _world(role("a", fit=9))
    await _packet("a", "ready")
    await _go()
    assert (await _roles())["a"].go_opened_at is None, "opened_at is set only once the tab is confirmed open"
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened))
    await _go()
    assert opened == [_link("a")] and (await _roles())["a"].go_opened_at is not None


async def test_when_the_apps_chrome_cannot_be_reached_the_status_says_so_with_the_fix_and_marks_nothing_opened(
        js, monkeypatch):
    launched: list[str] = []

    async def no_chrome() -> str:
        return "the browser bridge did not answer (ConnectError)"

    async def open_links(links, pause=None):
        launched.extend(links)

    monkeypatch.setattr(go_checks, "start_browser", no_chrome)
    monkeypatch.setattr(apply_go, "open_links", open_links)
    await _world(role("a", fit=9))
    await _packet("a", "ready")
    done = await _go()
    text = " ".join(done["notes"])
    assert "the browser bridge did not answer" in text and "remote debugging" in text
    assert "Launch in Browser again" in text
    assert [o["title"] for o in done["opened"]] == ["Product Lead a"] and done["opened"][0]["why"]
    assert (await _roles())["a"].go_opened_at is None


async def test_a_go_that_opens_nothing_says_why_and_is_never_a_silent_done(js, monkeypatch):
    monkeypatch.setattr(apply_go, "go_window", fake_go_window([]))
    await _world()
    done = await _go()
    assert done["phase"] == "done" and done["opened"] == [] and done["skipped"] == []
    assert any("Nothing in Ready to Launch" in n for n in done["notes"])


async def test_every_role_opened_or_skipped_is_listed_with_its_reason(js, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened))
    await _world(role("a", fit=9), role("b", fit=8))
    await _packet("a", "ready")
    await _packet("b", "ready")
    async with db.session_factory()() as session:
        found = (await session.scalars(select(Opportunity).where(Opportunity.slug == "b"))).one()
        found.apply_url = found.url = found.source_url = "not a web address"
        await session.commit()
    done = await _go()
    assert [o["title"] for o in done["opened"]] == ["Product Lead a"] and done["opened"][0]["why"]
    assert [s["title"] for s in done["skipped"]] == ["Product Lead b"] and done["skipped"][0]["why"]


# --- Launch is a run of its own: built roles open at once, the rest are built one after another ----------------------


async def _until(check, seconds: float = 10.0) -> None:
    """Wait up to `seconds` of wall time for `check()`. By the clock, not a count of short sleeps: on Windows a 10 ms
    sleep is under the clock's resolution and returns at once, so 500 of them were a fifth of a second."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if await check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("never happened")


async def _hold(session_id: str = "launch-1") -> None:
    """A session takes the seat that waits (`seats.take`), as the runner does."""
    async with db.session_factory()() as session:
        assert (await seats.take(session, OWNER, session_id))["seat"] is not None
        await session.commit()


async def _let_go(session_id: str = "launch-1") -> None:
    """The session ends without answering: its seat is freed (`seats.release`), as the runner's end does."""
    async with db.session_factory()() as session:
        assert await seats.release(session, OWNER, session_id) == 1
        await session.commit()


async def test_with_one_session_built_roles_open_at_once_and_the_rest_are_built_one_by_one(js, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened))
    await _world(role("a", fit=9), role("b", fit=8), role("c", fit=7))
    await _packet("a", "ready")
    await _limit(1)
    task = asyncio.create_task(apply_go.go(OWNER, poll=0.01, wait=30))
    try:
        await _until(lambda: _has_packets("b"))
        await _until(lambda: _has_building(1))
        assert opened == [_link("a")], "a's tab opened before b's packet was written"
        [asked] = await _packets_of("b")
        assert packets.launched(asked), "the packet is marked Launch's: the dedicated session takes it"
        assert await _packets_of("c") == [], "the owner allows one session: c is asked only when b is done"
        shown = apply_go.progress(OWNER)
        assert shown["phase"] == "building" and shown["total"] == 3 and shown["built"] == 1
        assert shown["current"]["title"] == "Product Lead b"
        assert shown["current"]["step"] == apply_go.STEPS["unclaimed"] and len(shown["building"]) == 1
        await _set_packets("b", "ready")
        await _until(lambda: _has_packets("c"))
        assert opened == [_link("a"), _link("b")], "b's tab opens as its packet is done, before c is written"
        await _set_packets("c", "ready")
        done = await asyncio.wait_for(task, 10)
    finally:
        if not task.done():
            task.cancel()
    assert opened == [_link("a"), _link("b"), _link("c")] and done["built"] == 3 and done["current"] is None


async def _limit(n: int) -> None:

    async with db.session_factory()() as session:
        await pool.set_active_sessions(session, OWNER, n)
        await session.commit()


async def _has_building(n: int) -> bool:
    return len(apply_go.progress(OWNER)["building"]) >= n


async def _has_packets(slug: str) -> bool:
    return bool(await _packets_of(slug))


async def test_a_packet_no_session_picks_up_is_said_on_the_status_and_a_session_that_dies_is_too(js, monkeypatch):
    monkeypatch.setattr(apply_go, "go_window", fake_go_window([]))
    monkeypatch.setattr(apply_go, "STALL_SECONDS", 0)
    await _world(role("a", fit=9))
    task = asyncio.create_task(apply_go.go(OWNER, poll=0.01, wait=30))
    try:
        await _until(lambda: _has_packets("a"))
        await _until(lambda: _stalled())
        assert "No writer session has picked up the packet for Product Lead a" in apply_go.progress(OWNER)["stalled"]
        assert any("Sessions may all be busy" in n for n in apply_go.progress(OWNER)["notes"])
        await _hold()
        await _until(lambda: _step(apply_go.STEPS["working"]))
        assert apply_go.progress(OWNER)["stalled"] is None, "a session took it: nothing is stalled"
        await _let_go()  # the session died: its seat is freed
        await _until(lambda: _stalled())
        assert "stopped before it was done" in apply_go.progress(OWNER)["stalled"]
        await _set_packets("a", "ready")
        done = await asyncio.wait_for(task, 10)
    finally:
        if not task.done():
            task.cancel()
    assert [o["title"] for o in done["opened"]] == ["Product Lead a"] and done["stalled"] is None


async def test_a_packet_waiting_its_turn_behind_a_busy_writer_is_not_stalled(js, monkeypatch):
    """Launch said "stalled" for a packet that only waited behind another one being written."""
    monkeypatch.setattr(apply_go, "go_window", fake_go_window([]))
    monkeypatch.setattr(apply_go, "STALL_SECONDS", 0.3)
    await _world(role("a", fit=9), role("b", fit=8))
    await _limit(2)
    task = asyncio.create_task(apply_go.go(OWNER, poll=0.01, wait=30))
    try:
        await _until(lambda: _has_packets("b"))
        await _hold()  # one writer session holds one of the two seats; the other packet waits its turn
        for _ in range(40):
            await asyncio.sleep(0.025)
            assert apply_go.progress(OWNER)["stalled"] is None, "the writer is busy, not stalled"
        await _set_packets("a", "ready")
        await _set_packets("b", "ready")
        await asyncio.wait_for(task, 10)
    finally:
        if not task.done():
            task.cancel()


async def _stalled() -> bool:
    return bool(apply_go.progress(OWNER)["stalled"])


async def _step(step: str) -> bool:
    return (apply_go.progress(OWNER)["current"] or {}).get("step") == step


async def test_go_stops_waiting_when_one_packet_is_not_done_in_time_and_says_the_rest_were_not_built(js, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened))
    await _world(role("a", fit=9), role("b", fit=8))
    done = await _go()
    assert opened == []
    assert {s["title"]: s["why"] for s in done["skipped"]} == {
        "Product Lead a": "Its packet wasn't finished in time.",
        "Product Lead b": "Launch stopped waiting for the writer before this one's turn: "
                          "press Launch in Browser again."}
    assert await _packets_of("b") == [], "no packet is asked for a role Go never reached"


async def test_an_existing_packet_being_written_is_joined_and_marked_launchs(js, monkeypatch):
    monkeypatch.setattr(apply_go, "go_window", fake_go_window([]))
    await _world(role("a", fit=9))
    await _packet("a", "writing")  # a mission asked for it earlier
    await _go()
    [joined] = await _packets_of("a")
    assert packets.launched(joined)


async def test_the_status_leaves_out_the_runs_private_parts(js, monkeypatch):
    monkeypatch.setattr(apply_go, "go_window", fake_go_window([]))
    await _world(role("a", fit=9))
    await _packet("a", "ready")
    await _go()
    assert set(apply_go.progress(OWNER)) == {"phase", "total", "built", "checked", "check_total", "opened", "skipped",
                                              "notes", "current", "building", "stalled", "done"}


async def test_a_launch_packets_seat_is_the_one_a_free_pool_session_is_told_to_take_and_a_note_is_listed_once(
        js, monkeypatch):
    """A Launch run asks for the packet and marks it Launch's, and the Launch lane is told to take its seat; the same
    note twice is listed once."""
    from app.jobsearch.work import session_kinds

    monkeypatch.setattr(apply_go, "go_window", fake_go_window([]))
    await _world(role("a", fit=9), role("b", fit=8))
    task = asyncio.create_task(apply_go.go(OWNER, poll=0.01, wait=30))
    try:
        await _until(lambda: _step(apply_go.STEPS["unclaimed"]))
        assert packets.launched((await _packets_of("a"))[0]), "the one in turn is asked for and marked Launch's"
        async with db.session_factory()() as session:
            assert await session_kinds.next_kind(session, OWNER) == session_kinds.SEAT
        state = apply_go._runs[OWNER]
        apply_go._note(state, "x")
        apply_go._note(state, "x")
        assert state["notes"].count("x") == 1
        await _set_packets("a", "ready")
        await _until(lambda: _has_opened(1))
        await _until(lambda: _has_packets("b"))
        await _set_packets("b", "ready")
        done = await asyncio.wait_for(task, 10)
    finally:
        if not task.done():
            task.cancel()
    assert done["built"] == 2 and done["skipped"] == [] and len(done["opened"]) == 2


async def _has_opened(n: int) -> bool:
    return len(apply_go.progress(OWNER)["opened"]) >= n


# --- a role skipped or closed during the run is replaced from Second Impression in the same run ---------------------


async def _one_place() -> None:
    async with db.session_factory()() as session:
        await mission_numbers.save(session, OWNER, {"second_impression_size": 1, "ready_to_launch_size": 1}, by="owner")
        await session.commit()


async def test_a_closed_posting_is_replaced_from_second_impression_in_the_same_run_and_the_status_names_it(
        js, monkeypatch):
    opened: list[str] = []
    pages = {_link("a"): Page(url=_link("a"), status=200, html=CLOSED_PAGE)}
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened, pages))
    await _world(role("a", fit=9, **counted()), role("c", fit=2, status=stages.SECOND_IMPRESSION, **counted()))
    await _one_place()
    await _packet("a", "ready")
    await _packet("c", "ready")
    done = await _go()
    roles = await _roles()
    assert roles["a"].status == stages.GONE_QUIET
    assert roles["c"].status == stages.READY_TO_LAUNCH and roles["c"].go_opened_at is not None
    assert opened == [_link("a"), _link("c")], "a's tab was closed, and c's opened in the same run"
    assert [o["title"] for o in done["opened"]] == ["Product Lead c"]
    assert "Pulled forward: Product Lead c at Firm c, in place of Product Lead a at Firm a." in done["notes"]


async def test_the_run_stops_pulling_forward_when_second_impression_has_nothing_left(js, monkeypatch):
    opened: list[str] = []
    pages = {_link("a"): Page(url=_link("a"), status=200, html=CLOSED_PAGE),
             _link("c"): Page(url=_link("c"), status=200, html=CLOSED_PAGE)}
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened, pages))
    await _world(role("a", fit=9, **counted()), role("c", fit=2, status=stages.SECOND_IMPRESSION, **counted()))
    await _one_place()
    await _packet("a", "ready")
    await _packet("c", "ready")
    done = await _go()
    roles = await _roles()
    assert roles["a"].status == roles["c"].status == stages.GONE_QUIET
    assert opened == [_link("a"), _link("c")] and done["opened"] == [] and done["phase"] == "done"
    assert [s["title"] for s in done["skipped"]] == ["Product Lead a", "Product Lead c"]


async def test_a_role_launch_skips_holds_no_place_so_the_next_one_is_pulled_forward_in_its_stead(js, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened))
    await _world(role("a", fit=9, apply_url="not a web address", **counted()),
                 role("c", fit=2, status=stages.SECOND_IMPRESSION, **counted()))
    await _one_place()
    await _packet("a", "ready")
    await _packet("c", "ready")
    done = await _go()
    roles = await _roles()
    assert roles["a"].status == stages.READY_TO_LAUNCH and roles["a"].go_skip, "it stays, with its reason"
    assert roles["c"].status == stages.READY_TO_LAUNCH and roles["c"].go_opened_at is not None
    assert opened == [_link("c")]
    assert "Pulled forward: Product Lead c at Firm c, in place of Product Lead a at Firm a." in done["notes"]


# --- no Go run outlives its test (`go_that_leaves_nothing_running`, tests/support.py; every test runs in it) ----------


async def _never(owner, **kw):
    await asyncio.Event().wait()


async def test_a_go_that_never_ends_is_cancelled_and_waited_for_when_the_block_ends(monkeypatch):
    monkeypatch.setattr(apply_go, "go", _never)
    async with go_that_leaves_nothing_running():
        await apply_go.start(OWNER)
        assert apply_go.running(OWNER)
        left_going = apply_go._tasks[OWNER]
    assert left_going.cancelled(), "cancelled, and done: the block waited for it"


async def test_the_block_starts_with_no_go_running_and_no_last_run_whatever_an_earlier_test_left(monkeypatch):
    monkeypatch.setattr(apply_go, "go", _never)
    await apply_go.start(OWNER)  # what an earlier test left going (the fixture around this test cancels it)
    assert apply_go.running(OWNER)
    async with go_that_leaves_nothing_running():
        assert apply_go.running(OWNER) is False and apply_go.progress(OWNER)["phase"] == "idle"


# --- the database writer is not held while Chrome is waited on (a held turn refused the owner's saves as busy) ------


def _real_tools_window(calls: list[tuple[str, bool]], gate: asyncio.Event | None = None,
                       entered: asyncio.Event | None = None):
    """Go's real window and its real `Tools.call` (the register of tabs included), with only the wire stubbed: it
    answers a target and a session, records whether a transaction was held at each call, and may wait on `gate`."""

    class RealCall(browser_code.GoWindow):
        def __init__(self) -> None:
            super().__init__()
            self.tools.seq = 0

            async def wire(method, params=None, session_id=None):
                calls.append((method, db.in_transaction()))
                if entered is not None:
                    entered.set()
                if gate is not None:
                    await gate.wait()
                return {"targetId": f"target-{len(calls)}", "sessionId": f"session-{len(calls)}"}

            self.tools._call = wire  # pyright: ignore[reportAttributeAccessIssue]

        async def open(self) -> None:
            return None

        async def read(self, target: str, url: str):
            return Page(url=url, status=200, html="<html><p>Apply</p></html>")

        async def keep(self) -> None:
            return None

    return RealCall


async def test_go_open_holds_no_transaction_during_cdp(js, monkeypatch):
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(apply_go, "go_window", _real_tools_window(calls))
    await _world(role("a", fit=9))
    await _packet("a", "ready")
    done = await _go()
    assert [o["title"] for o in done["opened"]] == ["Product Lead a"]
    assert calls and [m for m, _held in calls][0] == "Target.createTarget"
    assert not any(held for _m, held in calls), "no DevTools call is made inside a database transaction"


async def test_go_open_records_tab(js, monkeypatch):
    monkeypatch.setattr(apply_go, "go_window", _real_tools_window([]))
    await _world(role("a", fit=9))
    await _packet("a", "ready")
    await _go()
    found = (await _roles())["a"]
    async with db.session_factory()() as session:
        rows = list((await session.scalars(select(OpenTab))).all())
    assert [(r.kind, r.role_id) for r in rows] == [("go", found.id)]


async def test_cdp_call_inside_transaction_raises(js):
    tools = browser_code._DevTools(1, "go", "go")

    async def wire(method, params=None, session_id=None):
        return {}

    tools._call = wire  # pyright: ignore[reportAttributeAccessIssue]
    async with db.session_factory()() as session:
        await _world(role("a", fit=9))
        found = (await _roles())["a"]
        row = await session.get(Opportunity, found.id)
        assert row is not None
        row.go_skip = "held"  # a write: the session now holds the writer's turn
        await session.flush()
        with pytest.raises(db.IoInTransaction):
            await tools.call("Browser.getVersion")
        db.io_caught.clear()
        await session.rollback()


async def test_owner_write_not_blocked_while_go_waits_on_cdp(js, monkeypatch):
    gate, entered = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(apply_go, "go_window", _real_tools_window([], gate, entered))
    await _world(role("a", fit=9), role("b", fit=8))
    await _packet("a", "ready")
    running = asyncio.create_task(_go())
    try:
        await asyncio.wait_for(entered.wait(), 5)  # Go is now waiting on Chrome
        await asyncio.wait_for(_set_packets("b", "ready"), 5)  # an owner's write commits at once, not busy
    finally:
        gate.set()
        await running


# --- 4b. a skip reason names the packet's trouble, never the run's notes ---------------------------------------------


async def _look_at_a_twice_failed_packet(notes: list[str], checks: list[dict]) -> str:
    """The real supervisor on a role whose packet was held after Launch already asked again: the reason it stores."""
    await _world(role("a", fit=9))
    await _packet("a", "held", feedback=f"{apply_go.RETRY_WORDS} an earlier note")
    async with db.session_factory()() as session:
        (await session.scalars(select(Packet))).one().checks = checks
        await session.commit()
    state = {"notes": notes, "skipped": [], "built": 0}
    role_id = (await _roles())["a"].id
    assert await apply_go._Build(role_id).look(state, OWNER, 60) == "skipped"
    return (await _roles())["a"].go_skip


async def test_a_pulled_forward_note_never_becomes_the_reason_a_packet_could_not_be_built(js):
    why = await _look_at_a_twice_failed_packet(
        ["Pulled forward: Product Lead a at Firm a, in place of Product Lead z at Firm z.",
         "Product Lead a at Firm a: the writer did not finish it, so it was asked for again."], [])
    assert "Pulled forward" not in why and "asked for again" not in why
    assert why.startswith("The packet could not be built: the writer did not finish it.")


async def test_the_reason_comes_from_the_checks_findings_when_the_packet_failed_its_checks(js):
    why = await _look_at_a_twice_failed_packet(
        ["Pulled forward: Product Lead a at Firm a."],
        [{"check": "Dates", "ok": False, "blocking": True, "detail": "x", "problems": ["a date is missing"]}])
    assert why.startswith("The packet could not be built: Dates: a date is missing.")
