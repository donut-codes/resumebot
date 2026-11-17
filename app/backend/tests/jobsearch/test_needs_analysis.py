"""The researcher's pick and the pool's waiting count are one rule (`line.startable`):
only a role on deck (Second Impression) is started, and the roles the researcher can take, has, or failed are the
ones on deck that are Sourced. A Sourced role that holds no text of its posting waits for it, on deck with a tag, and
what the pool says is waiting is what it queues. Everything here is made up."""

from datetime import UTC, datetime

from sqlalchemy import select

from app import db
from app.jobsearch.missions import core as missions
from app.jobsearch.models import Mission, Opportunity
from app.jobsearch.pool import core as pool
from app.jobsearch.research import analysis, line
from tests.builders import analysis_setup
from tests.support import OWNER


async def _states(states: dict[str, str | None]) -> dict[str, object]:
    """The three made-up roles, each put in the research state given (by company); returns their ids."""
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        for role in (await session.scalars(select(Opportunity))).all():
            if role.company in states:
                role.analysis_state = states[role.company]
        await session.commit()
    return ids


async def _cards(client) -> dict[str, list[str]]:
    body = (await client.get("/api/v1/board")).json()
    return {c["key"]: [x["company"] for x in c["cards"]] for c in body["columns"]}


async def test_a_sourced_unresearched_role_is_picked(js):
    await analysis_setup()
    async with db.session_factory()() as session:
        picked = await analysis.picks(session, OWNER, None)
    assert sorted(r.company for r in picked) == ["Fabrikam", "Hooli", "Initech"]


async def test_a_sourced_role_waiting_for_its_text_is_not_picked_but_is_on_deck_with_no_words_on_its_row(js, client):
    ids = await _states({"Hooli": analysis.NO_TEXT})
    async with db.session_factory()() as session:
        assert {r.id for r in await analysis.picks(session, OWNER, None)} == {ids["Fabrikam"], ids["Initech"]}
    cols = await _cards(client)
    assert sorted(cols["second_impression"]) == ["Fabrikam", "Hooli", "Initech"], "on deck: shown here"
    columns = (await client.get("/api/v1/board")).json()["columns"]
    card = next(c for c in columns if c["key"] == "second_impression")["cards"]
    # Old: Hooli's row said "Waiting for the posting's text" (and, as the made-up roles were posted today, "Its first
    # hours add 20"). New (docs/PRD.md 24.18): waiting is the normal run, so no row says anything; the bonus is a key.
    assert {c["company"]: (c["next"], c["first_hours_bonus"]) for c in card} == {
        "Fabrikam": (None, 20), "Initech": (None, 20), "Hooli": (None, 20)}


async def test_the_researcher_takes_only_a_sourced_role_on_deck_and_has_or_failed_the_ones_it_began(js, client):
    ids = await _states({"Fabrikam": None, "Initech": analysis.WAITING, "Hooli": analysis.FAILED})
    async with db.session_factory()() as session:
        now = datetime.now(UTC)
        for n, (state, sourced, status) in enumerate(((analysis.DONE, now, "second_impression"),
                                                      (analysis.NO_TEXT, now, "second_impression"),
                                                      (None, None, "second_impression"),
                                                      (None, now, "first_impression"))):  # not on deck
            session.add(Opportunity(
                owner=OWNER, slug=f"x{n}", company=f"Extra {n}", title="Lead", origin="scout", status=status,
                match_key=f"extra {n}|lead", sourced_at=sourced, analysis_state=state))
        await session.commit()
    async with db.session_factory()() as session:
        startable = {r.id for r in (await session.scalars(select(Opportunity).where(line.startable()))).all()}
        picked = {r.id for r in await analysis.picks(session, OWNER, None)}
    assert picked == startable == {ids["Fabrikam"]}, "what is picked is what is startable"
    cols = await _cards(client)
    assert sorted(cols["second_impression"]) == ["Extra 0", "Extra 1", "Extra 2", "Fabrikam", "Hooli", "Initech"]
    assert cols["first_impression"] == ["Extra 3"], "sourced and unresearched, but not on deck: not started"


async def test_the_researchers_waiting_count_is_what_it_can_start_now(js):
    await analysis_setup()
    async with db.session_factory()() as session:
        await missions.ensure(session, OWNER)
        mission = await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == missions.ANALYSE))
        mission.settings = {**mission.settings, "target": 2}
        await session.commit()
        assert (await pool.waiting_counts(session, OWNER))[pool.RESEARCHER] == 2, "three roles, room for two"
        assert await pool.queue_research(session, OWNER, None) == 2, "and two is what the pool starts"
        assert (await pool.waiting_counts(session, OWNER))[pool.RESEARCHER] == 2, "the two queued; the room is used"
        assert await pool.queue_research(session, OWNER, None) == 0, "nothing more can start"
