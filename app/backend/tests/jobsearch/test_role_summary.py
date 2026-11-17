"""A researched role's summary (role_summary.py, the owner, 2026-10-24): three short lines kept on the role's
analysis and sent with the role for its pane. The research does not carry them: a summary alone is asked for a role
that enters Second Impression or Ready to Launch (once), or when one of those is opened without one; never in another
stage. It is handed to the analyst's lane after any research and taken
by the same submit. Every name and sentence is made up."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch import stages
from app.jobsearch.fit import scoring
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import Opportunity
from app.jobsearch.pipeline import live, live_sink
from app.jobsearch.pipeline import page as roles
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis, role_summary
from tests.builders import findings, save_documents, submit_findings
from tests.support import FIVE, OWNER, POSTING_WORDS, answered, load_profile

SUMMARY = {"company": "Fabrikam makes billing software for clinics.",
           "role": "Runs the delivery programs that roll the software out to new clinics.",
           "why": "It is the program leadership you look for, at a size you prefer, and you prove four of five asks."}


async def _setup() -> None:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates={"other": []})
        await save_documents(session)
        await roster.ensure_roster(session, OWNER)
        await session.commit()


async def _role(slug: str, status: str, *, state: str | None = analysis.DONE, extra: dict | None = None,
                favorite: bool = False) -> Opportunity:
    async with db.session_factory()() as session:
        role = Opportunity(
            owner=OWNER, slug=slug, company=f"Co {slug}", title=f"Lead {slug}", origin="scout",
            match_key=match_key(f"Co {slug}", f"Lead {slug}"), status=status, found=date.today(),
            url=f"https://jobs.example.test/{slug}", description=POSTING_WORDS, analysis_state=state,
            analysis={"notes": "A real programs role.", "required": [{"item": "Programs", "class": "P"}],
                      **(extra or {})} if state else {},
            analysed_at=datetime.now(UTC) if state else None,
            favorited_at=datetime.now(UTC) if favorite else None)
        session.add(role)
        await session.commit()
        return role


async def _drain(sub: live.Subscriber) -> list[tuple[str, dict]]:
    await live_sink.settle()
    out = []
    while not sub.queue.empty():
        _, kind, data = sub.queue.get_nowait()
        out.append((kind, data))
    return out


@pytest.fixture
def listening(js):
    sub = live.broker.subscribe(OWNER)
    yield sub
    live.broker.unsubscribe(sub)


async def _get(role_id) -> Opportunity:
    async with db.session_factory()() as session:
        return await session.get(Opportunity, role_id)


async def _waiting_role() -> Opportunity:
    async with db.session_factory()() as session:
        breakdown = scoring.compute(answered(), await profile_store.current(session, OWNER))
        role = Opportunity(
            owner=OWNER, slug="fabrikam", company="Fabrikam", title="Programs Lead", origin="scout",
            match_key=match_key("Fabrikam", "Programs Lead"), status=stages.SECOND_IMPRESSION, found=date.today(),
            url="https://jobs.example.test/fabrikam", description=POSTING_WORDS, fit=breakdown["total"],
            fit_breakdown=breakdown)
        session.add(role)
        await session.flush()
        await analysis.start(session, OWNER, role)
        await session.commit()
        return role


async def test_the_research_no_longer_carries_the_summary_but_a_role_it_leaves_in_second_impression_asks_for_one(js):
    await _setup()
    role = await _waiting_role()
    reply = await submit_findings(findings(role.id, summary=SUMMARY))
    assert reply["taken"] is True
    kept = (await _get(role.id)).analysis
    assert role_summary.SUMMARY_KEY not in kept, "a summary sent with the research is not kept"
    assert kept.get(role_summary.WANTED_KEY), "researched in Second Impression: a summary alone is asked, once"
    async with db.session_factory()() as session:
        shown = await roles.detail(session, OWNER, role.id)
    assert shown["summary"] is None and shown["summary_askable"] is False


async def test_a_role_entering_second_impression_or_ready_asks_once_and_other_stages_never_ask(js):
    await _setup()
    first = await _role("first", stages.FIRST_IMPRESSION)
    ready = await _role("ready", stages.FIRST_IMPRESSION)
    unread = await _role("unread", stages.FIRST_IMPRESSION, state=None)
    has_one = await _role("has", stages.FIRST_IMPRESSION, extra={
        role_summary.SUMMARY_KEY: {**SUMMARY, "v": role_summary.SUMMARY_VERSION}})
    given_up = await _role("tried", stages.FIRST_IMPRESSION, extra={role_summary.TRIES_KEY: role_summary.MOST_TRIES})
    async def move(role, to):
        async with db.session_factory()() as session:
            (await session.get(Opportunity, role.id)).status = to
            await session.commit()
    async def wanted(role):
        return bool((await _get(role.id)).analysis.get(role_summary.WANTED_KEY))
    await move(first, stages.APPLIED)  # a stage that is neither
    await move(ready, stages.SECOND_IMPRESSION)
    await move(unread, stages.SECOND_IMPRESSION)
    await move(has_one, stages.READY_TO_LAUNCH)
    await move(given_up, stages.READY_TO_LAUNCH)  # handed out MOST_TRIES times with no answer: given up on
    assert [await wanted(r) for r in (first, ready, unread, has_one, given_up)] == [False, True, False, False, False]
    stamp = (await _get(ready.id)).analysis[role_summary.WANTED_KEY]
    await move(ready, stages.READY_TO_LAUNCH)
    assert (await _get(ready.id)).analysis[role_summary.WANTED_KEY] == stamp, "asked once, not again on the next stage"


async def test_a_summary_alone_goes_to_the_analysts_lane_after_research_and_is_taken_by_the_same_submit(js, listening):
    await _setup()
    role = await _role("ready", stages.READY_TO_LAUNCH)
    async with db.session_factory()() as session:
        assert bool(await analysis.free(session, OWNER)) is False, "nothing asked yet"
        assert (await role_summary.ask(session, OWNER, role.id)) == {"asked": True}
        await session.commit()
    async with db.session_factory()() as session:
        assert bool(await analysis.free(session, OWNER)) is True
        task = await analysis.analysis_reply(session, OWNER, session_id="s-1")
        await session.commit()
    assert task["summary_only"] and task["role"]["id"] == str(role.id)
    assert task["research"]["notes"] == "A real programs role." and task["role"]["posting_text_kept"]
    assert task["response_shape"]["summary"].keys() == {"company", "role", "why"}
    async with db.session_factory()() as session:
        assert bool(await analysis.free(session, OWNER)) is False, "the session holds it"
        again = await analysis.analysis_reply(session, OWNER, session_id="s-2")
    assert again["task"] is None, "another session is not handed the same role"
    async with db.session_factory()() as session:
        refused = await analysis.submit(session, OWNER, {"role": str(role.id), "summary": {"company": "Only"}})
        await session.commit()
    assert refused["taken"] is False and "company, role and why" in refused["reason"]
    async with db.session_factory()() as session:
        taken = await analysis.submit(session, OWNER, {"role": str(role.id), "summary": SUMMARY})
        await session.commit()
    assert taken["taken"] is True and taken["summary"] == SUMMARY
    after = await _get(role.id)
    assert after.analysis_state == analysis.DONE and after.analysis["notes"] == "A real programs role."
    assert not {role_summary.WANTED_KEY, role_summary.CLAIMED_KEY} & after.analysis.keys()
    assert ("summary", {"id": str(role.id)}) in await _drain(listening), "an open pane is told to read it again"
    async with db.session_factory()() as session:
        assert bool(await analysis.free(session, OWNER)) is False


async def test_a_research_waiting_goes_before_a_summary_alone(js):
    await _setup()
    await _role("ready", stages.READY_TO_LAUNCH, extra={role_summary.WANTED_KEY: "2026-10-23T00:00:00.000000"})
    waiting = await _waiting_role()
    async with db.session_factory()() as session:
        task = await analysis.analysis_reply(session, OWNER, session_id="s-1")
    assert "summary_only" not in task and task["role"]["id"] == str(waiting.id)


async def test_opening_a_role_asks_only_in_second_impression_or_ready_and_a_favorite_elsewhere_asks_nothing(js, client):
    await _setup()
    fav = await _role("fav", stages.APPLIED, favorite=True)
    elsewhere = await _role("elsewhere", stages.FIRST_IMPRESSION)
    deck = await _role("deck", stages.SECOND_IMPRESSION)
    async with db.session_factory()() as session:
        assert (await session.scalars(select(Opportunity.id).where(role_summary.wanted()))).all() == []
        assert role_summary.askable(await session.get(Opportunity, fav.id)) is False
    for other in (fav, elsewhere):
        assert (await client.post(f"/api/v1/roles/{other.id}/summary")).json() == {"asked": False}
        assert (await client.get(f"/api/v1/roles/{other.id}")).json()["summary_askable"] is False
    assert (await client.get(f"/api/v1/roles/{deck.id}")).json()["summary_askable"] is True
    res = await client.post(f"/api/v1/roles/{deck.id}/summary")
    assert res.status_code == 200 and res.json() == {"asked": True}
    assert (await client.post(f"/api/v1/roles/{deck.id}/summary")).json() == {"asked": False}, "asked once"
    assert (await client.get(f"/api/v1/roles/{deck.id}")).json()["summary_askable"] is False


async def _want(*roles: Opportunity) -> int:
    """Ask a summary for each of these roles as a stage move does (`role_summary.want`); how many were asked."""
    asked = 0
    async with db.session_factory()() as session:
        for role in roles:
            asked += role_summary.want(await session.get(Opportunity, role.id))
        await session.commit()
    return asked


async def test_a_summary_of_an_older_voice_is_asked_again_and_the_new_one_replaces_it(js):
    await _setup()
    old = await _role("old", stages.READY_TO_LAUNCH, extra={
        role_summary.SUMMARY_KEY: {**SUMMARY, "why": "Against that, the owner's ask is higher."},
        role_summary.TRIES_KEY: role_summary.MOST_TRIES})
    current = await _role("current", stages.SECOND_IMPRESSION, extra={
        role_summary.SUMMARY_KEY: {**SUMMARY, "v": role_summary.SUMMARY_VERSION}})
    other_stage = await _role("other", stages.APPLIED, extra={role_summary.SUMMARY_KEY: {**SUMMARY}})
    assert role_summary.stale(await _get(old.id)) and not role_summary.stale(await _get(current.id))
    async with db.session_factory()() as session:
        assert bool(await analysis.free(session, OWNER)) is False, "an old summary alone is not asked for"
    assert await _want(old, current, other_stage) == 1
    assert (await _get(current.id)).analysis.get(role_summary.WANTED_KEY) is None
    assert (await _get(other_stage.id)).analysis.get(role_summary.WANTED_KEY) is None
    asked = await _get(old.id)
    assert asked.analysis[role_summary.SUMMARY_KEY]["why"].startswith("Against"), "shown until replaced"
    assert await _want(old, current, other_stage) == 0, "asked once"
    async with db.session_factory()() as session:
        task = await analysis.analysis_reply(session, OWNER, session_id="s-1")
        await session.commit()
    assert task["summary_only"] and task["role"]["id"] == str(old.id)
    async with db.session_factory()() as session:
        taken = await analysis.submit(session, OWNER, {"role": str(old.id), "summary": SUMMARY})
        await session.commit()
    assert taken["taken"] is True and taken["summary"] == SUMMARY
    after = await _get(old.id)
    assert after.analysis[role_summary.SUMMARY_KEY]["v"] == role_summary.SUMMARY_VERSION
    assert not role_summary.stale(after)
    async with db.session_factory()() as session:
        assert bool(await analysis.free(session, OWNER)) is False
    assert await _want(old, current, other_stage) == 0
