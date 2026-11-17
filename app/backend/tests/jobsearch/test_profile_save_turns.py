"""The page's save of a profile part keeps the writer's turn for writing (the owner, 2026-11-10: reading must not lock
everything). The route refuses on a reader, stores the version (with the words put on a Hard pass's old answers) in one
short transaction and commits it; the scores are then worked out again a page at a time, each page its own turn; and the
answer is read on a reader. Before, the whole re-score ran inside the save's one transaction, and every other save and
every runner report was refused for as long as it took. Every name, number and sentence is made up."""

from datetime import date

import pytest
from sqlalchemy import func, select

from app import db
from app.jobsearch import stages
from app.jobsearch.models import Opportunity, ProfileVersion
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import prioritize
from app.jobsearch.routes import profile as profile_routes
from app.jobsearch.work import engine_calls
from tests.jobsearch.test_prioritize import SETTINGS, _role, _scores
from tests.support import FIVE, OWNER, PROFILE, bare, load_profile

COMPANIES = ("Fabrikam", "Initech", "Hooli", "Wingtip", "Contoso")
BIGGER = {"asks": [{**FIVE["asks"][0], "points": 4}, *FIVE["asks"][1:]]}
#: An answer to the made-up profile's sentence rule, kept before the words of a rule were kept with its answers.
UNSTAMPED = [{"id": "travel", "answer": "fail", "evidence": "Made-up evidence."}]
TRAVEL = PROFILE["gates"]["other"][0]["text"]
REWORDED = {**PROFILE["gates"], "other": [{**PROFILE["gates"]["other"][0], "text": "Travel of 40% or more."}]}


@pytest.fixture
def turns():
    """The writer's turns ended from here on: (seconds waited, seconds held) for each."""
    seen: list[tuple[float, float | None]] = []

    def noted(waited: float, held: float | None) -> None:
        seen.append((waited, held))

    db.turn_ends.append(noted)
    try:
        yield seen
    finally:
        db.turn_ends.remove(noted)


async def _five_scored_roles() -> None:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, settings=SETTINGS)
        profile = await profile_store.current(session, OWNER)
        for company in COMPANIES:
            await _role(session, profile, company, researched=True)
        await session.commit()


async def _versions(kind: str) -> int:
    async with db.session_factory()() as session:
        return (await session.scalar(select(func.count()).select_from(ProfileVersion).where(
            ProfileVersion.owner == OWNER, ProfileVersion.kind == kind))) or 0


async def _role_with_an_unstamped_answer() -> None:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        session.add(Opportunity(
            owner=OWNER, slug="fabrikam", company="Fabrikam", title="Programs Lead", status=stages.SECOND_IMPRESSION,
            origin="scout", match_key="fabrikam|programs lead", found=date.today(), gate_answers=UNSTAMPED))
        await session.commit()


async def _gate_answers() -> list[dict]:
    async with db.session_factory()() as session:
        return (await session.scalars(select(Opportunity.gate_answers).where(Opportunity.owner == OWNER))).one()


async def test_a_save_of_prioritization_holds_no_turn_across_more_than_one_page_of_scores(client, monkeypatch, turns):
    await _five_scored_roles()
    monkeypatch.setattr(prioritize, "PAGE", 2)  # five roles: three pages
    turns.clear()
    saved = await client.put("/api/v1/profile/scoring", json={"data": BIGGER})
    assert bare(saved.json()) == {"saved": True, "version": 2, "said": "Worked out 5 scores again: 5 changed."}
    assert len(turns) >= 4, "the version's turn, then a turn for each of the three pages of scores"
    assert all(fit == 6 for fit, _ in (await _scores()).values())


async def test_a_restored_prioritization_holds_no_turn_across_more_than_one_page_of_scores(client, monkeypatch, turns):
    await _five_scored_roles()
    await client.put("/api/v1/profile/scoring", json={"data": BIGGER})
    monkeypatch.setattr(prioritize, "PAGE", 2)
    turns.clear()
    back = await client.post("/api/v1/profile/scoring/versions/1/restore")
    assert bare(back.json()) == {"restored": 1, "version": 3, "said": "Worked out 5 scores again: 5 changed."}
    assert len(turns) >= 4, "the version's turn, then a turn for each of the three pages of scores"


async def test_the_answer_to_a_save_is_read_on_a_reader_and_asks_for_no_turn(client, monkeypatch, turns):
    await _five_scored_roles()
    read: list[tuple[bool, int]] = []
    real_view = profile_routes._view

    async def view(session):
        before = len(turns)
        out = await real_view(session)
        await session.rollback()  # had it been a writer's, its turn ends here and is counted
        read.append((db.is_reader(session), len(turns) - before))
        return out

    monkeypatch.setattr(profile_routes, "_view", view)
    saved = await client.put("/api/v1/profile/scoring", json={"data": BIGGER})
    assert saved.json()["profile"]["versions"]["scoring"] == 2, "the answer holds the profile as it now stands"
    assert read == [(True, 0)]


async def test_a_save_refused_for_a_stale_base_version_writes_nothing(client):
    await _five_scored_roles()
    scores = await _scores()
    refused = await client.put("/api/v1/profile/scoring", json={"data": BIGGER, "base_version": 0})
    assert refused.status_code == 409
    assert await _versions("scoring") == 1
    assert await _scores() == scores


async def test_a_failed_rescore_keeps_the_save_and_says_so(client, monkeypatch):
    await _five_scored_roles()

    async def fails(*args, **kwargs):
        raise RuntimeError("made-up failure")

    monkeypatch.setattr(prioritize, "after_save", fails)
    saved = await client.put("/api/v1/profile/scoring", json={"data": BIGGER})
    assert bare(saved.json()) == {
        "saved": True, "version": 2,
        "said": "Saved, but the roles already stored could not be worked out again just now."}
    assert await _versions("scoring") == 2


async def test_the_words_put_on_old_answers_the_new_hard_pass_and_the_ask_commit_together(client, monkeypatch):
    await _role_with_an_unstamped_answer()

    async def fails(*args, **kwargs):
        raise RuntimeError("made-up failure after the stamps")

    monkeypatch.setattr(engine_calls, "ask", fails)  # the ask is made after the stamps, before the one commit
    with pytest.raises(RuntimeError, match="after the stamps"):
        await client.put("/api/v1/profile/gates", json={"data": REWORDED})
    assert await _versions("gates") == 1, "the version is not stored without what follows it"
    assert await _gate_answers() == UNSTAMPED, "nor are the words put on the answers"


async def test_a_save_of_hard_pass_puts_the_words_the_rule_had_on_the_answers_kept_without_any(client):
    await _role_with_an_unstamped_answer()
    saved = await client.put("/api/v1/profile/gates", json={"data": REWORDED})
    assert saved.json()["version"] == 2
    assert await _gate_answers() == [{**UNSTAMPED[0], "says": TRAVEL}]
