"""A favorite (favorites.py): the owner's star fast-tracks a role through the app's own steps: the owner's research ask
first, then a Ready to Launch place it holds while starred once it is read, and its packet (the step "Ask a favorite's
packet" of analyse_role.bpmn). Everything is made up."""

from datetime import UTC, datetime

from sqlalchemy import select, update

from app import db
from app.jobsearch import stages
from app.jobsearch.models import Opportunity, Packet
from app.jobsearch.pipeline import deck as pipeline
from app.jobsearch.research import analysis, line
from app.models.work import TrailEvent
from tests.builders import analysis_setup, counted, findings, submit_findings
from tests.support import FIVE, OWNER, PROFILE, load_profile


async def _role(role_id) -> Opportunity:
    async with db.session_factory()() as session:
        return await session.get(Opportunity, role_id)


async def _packets(role_id) -> list[Packet]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(Packet).where(Packet.role_id == role_id))).all())


async def _events(role_id) -> list[str]:
    async with db.session_factory()() as session:
        return list(await session.scalars(select(TrailEvent.event).where(TrailEvent.link_id == role_id)))


async def _set(role_id, **values) -> None:
    async with db.session_factory()() as session:
        await session.execute(update(Opportunity).where(Opportunity.id == role_id).values(**values))
        await session.commit()


async def _turn() -> None:
    async with db.session_factory()() as session:
        await pipeline.run(session, OWNER)
        await session.commit()


async def test_starring_an_unread_role_puts_it_first_in_the_analysts_line_and_it_goes_up_once_it_is_read(client):
    ids = await analysis_setup()
    hooli = ids["Hooli"]  # the weakest of the three: nothing would put it first
    answer = await client.put(f"/api/v1/roles/{hooli}/favorite", json={"favorite": True})
    assert answer.status_code == 200
    body = answer.json()
    assert body["favorite"] is True and body["changed"] is True and body["moved"] is False
    assert body["research"]["already"] is False and body["packet"] is None, "unread: its packet waits for the read"
    assert body["role"]["favorite"] is True and body["role"]["status"] == stages.SECOND_IMPRESSION
    assert body["counts"]["lanes"].get(stages.READY_TO_LAUNCH, 0) == 0 and body["counts"]["favorites"] == 1
    role = await _role(hooli)
    assert role.favorited_at is not None and role.status == stages.SECOND_IMPRESSION
    assert role.analysis_state == analysis.WAITING and line.asked_by_owner(role)
    assert {"favorite_set", "research_asked"} <= set(await _events(hooli))
    assert "favorite_ready" not in await _events(hooli)

    # The pipeline's own turn gives it no place before it is read, and the board shows the star.
    await _turn()
    assert (await _role(hooli)).status != stages.READY_TO_LAUNCH
    board = (await client.get("/api/v1/board", params={"column": stages.SECOND_IMPRESSION})).json()
    assert True in [c["favorite"] for c in board["columns"][0]["cards"]]
    detail = (await client.get(f"/api/v1/roles/{hooli}")).json()
    assert detail["role"]["favorite"] is True and detail["role"]["favorited_at"]

    await submit_findings(findings(hooli))
    assert (await _role(hooli)).status == stages.READY_TO_LAUNCH
    assert "favorite_ready" in await _events(hooli)


async def test_a_favorites_packet_is_asked_as_soon_as_its_read_is_done_and_no_other_roles_is(client):
    ids = await analysis_setup()
    await client.put(f"/api/v1/roles/{ids['Hooli']}/favorite", json={"favorite": True})
    async with db.session_factory()() as session:  # an ordinary read beside it
        await analysis.start(session, OWNER, await session.get(Opportunity, ids["Initech"]))
        await session.commit()
    await submit_findings(findings(ids["Hooli"]))
    await submit_findings(findings(ids["Initech"]))
    asked = await _packets(ids["Hooli"])
    assert [p.status for p in asked] == ["writing"], "the read's own process asked the favorite's packet"
    assert await _packets(ids["Initech"]) == [], "a role nobody starred waits for Go or Build now"
    role = await _role(ids["Hooli"])
    assert role.status == stages.READY_TO_LAUNCH and analysis.counts(role)
    async with db.session_factory()() as session:
        sentence = await session.scalar(select(TrailEvent.sentence).where(
            TrailEvent.link_id == ids["Hooli"], TrailEvent.event == "packet_asked"))
    assert sentence and "your favorite" in sentence


async def test_starring_a_role_that_is_read_asks_its_packet_at_once(client):
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        await analysis.start(session, OWNER, await session.get(Opportunity, ids["Initech"]))
        await session.commit()
    await submit_findings(findings(ids["Initech"]))
    assert await _packets(ids["Initech"]) == []
    body = (await client.put(f"/api/v1/roles/{ids['Initech']}/favorite", json={"favorite": True})).json()
    assert body["research"] is None, "read already: no research is asked"
    assert body["packet"] and [str(p.id) for p in await _packets(ids["Initech"])] == [body["packet"]]
    assert body["role"]["packet"]["status"] == "writing"
    # Starring again changes nothing, and asks no second packet.
    again = (await client.put(f"/api/v1/roles/{ids['Initech']}/favorite", json={"favorite": True})).json()
    assert again["changed"] is False and len(await _packets(ids["Initech"])) == 1


async def test_a_read_favorite_holds_its_ready_place_until_it_is_unstarred_then_competes_on_its_score(client):
    ids = await analysis_setup()
    hooli = ids["Hooli"]
    async with db.session_factory()() as session:  # one Ready place; the other two are read and fit better
        await load_profile(session, OWNER, scoring=FIVE, settings={**PROFILE["settings"], "ready_to_launch_size": 1})
        await session.commit()
    for key in ("Fabrikam", "Initech"):
        await _set(ids[key], **counted())
    # Read, out of Ready, starred in First Impression: the turn takes it up and keeps it there.
    await _set(hooli, status=stages.FIRST_IMPRESSION, favorited_at=datetime.now(UTC), **counted())
    await _turn()
    assert (await _role(hooli)).status == stages.READY_TO_LAUNCH
    await _turn()
    assert (await _role(hooli)).status == stages.READY_TO_LAUNCH, "it holds the place against better roles"
    body = (await client.put(f"/api/v1/roles/{hooli}/favorite", json={"favorite": False})).json()
    assert body["changed"] is True and body["role"]["favorite"] is False
    await _turn()
    assert (await _role(hooli)).status == stages.SECOND_IMPRESSION, "unstarred: the better roles take Ready"
    assert "favorite_cleared" in await _events(hooli)


async def test_a_starred_role_not_read_yet_holds_no_place_whatever_the_turn(client):
    ids = await analysis_setup()
    hooli = ids["Hooli"]
    await _set(hooli, status=stages.FIRST_IMPRESSION, favorited_at=datetime.now(UTC))
    await _turn()
    role = await _role(hooli)
    assert role.status == stages.FIRST_IMPRESSION and role.analysis_state == analysis.WAITING
    await _turn()
    assert (await _role(hooli)).status == stages.FIRST_IMPRESSION


async def test_a_closed_or_applied_role_can_be_starred_and_keeps_its_stage_and_its_reason(client):
    ids = await analysis_setup()
    await _set(ids["Hooli"], status=stages.GATED_OUT, screened_out_reason="Pay below your floor")
    await _set(ids["Initech"], status=stages.APPLIED)
    for key in ("Hooli", "Initech"):
        body = (await client.put(f"/api/v1/roles/{ids[key]}/favorite", json={"favorite": True})).json()
        assert body["favorite"] is True and body["moved"] is False
        assert body["research"] is None and body["packet"] is None
    gated = await _role(ids["Hooli"])
    assert gated.status == stages.GATED_OUT and gated.screened_out_reason == "Pay below your floor"
    assert gated.analysis_state is None
    await _turn()
    assert (await _role(ids["Initech"])).status == stages.APPLIED


async def test_favorites_lists_every_starred_role_whatever_its_stage_the_latest_starred_first(client):
    ids = await analysis_setup()
    await _set(ids["Hooli"], status=stages.APPLIED)
    assert (await client.get("/api/v1/favorites")).json()["cards"] == []
    for key in ("Hooli", "Initech"):
        await client.put(f"/api/v1/roles/{ids[key]}/favorite", json={"favorite": True})
    body = (await client.get("/api/v1/favorites")).json()
    assert body["key"] == "favorites" and body["count"] == 2
    assert [c["id"] for c in body["cards"]] == [str(ids["Initech"]), str(ids["Hooli"])]
    assert all(c["favorite"] for c in body["cards"])
    assert body["cards"][1]["status"] == stages.APPLIED and body["cards"][1]["status_label"] == "Applied"
    await client.put(f"/api/v1/roles/{ids['Initech']}/favorite", json={"favorite": False})
    assert [c["id"] for c in (await client.get("/api/v1/favorites")).json()["cards"]] == [str(ids["Hooli"])]


async def test_starring_a_role_there_is_none_of_is_404(client):
    answer = await client.put("/api/v1/roles/00000000-0000-0000-0000-000000000000/favorite", json={"favorite": True})
    assert answer.status_code == 404
