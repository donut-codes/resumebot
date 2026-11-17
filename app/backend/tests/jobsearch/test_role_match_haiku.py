"""The Haiku pass of the target-role match (docs/PRD.md 30.5): the title job starts by itself for live roles it has not
answered, keeps every answer (a role or none) on the role, never asks a role again once Haiku or the analyst answered,
and never loops on roles it cannot get answered. Every name is made up."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import event, inspect, select

from app import db
from app.agents import roster
from app.jobsearch import stages
from app.jobsearch.doors import tools
from app.jobsearch.models import Opportunity, RoleTagJob, SearchRun
from app.jobsearch.work import hand_outs, role_match, tagging, titles
from tests.jobsearch.work_queue.support import posted_now
from tests.support import OWNER, load_profile

NOW = datetime.now(UTC)
LIVE = stages.SECOND_IMPRESSION


async def _profile(roles: bool = True) -> None:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, **({} if roles else {"criteria": {"company": "A small agency."}}))
        await session.commit()


async def _roles(n=1, status=LIVE, start=0, **fields) -> list[str]:
    async with db.session_factory()() as session:
        rows = [Opportunity(
            owner=OWNER, slug=f"role-{i}", company=f"Company {i}", title=f"Role Title {i}", status=status, fit=3,
            match_key=f"company {i}|role title {i}", origin="scout", created_at=NOW + timedelta(seconds=i), **fields)
            for i in range(start, start + n)]
        session.add_all(rows)
        await session.commit()
        return [str(r.id) for r in rows]


async def _jobs() -> list[RoleTagJob]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(RoleTagJob).order_by(RoleTagJob.started_at))).all())


async def _tick() -> bool:
    """The tagging part of the housekeeping tick, committed: whether a job is open after it."""
    async with db.session_factory()() as session:
        job = await tagging.settle(session, OWNER)
        await session.commit()
        return job is not None


# --- which roles Haiku still has to answer -----------------------------------------------------------------------


async def test_the_live_scope_leaves_out_gated_and_ended_roles_and_those_haiku_or_the_analyst_answered(js):
    for n, status in enumerate((stages.GATED_OUT, stages.REJECTED, stages.GONE_QUIET, stages.WITHDREW)):
        await _roles(1, status=status, start=10 + n)
    undecided, code, haiku_role, haiku_none, analyst = (await _roles(5, start=100, matched_by=None))
    async with db.session_factory()() as session:
        for rid, by, target in ((code, "code", "builders"), (haiku_role, "haiku", "builders"),
                                (haiku_none, "haiku", None), (analyst, "analyst", "builders")):
            row = await session.get(Opportunity, uuid.UUID(rid))
            row.matched_by, row.target_role = by, target
        await session.commit()
        got = {str(r.id) for r in await tagging.untagged(session, OWNER, role_match.LIVE)}
    assert got == {undecided, code}, "no answer, or the code pass's, in a live stage; nothing else"


async def test_with_no_scope_the_job_still_asks_every_role_a_gate_did_not_stop_and_loads_no_description(js):
    ended = await _roles(1, status=stages.REJECTED)
    await _roles(1, status=stages.GATED_OUT, start=5)
    async with db.session_factory()() as session:
        rows = await tagging.untagged(session, OWNER)
        assert [str(r.id) for r in rows] == ended
        assert "description" in inspect(rows[0]).unloaded, "a title session is handed no description: none is read"


# --- what an answer keeps -----------------------------------------------------------------------------------------


async def _answer(role_id: str, role: str | None, version: int | None = 7, *, before=None) -> Opportunity:
    async with db.session_factory()() as session:
        job = RoleTagJob(owner=OWNER, status="open", counts={"version": version} if version else {})
        session.add(job)
        row = await session.get(Opportunity, uuid.UUID(role_id))
        if before:
            before(row)
        tagging.tag(job, row, role)
        await session.commit()
    async with db.session_factory()() as session:
        return await session.get(Opportunity, uuid.UUID(role_id))


async def test_a_role_answer_is_kept_as_haikus_with_the_version_handed(js):
    [rid] = await _roles(1)
    row = await _answer(rid, "builders")
    assert (row.target_role, row.matched_by, row.matched_by_version) == ("builders", "haiku", 7)


async def test_a_none_answer_is_kept_too_and_the_role_is_not_asked_again(js):
    [rid] = await _roles(1)
    row = await _answer(rid, None)
    assert (row.target_role, row.matched_by, row.matched_by_version) == (None, "haiku", 7)
    async with db.session_factory()() as session:
        assert await tagging.untagged(session, OWNER, role_match.LIVE) == []


async def test_an_analyst_answer_that_landed_since_the_hand_out_is_not_overwritten(js):
    [rid] = await _roles(1)

    def analyst_answered(row):
        row.target_role, row.matched_by = "ops", "analyst"

    row = await _answer(rid, "builders", before=analyst_answered)
    assert (row.target_role, row.matched_by) == ("ops", "analyst")


async def test_a_code_answer_is_replaced_by_haikus_even_when_they_agree(js):
    [rid] = await _roles(1, target_role="builders", matched_by="code", matched_by_version=3)
    row = await _answer(rid, "builders")
    assert (row.matched_by, row.matched_by_version) == ("haiku", 7)


# --- the cost of a batch -----------------------------------------------------------------------------------------


async def test_a_report_of_a_hundred_answers_runs_a_bounded_number_of_statements(js):
    await _profile()
    ids = await _roles(100)
    async with db.session_factory()() as session:
        job, _n = await tagging.start(session, OWNER, role_match.LIVE)
        roles = await tagging.waiting(session, job, limit=100)
        tagging.hand_out(job, roles)
        await session.commit()
        seen: list[str] = []
        engine = db.engine().sync_engine

        def noted(_conn, _cursor, statement, _parameters, _context, _many):
            seen.append(statement)

        event.listen(engine, "before_cursor_execute", noted)
        try:
            reply = await titles.answer(session, job, [{"id": i[:8], "role": "builders"} for i in ids])
        finally:
            event.remove(engine, "before_cursor_execute", noted)
    assert reply["taken"] == 100 and reply["problems"] == []
    reads = [q for q in seen if q.lstrip().upper().startswith("SELECT")]
    assert len(reads) <= 5, f"{len(reads)} reads for one report"
    [roles_read] = [q for q in reads if "FROM js_opportunities" in q]
    assert " IN (" in roles_read, "the roles are read for the batch's ids, not every role that needs an answer"


async def test_waiting_gives_a_full_session_though_the_ledger_holds_hundreds_of_roles_at_the_limit(js):
    await _profile()
    await _roles(40)
    async with db.session_factory()() as session:
        job, _n = await tagging.start(session, OWNER, role_match.LIVE)
        stuck = {str(uuid.uuid4()): tagging.HANDED_AT_MOST for _ in range(300)}
        job.counts = {**job.counts, "handed": stuck}
        got = await tagging.waiting(session, job, limit=25)
    assert len(got) == 25, "the ledger's ids are left out in the query: a long one never starves a session"


# --- the job starts by itself -------------------------------------------------------------------------------------


async def test_with_a_target_role_and_a_waiting_live_role_one_live_job_opens_and_posts_for_the_title_session(js):
    await _profile()
    await _roles(2)
    assert await _tick() is True
    [job] = await _jobs()
    assert job.status == "open" and job.counts["stages"] == list(role_match.LIVE) and job.counts["total"] == 2
    assert "ready.titles" in {topic for topic, _key in await posted_now()}
    assert await _tick() is True and len(await _jobs()) == 1, "one job at a time"


async def test_no_job_opens_while_a_search_is_open(js):
    await _profile()
    await _roles(1)
    async with db.session_factory()() as session:
        session.add(SearchRun(owner=OWNER, status="open", started_by="manual"))
        await session.commit()
        assert await roster.open_run(session, OWNER) is not None
    assert await _tick() is False and await _jobs() == []


async def test_no_job_opens_without_a_target_role(js):
    await _profile(roles=False)
    await _roles(1)
    assert await _tick() is False and await _jobs() == []


async def test_no_job_opens_when_only_a_role_that_went_quiet_is_waiting(js):
    await _profile()
    await _roles(1, status=stages.GONE_QUIET)
    assert await _tick() is False and await _jobs() == []


async def test_a_role_handed_twice_and_never_answered_does_not_open_a_third_job(js, monkeypatch):
    monkeypatch.setattr(hand_outs, "ITEM_LEASE_SECONDS", -1)  # the batch is handed again at once: no lease holds
    await _profile()
    [rid] = await _roles(1)
    assert await _tick() is True
    for _ in range(tagging.HANDED_AT_MOST):
        async with db.session_factory()() as session:
            reply = await tools.titles_next_reply(session, OWNER)
            await session.commit()
        assert [p["id"] for p in reply["postings"]] == [rid[:8]]
    assert await _tick() is False, "nothing left to hand: the job closes"
    assert await _tick() is False and len(await _jobs()) == 1, "and the role it could not get answered opens no other"
    async with db.session_factory()() as session:
        assert (await session.get(Opportunity, uuid.UUID(rid))).matched_by is None


async def test_a_new_live_role_after_a_closed_job_opens_the_next_one(js):
    await _profile()
    [first] = await _roles(1)
    assert await _tick() is True
    async with db.session_factory()() as session:
        job = (await session.scalars(select(RoleTagJob))).one()
        row = await session.get(Opportunity, uuid.UUID(first))
        tagging.tag(job, row, "builders", 3)
        await session.commit()
    assert await _tick() is False
    await _roles(1, start=1)
    assert await _tick() is True and len(await _jobs()) == 2

