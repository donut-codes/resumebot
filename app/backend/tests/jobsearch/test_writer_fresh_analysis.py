"""A role whose research is out of date is researched again before its packet is written (the owner, 2026-10-19: the
writer always works with the freshest information): the app's own ask for a packet (`packets.start` by the app, which
is what Go does) sends it back to the analyst and Go goes on with the other roles; research with no versions kept is
written as before; the owner's own ask for a packet is not held. Synthetic data only."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch import stages
from app.jobsearch.actors import BY_APP
from app.jobsearch.browser import go as apply_go
from app.jobsearch.models import Opportunity, Packet
from app.jobsearch.research import analysis, requeue, staleness
from app.jobsearch.writing import packets
from app.models.work import TrailEvent
from tests.builders import save_documents
from tests.support import OWNER, fake_go_window, load_profile

STALE, FRESH, UNKNOWN = "stale", "fresh", "unknown"
#: The place question is answered, so the final gates do not send the role to the analyst for that; the research is
#: a Second Impression (`DECK_READ`), so it counts.
ANALYSIS = {"required": [{"item": "Programs", "class": "P"}], "place": "not_stated", analysis.DECK_READ: True}
TEXT = "Lead programs for the board. " * 2


async def _setup(*roles: tuple[str, int, str]) -> dict[str, object]:
    """Ready to Launch roles (name, fit, how their research stands); returns their ids by name."""
    made = {}
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await save_documents(session)
        await roster.ensure_roster(session, OWNER)
        stamps = await staleness.stamp_analysis(session, OWNER)
        for name, fit, standing in roles:
            made_under = {STALE: {**stamps, "instructions": 0}, FRESH: stamps, UNKNOWN: None}[standing]
            role = Opportunity(
                owner=OWNER, slug=name, company=f"Co {name}", title=f"Role {name}", fit=fit, origin="scout",
                match_key=f"{name}|role", status=stages.READY_TO_LAUNCH, found=date.today(), analysis_state="done",
                analysis=ANALYSIS, description=TEXT, analysis_made_under=made_under, sourced_at=datetime.now(UTC),
                url=f"https://careers.example.test/open/{name}")
            session.add(role)
            made[name] = role
        await session.commit()
        return {name: role.id for name, role in made.items()}


async def _ask(role_id) -> Packet:
    """The app asks for the role's packet, as Go does."""
    async with db.session_factory()() as session:
        try:
            return await packets.start(session, OWNER, role_id, by=BY_APP)
        finally:  # a role sent back keeps the move to the analyst: the caller commits it, as Go does
            await session.commit()


async def _role(role_id) -> Opportunity:
    async with db.session_factory()() as session:
        return await session.get(Opportunity, role_id, populate_existing=True)


async def _roles_with_packets() -> set:
    async with db.session_factory()() as session:
        return set((await session.scalars(select(Packet.role_id))).all())


async def _requeued(role_id) -> list[str | None]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(TrailEvent.detail).where(
            TrailEvent.link_id == role_id, TrailEvent.event == "research_requeued"))).all())


async def test_the_apps_ask_for_a_packet_sends_a_stale_role_back_to_the_analyst(js):
    ids = await _setup(("a", 4, STALE), ("b", 2, FRESH))
    with pytest.raises(packets.SentBack, match="out of date"):
        await _ask(ids["a"])
    assert await _roles_with_packets() == set(), "no packet for the stale role"
    top = await _role(ids["a"])
    assert top.analysis_state == analysis.WAITING and top.status == stages.READY_TO_LAUNCH
    said = await _requeued(ids["a"])
    assert len(said) == 1 and "instructions changed" in (said[0] or "")
    assert (await _ask(ids["b"])).status == "writing", "the fresh role is written as usual"


async def test_go_marks_a_stale_ready_role_to_be_read_again_keeps_its_place_and_goes_on_with_the_fresh(js, monkeypatch):
    """Replaced behaviour (1.137.1, the owner): the pipeline Go runs first (`pipeline.mark_for_reads`) used to put a
    role with research made under older inputs back on deck. A read still stands on its fit in the best-of queue, so the
    role keeps its Ready place, marked to be read again, and Go asks no packet for it and goes on with the fresh one."""
    monkeypatch.setattr(apply_go, "go_window", fake_go_window([]))
    ids = await _setup(("a", 4, STALE), ("b", 2, FRESH))
    await apply_go.go(OWNER, poll=0, wait=0)
    assert await _roles_with_packets() == {ids["b"]}, "a packet is asked for the fresh role only"
    stale = await _role(ids["a"])
    assert stale.status == stages.READY_TO_LAUNCH, "it keeps its place on its fit"
    assert stale.analysis[analysis.REANALYSE] is True, "marked, so the analyst reads it again"
    assert (await _role(ids["b"])).status == stages.READY_TO_LAUNCH


async def test_research_with_no_versions_kept_is_written_as_before(js):
    ids = await _setup(("a", 4, UNKNOWN))
    assert (await _ask(ids["a"])).status == "writing"
    assert (await _role(ids["a"])).analysis_state == "done"


async def test_a_role_already_marked_for_research_again_is_not_written_or_queued_twice(js):
    ids = await _setup(("a", 4, STALE))
    async with db.session_factory()() as session:
        await requeue.mark_for_reanalysis(session, OWNER, [await session.get(Opportunity, ids["a"])])
        await session.commit()
    marked = len(await _requeued(ids["a"]))
    with pytest.raises(packets.SentBack):
        await _ask(ids["a"])
    assert await _roles_with_packets() == set()
    assert len(await _requeued(ids["a"])) == marked, "not sent to the analyst again"
    assert analysis.REANALYSE in (await _role(ids["a"])).analysis


async def test_a_role_with_the_analyst_is_not_queued_twice(js):
    ids = await _setup(("a", 4, STALE))
    for _ in range(2):
        with pytest.raises(packets.SentBack):
            await _ask(ids["a"])
    assert len(await _requeued(ids["a"])) == 1


async def test_the_owners_own_ask_for_a_packet_is_not_held_for_stale_research(js):
    ids = await _setup(("a", 4, STALE))
    async with db.session_factory()() as session:
        packet = await packets.start(session, OWNER, ids["a"])
        await session.commit()
    assert packet.status == "writing"
    assert (await _role(ids["a"])).analysis_state == "done"
