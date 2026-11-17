"""The check made when a role comes into Ready to Launch (docs/PRD.md 24.16, the owner, 2026-11-06): whichever way it
came in (the deck, a star, a hand move), its posting is checked at once and at any hour, as Go's check reads it
(`go_checks.check_entered`, a step of the pipeline's turn): still open, and the employer's own page still the posting
held. The result is on the role, a posting that is gone takes it out of Ready to Launch with its reason, and a page code
cannot read is the browser's, at priority 0. Everything here is made up."""

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.browser import go_checks
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.models import Opportunity, StageMove
from app.jobsearch.pipeline import board, quiet
from app.jobsearch.pipeline import deck as pipeline
from app.jobsearch.pipeline import row as role_row
from tests.builders import counted
from tests.jobsearch.test_go_checks import _add, _roles, _trail, fixture_internet, role
from tests.jobsearch.test_go_posting_match import POSTING, SOURCE, TITLE
from tests.support import OWNER, load_profile

SITE = "https://careers.example.test"
READY, SECOND = stages.READY_TO_LAUNCH, stages.SECOND_IMPRESSION


def at(path: str) -> dict:
    """A role that Launch would open at this page of the made-up site."""
    return {"apply_url": f"{SITE}/{path}"}


async def _check(now: datetime | None = None) -> int:
    async with db.session_factory()() as session:
        done = await go_checks.check_entered(session, OWNER, now)
        await session.commit()
        return done


async def test_a_role_that_came_into_ready_is_checked_and_says_the_posting_is_open_once(js, monkeypatch):
    asked: list[str] = []
    monkeypatch.setattr(polite, "make", fixture_internet(asked))
    await _add(role("live", **at("live")))
    assert await _check() == 1
    got = (await _roles())["live"]
    assert got.status == READY and got.go_check == go_checks.ENTERED and got.go_checked_at is not None
    [line] = await _trail(go_checks.READY_CHECK_EVENT)
    assert line.sentence == "Checked on entering Ready to Launch: the posting is still open."
    assert asked == ["/live"]
    assert await _check() == 0, "checked once: the role stays as it is until it comes in again"
    assert asked == ["/live"]


async def test_a_role_whose_posting_is_gone_leaves_ready_to_launch_with_its_reason(js, monkeypatch):
    monkeypatch.setattr(polite, "make", fixture_internet())
    await _add(role("gone", **at("gone")), role("removed", **at("removed")), role("expired", **at("expired")),
               role("closed-text", **at("closed-text")), role("live", **at("live")))
    assert await _check() == 5
    got = await _roles()
    assert {slug: r.status for slug, r in got.items()} == {
        "gone": stages.GONE_QUIET, "removed": stages.GONE_QUIET, "expired": stages.GONE_QUIET,
        "closed-text": stages.GONE_QUIET, "live": READY}
    assert {r.ended_reason for slug, r in got.items() if slug != "live"} == {stages.POSTING_GONE}
    lines = await _trail(quiet.REMOVED_EVENT)
    assert len(lines) == 4 and all(x.sentence == "Gone Quiet: the employer took the posting down." for x in lines)
    assert [x.sentence for x in await _trail(go_checks.READY_CHECK_EVENT)] == [
        "Checked on entering Ready to Launch: the posting is still open."], "only the open one says it is open"


async def test_a_page_code_cannot_read_is_the_browsers_item_and_no_chrome_is_started_by_the_turn(js, monkeypatch):
    """Old (2026-11-06, first build): a page that code could not tell about (`UNKNOWN`) was the browser's item too. New
    (docs/PRD.md 24.16): only a page that refuses the app or is a job board's (`UNREADABLE`) is."""
    monkeypatch.setattr(polite, "make", fixture_internet())

    async def no_chrome() -> str:
        raise AssertionError("the turn starts no Chrome: the browser's own item looks")

    monkeypatch.setattr(go_checks, "start_browser", no_chrome)
    await _add(role("blocked", **at("blocked")))
    assert await _check() == 1
    got = await _roles()
    assert got["blocked"].go_check == go_checks.LOOKING
    assert {x.sentence for x in await _trail(go_checks.READY_CHECK_EVENT)} == {
        "Checked on entering Ready to Launch: the app could not read the page, so the browser looks at it next."}
    async with db.session_factory()() as session:
        wanted = {role_id for role_id, _ in await go_checks.browser_checks(session, OWNER)}
    assert wanted == {got["blocked"].id}, "the browser's queue makes it the owner's item (`_wanted`)"


async def test_a_page_the_app_could_not_tell_about_is_left_to_launch_and_never_sent_to_the_browser(js, monkeypatch):
    """A page with no text of the posting in its markup, or one that did not answer, says nothing either way (`UNKNOWN`:
    Go counts it as open too). A short outage, or a posting drawn in the browser, must not queue a read for every role
    that entered Ready: the role says so (`UNCLEAR`) and Launch checks it again before its tab opens."""
    monkeypatch.setattr(polite, "make", fixture_internet())
    await _add(*(role(f"plain{n}", **at("open")) for n in range(4)))
    assert await _check() == 4
    got = await _roles()
    assert {r.go_check for r in got.values()} == {go_checks.UNCLEAR}
    assert all(r.go_checked_at is not None for r in got.values()), "checked once: none is picked again"
    assert {x.sentence for x in await _trail(go_checks.READY_CHECK_EVENT)} == {
        "Checked on entering Ready to Launch: the app could not tell from the page; Launch checks it again before "
        "its tab opens."}
    async with db.session_factory()() as session:
        assert await go_checks.browser_checks(session, OWNER) == [], "no browser read is queued"
    assert await _check() == 0
    now = datetime.now(UTC)
    assert not go_checks._fresh(Opportunity(go_check=go_checks.UNCLEAR, go_checked_at=now), now,
                                go_checks.CHECKED_WITHIN), "Go does not take it as its own check: it looks itself"


async def test_a_call_checks_a_deck_of_entries_at_most_and_the_next_takes_the_rest(js, monkeypatch):
    """Old: every entry was read in one step, each page up to 20 seconds, while the step held the pipeline's turn. New:
    `ENTRIES_PER_CHECK` roles a call (in id order)."""
    monkeypatch.setattr(go_checks, "ENTRIES_PER_CHECK", 2)
    asked: list[str] = []
    monkeypatch.setattr(polite, "make", fixture_internet(asked))
    await _add(*(role(f"live{n}", **at("live")) for n in range(5)))
    assert await _check() == 2
    assert sum(1 for r in (await _roles()).values() if r.go_checked_at is not None) == 2
    assert await _check() == 2 and await _check() == 1 and await _check() == 0
    assert asked == ["/live"] * 5, "each page was read once"


async def test_the_turn_goes_on_to_the_rest_of_the_entries_and_asks_for_the_next_turn_when_it_stops_short(
        js, monkeypatch):
    monkeypatch.setattr(go_checks, "ENTRIES_PER_CHECK", 1)
    monkeypatch.setattr(polite, "make", fixture_internet())
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()
    await _add(*(role(f"deck{n}", status=SECOND, **counted(), fit_breakdown={"math": 1, "percent": 50, "asks": []},
                      **at("live")) for n in range(4)))
    async with db.session_factory()() as session:
        with pipeline.accounting() as account:
            done = await pipeline.run(session, OWNER)
        await session.commit()
    assert done["ready"] == 4 and done["ready_check"] == pipeline.ROUNDS, "one a round, the rounds the turn has"
    assert account["more"] is True, "one is left: the next turn is owed"
    assert sum(1 for r in (await _roles()).values() if r.go_check == go_checks.ENTERED) == pipeline.ROUNDS
    async with db.session_factory()() as session:
        with pipeline.accounting() as account:
            again = await pipeline.run(session, OWNER)
        await session.commit()
    assert again["ready_check"] == 1 and account["more"] is False
    assert {r.go_check for r in (await _roles()).values()} == {go_checks.ENTERED}


async def test_the_employers_own_page_is_compared_with_the_posting_held_and_a_changed_one_is_flagged(js, monkeypatch):
    part = POSTING[: len(POSTING) // 3] + " ".join(f"new sentence {n} about other work" for n in range(60))

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, html=f"<html><body><h1>{TITLE}</h1><p>{part}</p></body></html>")

    monkeypatch.setattr(polite, "make", lambda: Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(handle)),
                                                        per_host_seconds=0))
    await _add(role("changed", title=TITLE, description=POSTING, source_url=SOURCE, apply_url=SOURCE,
                    source_kind="employer"))
    assert await _check() == 1
    got = (await _roles())["changed"]
    assert got.status == READY and got.go_check == go_checks.ENTERED, "the posting is up, in other words"
    [flag] = await _trail(go_checks.CHANGED_EVENT)
    assert flag.sentence.startswith("Posting changed:")
    [line] = await _trail(go_checks.READY_CHECK_EVENT)
    assert line.sentence == "Checked on entering Ready to Launch: the posting is up, in other words than the text held."


async def test_only_a_role_in_ready_that_launch_has_not_opened_is_checked(js, monkeypatch):
    asked: list[str] = []
    monkeypatch.setattr(polite, "make", fixture_internet(asked))
    await _add(role("opened", go_opened_at=datetime.now(UTC), **at("live")), role("deck", status=SECOND, **at("live"),
                                                                                  **counted()))
    assert await _check() == 0 and asked == []
    assert {r.go_check for r in (await _roles()).values()} == {None}


async def test_a_role_with_no_link_is_marked_checked_and_nothing_more(js, monkeypatch):
    monkeypatch.setattr(polite, "make", fixture_internet())
    await _add(role("nolink", apply_url=None, url=None, sourced_at=None))
    assert await _check() == 1
    got = (await _roles())["nolink"]
    assert got.go_checked_at is not None and got.go_check is None and got.status == READY
    assert await _trail(go_checks.READY_CHECK_EVENT) == []
    assert await _check() == 0


async def test_a_role_that_comes_into_ready_again_after_its_check_is_checked_again(js, monkeypatch):
    asked: list[str] = []
    monkeypatch.setattr(polite, "make", fixture_internet(asked))
    await _add(role("live", **at("live")))
    assert await _check() == 1
    async with db.session_factory()() as session:
        found = (await session.scalars(select(Opportunity))).one()
        found.status = SECOND
        await session.commit()
    assert await _check() == 0, "on deck, it is not in Ready to Launch"
    async with db.session_factory()() as session:
        found = (await session.scalars(select(Opportunity))).one()
        found.status = READY
        await session.commit()
        moves = (await session.scalars(select(StageMove).where(StageMove.to_status == READY))).all()
    assert len(moves) == 2
    assert await _check() == 1 and asked == ["/live", "/live"]


async def test_the_owners_hand_move_into_ready_is_checked_like_any_other_way_in(js, monkeypatch):
    monkeypatch.setattr(polite, "make", fixture_internet())
    await _add(role("moved", status=SECOND, **counted(), fit_breakdown={"math": 1, "percent": 50, "asks": []},
                    **at("expired")))
    async with db.session_factory()() as session:
        found = (await session.scalars(select(Opportunity))).one()
        await board.move(session, OWNER, found.id, READY)
        await session.commit()
    assert (await _roles())["moved"].status == READY
    assert await _check() == 1
    assert (await _roles())["moved"].status == stages.GONE_QUIET, "its posting is gone: it leaves with its reason"


async def test_the_turn_checks_a_role_the_deck_just_put_in_ready_to_launch(js, monkeypatch):
    monkeypatch.setattr(polite, "make", fixture_internet())
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()
    await _add(role("deck", status=SECOND, **counted(), fit_breakdown={"math": 1, "percent": 50, "asks": []},
                    **at("live")))
    async with db.session_factory()() as session:
        done = await pipeline.run(session, OWNER)
        await session.commit()
    assert done["ready"] == 1 and done["ready_check"] == 1
    got = (await _roles())["deck"]
    assert got.status == READY and got.go_check == go_checks.ENTERED


def test_go_takes_none_of_the_entry_checks_as_its_own_since_they_never_read_the_mail():
    now = datetime.now(UTC)
    within = go_checks.CHECKED_WITHIN
    for held in (go_checks.ENTERED, go_checks.LOOKING):
        assert not go_checks._fresh(Opportunity(go_check=held, go_checked_at=now), now, within), held
    assert go_checks._fresh(Opportunity(go_check=go_checks.BROWSER, go_checked_at=now), now, within)
    assert go_checks._fresh(Opportunity(go_check=go_checks.OPEN, go_checked_at=now - timedelta(hours=1)), now, within)


async def test_the_row_of_a_role_found_open_says_nothing_of_the_check(js, monkeypatch):
    monkeypatch.setattr(polite, "make", fixture_internet())
    await _add(role("live", **at("live")))
    await _check()
    async with db.session_factory()() as session:
        found = (await session.scalars(select(Opportunity))).one()
        row = await role_row.row(session, OWNER, found.id)
    # Old: "Launch builds its packet · Posting open when checked today · Its first hours add 20". New (docs/PRD.md
    # 24.16, 24.18): a posting found open is the normal run and says nothing; the hours are the row's key, no sentence.
    assert row["next"] is None and row["first_hours_bonus"] == 20, "the made-up role was posted today"


async def test_a_ready_role_under_research_whose_posting_is_found_gone_leaves_the_line_and_ready_with_its_reason(
        js, monkeypatch):
    """The catch-up researches a Ready role it imported (`test_catch_up.py` leaves the entry check out of its cases):
    when the turn finds the posting gone, the role leaves the analyst's line with Ready and says why."""
    monkeypatch.setattr(polite, "make", fixture_internet())
    await _add(role("under-research", analysis_state="waiting", analysis={}, **at("gone")),
               role("kept", analysis_state="waiting", analysis={}, **at("live")))
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()
    async with db.session_factory()() as session:
        done = await pipeline.run(session, OWNER)
        await session.commit()
    assert done["ready_check"] >= 1
    got = await _roles()
    gone = got["under-research"]
    assert gone.status == stages.GONE_QUIET and gone.ended_reason == stages.POSTING_GONE
    assert gone.analysis_state is None, "it is out of the analyst's line"
    [line] = [x for x in await _trail(quiet.REMOVED_EVENT) if x.link_id == gone.id]
    assert line.sentence == "Gone Quiet: the employer took the posting down."
    assert got["kept"].status == READY and got["kept"].analysis_state == "waiting", "the one that is up waits as it did"
    async with db.session_factory()() as session:
        said = (await role_row.row(session, OWNER, gone.id))["next"]
    assert said != role_row.RECHECKING_WORDS, "its row no longer says it is being read again"
