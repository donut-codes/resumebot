"""The one-time free title match for the roles already stored (`role_match.code_pass`, command `tag-live-roles`,
docs/PRD.md 30.5). Every name is made up."""

from types import SimpleNamespace

from sqlalchemy import select

from app import db
from app.jobsearch import cli, stages
from app.jobsearch.models import Opportunity, RoleTagJob
from app.jobsearch.work import role_match
from app.settings import settings
from tests.jobsearch.work_queue.support import make_role, posted, saved
from tests.support import OWNER, load_profile

CRITERIA = {"company": "A made-up guild.", "target_roles": [
    {"id": "ledgers", "name": "Harbor ledgering", "titles": ["Harbor Ledgering Lead"], "description": "Keeps ledgers."},
    {"id": "wheels", "name": "Wheelwrighting", "titles": ["Wheelwright"], "description": "Builds wheels."}]}


async def _setup(session, *roles):
    await load_profile(session, OWNER, criteria=CRITERIA)
    await saved(session, *roles)


async def _answers(session):
    rows = (await session.execute(select(Opportunity.title, Opportunity.target_role, Opportunity.matched_by,
                                         Opportunity.matched_by_version))).all()
    return {title: (target, by, version) for title, target, by, version in rows}


async def test_a_live_role_matching_by_title_is_tagged_and_a_gone_quiet_one_is_left(js):
    async with db.session_factory()() as session:
        await _setup(
            session, make_role(title="Senior Wheelwright", status=stages.FIRST_IMPRESSION),
            make_role(title="Baker", status=stages.FIRST_IMPRESSION),
            make_role(title="Cook", status=stages.APPLIED), make_role(title="Wheelwright", status=stages.GONE_QUIET))
        assert await role_match.code_pass(session, OWNER) == (3, 1)
        answers = await _answers(session)
    assert answers["Senior Wheelwright"][:2] == ("wheels", role_match.CODE) and answers["Senior Wheelwright"][2]
    assert answers["Baker"] == (None, None, None) and answers["Wheelwright"] == (None, None, None)


async def test_a_role_a_higher_pass_answered_is_not_touched(js):
    async with db.session_factory()() as session:
        await _setup(
            session, make_role(title="Wheelwright A", status=stages.FIRST_IMPRESSION, matched_by="haiku"),
            make_role(title="Wheelwright B", status=stages.FIRST_IMPRESSION, matched_by="analyst",
                      target_role="ledgers"))
        assert await role_match.code_pass(session, OWNER) == (0, 0)
        answers = await _answers(session)
    assert answers["Wheelwright A"][:2] == (None, "haiku") and answers["Wheelwright B"][:2] == ("ledgers", "analyst")


async def test_a_second_run_matches_none(js):
    async with db.session_factory()() as session:
        await _setup(session, make_role(title="Wheelwright", status=stages.FIRST_IMPRESSION))
        assert await role_match.code_pass(session, OWNER) == (1, 1)
        assert await role_match.code_pass(session, OWNER) == (0, 0)


async def test_the_pass_asks_no_model_opens_no_job_and_posts_once(js):
    async with db.session_factory()() as session:
        await _setup(session, make_role(title="Wheelwright", status=stages.FIRST_IMPRESSION),
                     make_role(title="Harbor Ledgering Lead", status=stages.APPLIED))
        await role_match.code_pass(session, OWNER)
        todos = await posted(session)
        assert (await session.scalars(select(RoleTagJob))).all() == []
    assert sorted(todos) == [("deck.dirty", ""), ("pool.owed", "")]


async def test_more_roles_than_one_chunk_are_all_written(js, monkeypatch):
    monkeypatch.setattr(role_match, "CHUNK", 2)
    async with db.session_factory()() as session:
        await _setup(session, *[make_role(title=f"Wheelwright {n}", status=stages.FIRST_IMPRESSION) for n in range(5)])
        assert await role_match.code_pass(session, OWNER) == (5, 5)
        assert {v[1] for v in (await _answers(session)).values()} == {role_match.CODE}


async def test_the_command_prints_counts_only_and_a_second_run_matches_none(js, monkeypatch, capsys):
    monkeypatch.setattr(settings, "owner", OWNER)
    async with db.session_factory()() as session:
        await _setup(session, make_role(title="Wheelwright", status=stages.FIRST_IMPRESSION),
                     make_role(title="Baker", status=stages.FIRST_IMPRESSION))
        await session.commit()
    capsys.readouterr()
    assert await cli._tag_live_roles(SimpleNamespace()) == 0
    assert capsys.readouterr().out == "2 live roles had no answer: 1 matched by title, 1 left for the Haiku job\n"
    assert await cli._tag_live_roles(SimpleNamespace()) == 0
    assert capsys.readouterr().out == "1 live roles had no answer: 0 matched by title, 1 left for the Haiku job\n"
