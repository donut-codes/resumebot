"""The page saves a profile part and restores an earlier version; the engine checks the roles against the gates again
and works the scores out again. The page stores the version and asks the engine
(`engine_calls.ask`, "profile.after_save") in the same commit, waits up to `ASK_WAIT_S`, and answers with the engine's
sentence as `said`; with no answer in time it answers the saved version without `said`. Every name, number and sentence
is made up."""

import asyncio

import pytest
from sqlalchemy import func, select

from app import db, notice
from app.jobsearch import facts
from app.jobsearch.factory import build_app
from app.jobsearch.models import ProfileVersion
from app.jobsearch.profile import after_save
from app.jobsearch.profile import store as profile_store
from app.jobsearch.routes import profile as profile_routes
from app.jobsearch.work import engine_asks, engine_calls
from app.jobsearch.work.queue import topics
from app.timeutil import utc_now
from tests.jobsearch.work_queue.support import posted_now
from tests.split.test_route_programs import _keys
from tests.support import OWNER, PROFILE, bare, load_profile

pytestmark = pytest.mark.usefixtures("the_clock_says_noon")

SAVES = {"PUT /api/v1/profile/{kind}", "POST /api/v1/profile/{kind}/versions/{number}/restore"}
NOTHING_TO_CHECK = "Re-checked 0 postings and 0 roles: nothing changed."
REWORDED = {**PROFILE["gates"], "other": [{**PROFILE["gates"]["other"][0], "text": "Travel of 40% or more."}]}


async def _profile() -> None:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()


async def _versions(kind: str) -> int:
    async with db.session_factory()() as session:
        return (await session.scalar(select(func.count()).select_from(ProfileVersion).where(
            ProfileVersion.owner == OWNER, ProfileVersion.kind == kind))) or 0


async def _fact(key: str) -> str | None:
    async with db.read_session_factory()() as reader:
        return await facts.get(reader, OWNER, key)


def _watch(monkeypatch) -> list[tuple]:
    """What the engine's work was called with, in order: the check and the re-score. The page must never call them."""
    seen: list[tuple] = []
    real_run, real_out = after_save.run, profile_store.works_out_again

    async def run(session, owner, kind, version, how, said):
        seen.append(("after_save", kind, version, how))
        return await real_run(session, owner, kind, version, how, said)

    async def out(session, owner, kind, version):
        seen.append(("works_out_again", kind, version))
        return await real_out(session, owner, kind, version)

    monkeypatch.setattr(after_save, "run", run)
    monkeypatch.setattr(profile_store, "works_out_again", out)
    return seen


def _an_engine_that_answers(monkeypatch, *, tell: bool = True) -> list[asyncio.Task]:
    """A stand-in engine in this process: the first bell starts it. It takes the writer's turn for a fact of its own
    first (so the page's wait must hold none, or this commit ends in Busy and the test sees no answer), then answers
    every ask owed with the engine's own function. With `tell` False its notice is lost: the page hears nothing and
    reads the answer when its wait ends."""
    started: list[asyncio.Task] = []
    # In the whole app (what a test is) an ask runs on the spot with no row and no wait; here the real path is pinned:
    # the ask is a row and the page waits. The process stays the whole app, so the engine's own code can run in it.
    monkeypatch.setattr(engine_asks, "is_registered", lambda name: False)

    async def engine() -> None:
        async with db.session_factory()() as other:
            await facts.put(other, OWNER, "page_wait_holds_no_turn", "yes")
            await other.commit()
        await engine_asks.answer_due(OWNER, [topics.ANY], utc_now())

    def bell() -> None:
        if not started:
            started.append(asyncio.get_running_loop().create_task(engine()))

    monkeypatch.setattr(engine_calls, "nudge", bell)
    if not tell:
        monkeypatch.setattr(notice.waiters, "notify", lambda kind, ids: 0)
    return started


def test_the_page_serves_the_save_and_the_restore_and_the_engine_does_not():
    page, engine = (set(_keys(build_app(role))) for role in ("page", "engine"))
    assert SAVES <= page, sorted(SAVES - page)
    assert not SAVES & engine, sorted(SAVES & engine)


async def test_with_the_engine_up_a_save_of_hard_pass_answers_with_the_sentence_the_engine_said(client, monkeypatch):
    await _profile()
    monkeypatch.setattr(profile_routes, "ASK_WAIT_S", 10)
    seen = _watch(monkeypatch)
    started = _an_engine_that_answers(monkeypatch)
    saved = await client.put("/api/v1/profile/gates", json={"data": REWORDED})
    await asyncio.gather(*started)
    assert saved.status_code == 200
    assert bare(saved.json()) == {"saved": True, "version": 2, "said": NOTHING_TO_CHECK}
    assert saved.json()["profile"]["versions"]["gates"] == 2
    assert seen == [("after_save", "gates", 2, "was saved"), ("works_out_again", "gates", 2)]
    assert await _fact("said:gates:2") == NOTHING_TO_CHECK
    assert await _fact("page_wait_holds_no_turn") == "yes"


async def test_a_restore_asks_the_same_work_and_says_how_the_version_came_back(client, monkeypatch):
    await _profile()
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "gates", REWORDED, by="test", defer_works_out=True)
        await session.commit()
    monkeypatch.setattr(profile_routes, "ASK_WAIT_S", 10)
    seen = _watch(monkeypatch)
    started = _an_engine_that_answers(monkeypatch)
    back = await client.post("/api/v1/profile/gates/versions/1/restore")
    await asyncio.gather(*started)
    assert back.status_code == 200
    assert bare(back.json()) == {"restored": 1, "version": 3, "said": NOTHING_TO_CHECK}
    assert seen == [("after_save", "gates", 3, "was made by bringing version 1 back"),
                    ("works_out_again", "gates", 3)]


async def test_with_the_engine_down_the_version_is_stored_the_answer_has_no_sentence_and_the_ask_is_kept(
        client, monkeypatch):
    await _profile()
    monkeypatch.setattr(profile_routes, "ASK_WAIT_S", 0.05)
    monkeypatch.setattr(engine_asks, "is_registered", lambda name: False)  # the real path: a row, and a wait
    seen = _watch(monkeypatch)
    rung: list[str] = []
    monkeypatch.setattr(engine_calls, "nudge", lambda: rung.append("look"))
    saved = await client.put("/api/v1/profile/gates", json={"data": REWORDED})
    assert saved.status_code == 200
    assert bare(saved.json()) == {"saved": True, "version": 2}
    assert saved.json()["profile"]["versions"]["gates"] == 2
    assert seen == [], "the check and the re-score ran in the page"
    assert rung
    assert await _versions("gates") == 2
    assert [topic for topic, _ in await posted_now()].count("ask") == 1


async def test_a_lost_notice_costs_nothing_the_answer_written_is_read_when_the_wait_ends(client, monkeypatch):
    await _profile()
    monkeypatch.setattr(profile_routes, "ASK_WAIT_S", 1.0)
    started = _an_engine_that_answers(monkeypatch, tell=False)
    saved = await client.put("/api/v1/profile/gates", json={"data": REWORDED})
    await asyncio.gather(*started)
    assert bare(saved.json()) == {"saved": True, "version": 2, "said": NOTHING_TO_CHECK}


async def test_a_save_that_stores_nothing_or_a_part_nothing_follows_asks_nothing(client, monkeypatch):
    await _profile()

    async def not_asked(*args, **kwargs):
        raise AssertionError("the page asked the engine")

    monkeypatch.setattr(engine_calls, "ask", not_asked)
    same = await client.put("/api/v1/profile/gates", json={"data": PROFILE["gates"]})
    other = await client.put(
        "/api/v1/profile/criteria",
        json={"data": {**PROFILE["criteria"], "positioning": "A director who builds things."}})
    assert bare(same.json()) == {"saved": False, "version": None}
    assert bare(other.json()) == {"saved": True, "version": 2}
