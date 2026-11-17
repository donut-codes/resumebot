"""The roles researched before findings that grade nothing were sent back (`test_graded_without_posting.py`) are sent
back by a command, once, after the release (`cli.py send-back-ungraded`): a role that is Done, before Applied, whose
research holds no item and whose text has no requirement lines was graded on the owner's other asks alone. The
command's query picks the candidates in the database, leaves alone a job the owner added and a role nothing can read
again, and a dry run only counts. A start of the app does not run it: a repair done once is a command, not a step of
every start (`loops/startup.py`). Everything is made up."""

import logging
from datetime import UTC, date, datetime
from types import SimpleNamespace

from sqlalchemy import event, select

from app import db
from app.jobsearch import cli, stages
from app.jobsearch.fit import scoring
from app.jobsearch.loops.startup import run_startup_fixups
from app.jobsearch.models import Opportunity
from app.jobsearch.postings import ungraded
from app.jobsearch.research import analysis
from app.models.work import TrailEvent
from tests.builders import pipeline_turn, rank_roles
from tests.jobsearch.test_graded_without_posting import CAREERS, NOTES
from tests.support import OWNER, load_profile

WITH_LINES = CAREERS + "\n\nRequirements\n- Five years of programs\n- A certification"
ANSWERS = [{"id": "company", "answer": "met", "evidence": "the employer's own page"}]
EMPTY = {"required": [], "preferred": [], "responsibilities": [], "notes": NOTES, "at": "2026-10-20T02:05:45+00:00",
         analysis.DECK_READ: True}


async def _role(slug: str, status: str, *, text: str | None = CAREERS, research: dict | None = None,
                rank: int | None = 70, state: str | None = analysis.DONE, origin: str = "scout",
                kind: str = "employer", link: bool = True) -> Opportunity:
    scored = {"answers": ANSWERS, "asks": [{"id": "company", "kind": "ask", "earned": 9}], "total": 9, "percent": 82,
              "math": scoring.MATH} if state else None
    async with db.session_factory()() as session:
        role = Opportunity(
            owner=OWNER, slug=slug, company=f"Co {slug}", title=f"Role {slug}", match_key=f"co {slug}|role {slug}",
            origin=origin, status=status, found=date.today(), source_kind=kind, sourced_at=datetime.now(UTC),
            url=f"https://careers.example.test/{slug}" if link else None,
            source_url=f"https://careers.example.test/{slug}" if link else None,
            description=text, description_at=datetime.now(UTC) if text else None, analysis_state=state,
            analysis=research, fit=9 if state else None, fit_breakdown=scored)
        session.add(role)
        await session.flush()
        if rank is not None:
            await rank_roles(session, {role.id: rank})
        await session.commit()
        return role


async def _fresh(role: Opportunity) -> Opportunity:
    async with db.session_factory()() as session:
        return await session.get(Opportunity, role.id, populate_existing=True)


async def _world() -> dict[str, Opportunity]:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()
    return {
        "ready": await _role("ready", stages.READY_TO_LAUNCH, research=EMPTY),
        "first": await _role("first", stages.FIRST_IMPRESSION, research={**EMPTY, analysis.DECK_READ: False}),
        "followup": await _role("followup", stages.FOLLOWUP_NEEDED, research=EMPTY, rank=None),
        # What is not the repair's:
        "item": await _role("item", stages.SECOND_IMPRESSION, research={
            **EMPTY, "required": [{"item": "Travel", "class": "N", "why": "no resume line shows it"}]}),
        "lines": await _role("lines", stages.READY_TO_LAUNCH, text=WITH_LINES, research=EMPTY),
        "applied": await _role("applied", stages.APPLIED, research=EMPTY),
        "ended": await _role("ended", stages.REJECTED, research=EMPTY),
        "none": await _role("none", stages.FIRST_IMPRESSION, research=None),
        "waiting": await _role("waiting", stages.FIRST_IMPRESSION, research=EMPTY, state=analysis.WAITING),
        # A job the owner added, an Easy Apply and a role with no link: their text may be all there is.
        "added": await _role("added", stages.FIRST_IMPRESSION, research=EMPTY, origin="owner"),
        "easy": await _role("easy", stages.FIRST_IMPRESSION, research=EMPTY, kind="board"),
        "nolink": await _role("nolink", stages.FIRST_IMPRESSION, research=EMPTY, link=False),
    }


SENT = ("ready", "first", "followup", "applied")
LEFT = ("item", "lines", "ended", "none", "waiting", "added", "easy", "nolink")


async def test_a_dry_run_counts_the_roles_and_changes_nothing(js):
    world = await _world()
    async with db.session_factory()() as session:
        found = await ungraded.send_back_ungraded(session, OWNER, dry_run=True)
        await session.commit()
    assert found.roles == 4 and found.by_stage == {
        "Ready to Launch": 1, "First Impression": 1, "Followup needed": 1, "Applied": 1}
    for role in world.values():
        now = await _fresh(role)
        assert (now.analysis_state, now.description, now.fit) == (role.analysis_state, role.description, role.fit)


async def test_the_roles_that_graded_nothing_wait_for_their_text_and_leave_ready(js):
    world = await _world()
    async with db.session_factory()() as session:
        done = await ungraded.send_back_ungraded(session, OWNER)
        await session.commit()
    assert done.roles == 4
    for name in SENT:
        role = await _fresh(world[name])
        assert role.analysis_state == analysis.NO_TEXT, name
        assert role.description is None and role.description_at is None, name
        assert role.fit is None and role.fit_breakdown == {"answers": ANSWERS}, "no score stands over no grade"
        assert role.analysis["notes"] == NOTES, "the old research stays until new research replaces it"
        async with db.session_factory()() as session:
            [said] = (await session.scalars(select(TrailEvent).where(
                TrailEvent.link_id == role.id, TrailEvent.event == "waiting_for_text"))).all()
        assert "not its posting" in (said.detail or ""), name
    for name in LEFT:
        role = await _fresh(world[name])
        assert role.analysis_state == world[name].analysis_state and role.description == world[name].description, name
        assert role.fit == world[name].fit, name
    await pipeline_turn()
    assert (await _fresh(world["ready"])).status == stages.FIRST_IMPRESSION, "it is ranked: it goes back to the deck"
    assert (await _fresh(world["followup"])).status == stages.FOLLOWUP_NEEDED, "it has no rank: it stays"
    assert (await _fresh(world["applied"])).status == stages.APPLIED


async def test_a_second_run_finds_nothing_and_each_role_was_given_the_one_lap(js):
    world = await _world()
    async with db.session_factory()() as session:
        await ungraded.send_back_ungraded(session, OWNER)
        await session.commit()
    async with db.session_factory()() as session:
        assert (await ungraded.send_back_ungraded(session, OWNER)).roles == 0
    for name in SENT:
        assert (await _fresh(world[name])).analysis[analysis.SENT_BACK] == 1, name


async def test_the_query_picks_the_candidates_in_the_database_and_loads_no_other_role(js):
    world = await _world()
    loaded = set()

    def note(target, context):
        loaded.add(target.id)

    event.listen(Opportunity, "load", note)
    try:
        async with db.session_factory()() as session:
            await ungraded.send_back_ungraded(session, OWNER, dry_run=True)
    finally:
        event.remove(Opportunity, "load", note)
    # Only a role that is Done, in a stage a reader asks the text of (before the application, Applied or Active
    # Discussion: 2026-11-05), not added by the owner, readable again, whose research the analyst sent and holds no
    # item. The text that has requirement lines is told apart by its text, read after.
    assert loaded == {world[n].id for n in (*SENT, "lines")}


async def test_one_role_that_fails_is_logged_with_its_id_and_the_rest_still_go(js, monkeypatch, caplog):
    world = await _world()
    broken = world["first"].id
    original = analysis.send_back

    async def flaky(session, role, text, *, notes=""):
        if role.id == broken:
            role.title = "changed inside the savepoint"  # the failed savepoint expires the row, as a real one does
            await session.flush()
            raise RuntimeError("the history could not be written")
        return await original(session, role, text, notes=notes)

    monkeypatch.setattr(analysis, "send_back", flaky)
    async with db.session_factory()() as session:
        done = await ungraded.send_back_ungraded(session, OWNER)
        await session.commit()

    assert done.roles == 3 and done.by_stage == {"Ready to Launch": 1, "Followup needed": 1, "Applied": 1}
    assert str(broken) in caplog.text, "the failure is logged with the role it was about"
    assert (await _fresh(world["first"])).analysis_state == analysis.DONE
    assert (await _fresh(world["first"])).title == world["first"].title, "its savepoint was rolled back whole"
    sent = ("ready", "followup", "applied")
    assert [(await _fresh(world[n])).analysis_state for n in sent] == [analysis.NO_TEXT] * 3


async def test_a_start_of_the_app_does_not_send_them_back(js, caplog):
    world = await _world()
    caplog.set_level(logging.INFO)
    async with db.session_factory()() as session:
        await run_startup_fixups(session, OWNER)
        await session.commit()
    assert "start-up step" not in caplog.text, "a step that fails is logged, and no step failed"
    assert [(await _fresh(world[n])).analysis_state for n in SENT] == [analysis.DONE] * 4
    async with db.session_factory()() as session:
        assert not (await session.scalars(select(TrailEvent).where(TrailEvent.event == "waiting_for_text"))).all()


async def test_the_command_counts_on_a_dry_run_and_sends_the_roles_back_when_it_is_run(js, monkeypatch, capsys):
    world = await _world()
    monkeypatch.setattr(cli.settings, "owner", OWNER)

    assert await cli._send_back_ungraded(SimpleNamespace(dry_run=True)) == 0

    assert capsys.readouterr().out.strip() == (
        "would send 4 roles back for the posting's text: "
        "1 in Applied, 1 in First Impression, 1 in Followup needed, 1 in Ready to Launch")
    assert [(await _fresh(world[n])).analysis_state for n in SENT] == [analysis.DONE] * 4

    assert await cli._send_back_ungraded(SimpleNamespace(dry_run=False)) == 0

    assert capsys.readouterr().out.strip().startswith("sent 4 roles back for the posting's text")
    assert [(await _fresh(world[n])).analysis_state for n in SENT] == [analysis.NO_TEXT] * 4
