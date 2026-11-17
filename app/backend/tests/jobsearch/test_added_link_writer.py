"""A role still called "Added link" is not written for (the owner, 2026-10-16): the writer refuses it, and Go passes it
by with the reason. Go's line is Ready to Launch in the pipeline's order, read live, so a new rank moves it; a role
waiting for its research again is left out of it but stays in its column. Synthetic data only."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from app import db
from app.jobsearch import stages
from app.jobsearch.browser import go as apply_go
from app.jobsearch.models import ADDED_LINK, Opportunity, Packet
from app.jobsearch.pipeline import board
from app.jobsearch.research import analysis
from app.jobsearch.writing import packets
from tests.builders import analysis_setup, findings, save_documents
from tests.support import OWNER, fake_go_window, load_profile


async def _role(slug, status, *, company=None, origin="scout", state=analysis.DONE, fit=6,
                stamped=False) -> Opportunity:
    """`stamped`: the read was made by the new pipeline (stamped), so Go's checks keep the role in Ready."""
    read = {"required": [{"item": "Programs", "class": "P"}], **({analysis.DECK_READ: True} if stamped else {})}
    async with db.session_factory()() as session:
        made = Opportunity(
            owner=OWNER, slug=slug, company=company or f"Co {slug}", title=f"Role {slug}", fit=fit, origin=origin,
            match_key=f"{company or 'co ' + slug}|role {slug}", status=status, found=date.today(),
            analysis_state=state, analysis=read if state else None, sourced_at=datetime.now(UTC))
        session.add(made)
        await session.commit()
        return made


async def _fresh(role_id) -> Opportunity:
    async with db.session_factory()() as session:
        return await session.get(Opportunity, role_id, populate_existing=True)


@pytest.mark.parametrize("origin", ["owner", "scout"])
async def test_the_writer_refuses_a_role_whose_employer_is_not_named(js, origin):
    role = await _role("link", stages.READY_TO_LAUNCH, company=ADDED_LINK, origin=origin)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await save_documents(session)
        with pytest.raises(packets.PacketError, match="employer isn't named"):
            await packets.start(session, OWNER, role.id)
        assert await session.scalar(select(func.count()).select_from(Packet)) == 0


async def test_go_passes_an_unnamed_role_by_with_its_reason_and_goes_on_with_the_named_ones(js, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened))
    unnamed = await _role("link", stages.READY_TO_LAUNCH, company=ADDED_LINK, fit=9, stamped=True)
    named = await _role("named", stages.READY_TO_LAUNCH, fit=5, stamped=True)
    for made in (unnamed, named):
        async with db.session_factory()() as session:
            await session.execute(update(Opportunity).where(Opportunity.id == made.id).values(
                apply_url=f"https://careers.example.test/open/{made.slug}"))
            await session.commit()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await save_documents(session)
        session.add(Packet(owner=OWNER, role_id=named.id, version=1, status="ready", brief={}, resume={}, letter={},
                           comp={}, reasoning={}, checks=[]))
        await session.commit()
    done = await apply_go.go(OWNER, poll=0, wait=0)
    assert opened == ["https://careers.example.test/open/named"]
    [skipped] = done["skipped"]
    assert skipped["title"] == "Role link" and "employer isn't named yet" in skipped["why"]
    held = await _fresh(unnamed.id)
    assert held.status == stages.READY_TO_LAUNCH and held.go_skip and "isn't named" in held.go_skip
    async with db.session_factory()() as session:
        assert (await session.scalars(select(Packet.role_id))).all() == [named.id], "nothing is written for it"


async def test_findings_for_a_role_that_is_only_a_link_are_refused_without_the_employer_and_title(js):
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        role.company = ADDED_LINK
        await analysis.start(session, OWNER, role)
        await session.commit()
    async with db.session_factory()() as session:
        body = findings(ids["Fabrikam"])
        body.pop("company", None)
        body.pop("title", None)
        result = await analysis.submit(session, OWNER, body)
    assert result["taken"] is False and "company and title" in result["reason"]


async def test_the_days_line_is_ready_to_launch_best_first_and_follows_a_new_score(client):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={"avoid_peak": False, "apply_per_day": 2})
        await session.commit()
    old = datetime.now(UTC) - timedelta(days=30)
    roles = {}
    for slug, percent in (("a", 40), ("b", 30), ("c", 20), ("d", 10)):
        made = await _role(slug, stages.READY_TO_LAUNCH, stamped=True)
        roles[slug] = made.id
        async with db.session_factory()() as session:  # a read role is ordered by its score, not its rank
            await session.execute(update(Opportunity).where(Opportunity.id == made.id).values(
                created_at=old, fit_breakdown={"math": 1, "percent": percent, "asks": []}))
            await session.commit()

    async def line() -> tuple[list[str], int]:
        async with db.session_factory()() as session:
            found, target = await board.ready_line(session, OWNER)
        return [r.title for r in found], target

    async def column() -> list[str]:
        body = (await client.get("/api/v1/board", params={"column": "ready_to_launch"})).json()
        return [c["title"] for c in body["columns"][0]["cards"]]

    assert await line() == (["Role a", "Role b", "Role c", "Role d"], 2), "the whole list, and the day's number"
    assert await column() == ["Role a", "Role b", "Role c", "Role d"], "the screen shows the same order"
    async with db.session_factory()() as session:  # scored again: the newest score is the one that counts
        await session.execute(update(Opportunity).where(Opportunity.id == roles["d"]).values(
            fit_breakdown={"math": 1, "percent": 50, "asks": []}))
        await session.commit()
    assert (await line())[0][:2] == ["Role d", "Role a"], "a new score moves the line on the next read"
    assert await column() == (await line())[0]


async def test_the_days_number_is_the_ready_to_launch_size_not_the_second_impression_one(client):
    """Go's batch is the Ready to Launch size (the owner, 2026-10-28); the deck's size has no say in it."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={"avoid_peak": False, "second_impression_size": 9,
                                                     "ready_to_launch_size": 3})
        await session.commit()
    await _role("a", stages.READY_TO_LAUNCH, stamped=True)
    async with db.session_factory()() as session:
        assert (await board.ready_line(session, OWNER))[1] == 3
        await load_profile(session, OWNER, settings={"avoid_peak": False, "second_impression_size": 9})
        assert (await board.ready_line(session, OWNER))[1] == board.APPLY_BATCH_DEFAULT


async def test_a_role_waiting_for_its_research_again_is_not_in_the_line_but_stays_in_its_column(client):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={"avoid_peak": False, "apply_per_day": 2})
        await session.commit()
    again = await _role("a", stages.READY_TO_LAUNCH, fit=6, state=analysis.WAITING)  # the best fit, but re-checking
    for slug in ("b", "c", "d"):
        await _role(slug, stages.READY_TO_LAUNCH, stamped=True)

    body = (await client.get("/api/v1/board", params={"column": "ready_to_launch"})).json()
    cards = {c["title"]: c for c in body["columns"][0]["cards"]}
    assert set(cards) == {"Role a", "Role b", "Role c", "Role d"}
    assert "re-checked" in cards["Role a"]["next"]
    assert cards["Role d"]["next"] is None, "a role that is the normal run says nothing (docs/PRD.md 24.18)"
    async with db.session_factory()() as session:
        line, target = await board.ready_line(session, OWNER)
    assert target == 2 and again.id not in {r.id for r in line} and len(line) == 3
