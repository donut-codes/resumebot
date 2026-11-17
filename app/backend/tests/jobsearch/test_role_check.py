"""Check my roles (docs/PRD.md 31.2): a dry run of the title check over the roles the owner pursued, a sample of recent
postings the title check stopped, or one pasted posting. Whether an agent can take the work now (peak
hours and the agent's state included), the request and its checks, the job's states and timeouts, the runner being
handed the work, the two tools, the answers and the result for each scope, and that nothing is stored on a posting or
a role. Synthetic data only."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text

from app import db
from app.agents import roster
from app.jobsearch.doors import scout as mcp_server
from app.jobsearch.employers import core as employers
from app.jobsearch.jobs import check as role_check
from app.jobsearch.main import app
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.missions import core as missions
from app.jobsearch.missions import search_run as run
from app.jobsearch.models import CheckJob, JobPosting, Opportunity, ParseJob
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.views import for_titles
from app.jobsearch.work import hours as night
from app.jobsearch.work import job_queue, session_kinds, wake
from app.planengine.budget import payload_chars
from app.settings import settings
from tests.builders import MINUTE, POSTING_TEXT, api_url, parse_sweep, settle_housekeeping
from tests.support import AUTH, EXAMPLE_SYSTEM, OWNER, PROFILE, TOKEN, load_profile

NOW = datetime.now(UTC)
BUILDERS = PROFILE["criteria"]["target_roles"][0]
OPS = {"id": "ops", "name": "Operations", "titles": ["Operations Manager"],
       "description": "Keeps the organization running: facilities, vendors and the budget."}
TWO_ROLES = {**PROFILE["criteria"], "target_roles": [BUILDERS, OPS]}
LOOKS = {"company": "A made-up agency.", "target_roles": [
    {"id": "builders", "name": "Builders", "titles": ["Director of Programs"], "description": "Builds programs."},
    {"id": "ops", "name": "Operations", "titles": ["Operations Manager"], "description": "Keeps things running."}]}


@pytest.fixture
async def client(js, monkeypatch, scout_token):
    monkeypatch.setattr(settings, "owner", OWNER)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=TWO_ROLES)
        await session.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def door():
    return SimpleNamespace(headers={"authorization": f"Bearer {TOKEN}"})


async def _runner(*, seen=MINUTE, checked_in=None, kind=None, paused=False, now=None):
    """The runner's connection as the roster keeps it: each stamp that is given is that long before `now`."""
    now = now or datetime.now(UTC)
    async with db.session_factory()() as session:
        conn = await roster.ensure_roster(session, OWNER)
        conn.last_seen_at = None if seen is None else now - seen
        conn.checked_in_at = None if checked_in is None else now - checked_in
        conn.session_kind, conn.paused = kind, paused
        await session.commit()


def _role(n: int, status: str, **more) -> Opportunity:
    """A role found `n` minutes ago (so a lower n is newer)."""
    return Opportunity(owner=OWNER, slug=f"co-{n}", company=f"Company {n}", title=f"Role Title {n}", status=status,
                       fit=3, match_key=f"company {n}|role title {n}", origin="scout",
                       created_at=NOW - timedelta(minutes=n), **more)


async def _pursued_scenario() -> dict[int, uuid.UUID]:
    """Roles 1, 2, 3, 4, 7, 9 and 10 moved forward; 5 (on deck), 6 (gated out) and 8 (gone quiet, never applied) did
    not. Roles 1, 4, 7 and 10 were found for no target role."""
    async with db.session_factory()() as session:
        rows = {
            1: _role(1, "applied"), 2: _role(2, "applied", target_role="builders"),
            3: _role(3, "ready_to_launch", target_role="ops"), 4: _role(4, "ready_to_launch"),
            5: _role(5, "second_impression"), 6: _role(6, "gated_out", screened_out_reason="the place"),
            7: _role(7, "applied"), 8: _role(8, "gone_quiet"),
            9: _role(9, "second_impression", packet_complete=True, target_role="builders"),
            10: _role(10, "active_discussion"),
        }
        session.add_all(rows.values())
        await session.commit()
        rows[7].status = "rejected"  # it reached Applied before it ended
        await session.commit()
        return {n: r.id for n, r in rows.items()}


PURSUED = {1, 2, 3, 4, 7, 9, 10}


async def _stopped_postings(title_stops=10, code_stops=4, start=1) -> list[JobPosting]:
    """Postings the filter stopped: by the title check and by a code gate, the newest first (a lower number is newer);
    one title stop that has closed and one posting that passed are there too."""
    async with db.session_factory()() as session:
        employer = await employers.find_or_make(session, OWNER, "Fabrikam")
        rows = []
        reasons = [("not your kind of job: a sales job", title_stops), ("the place", code_stops)]
        i = start
        for why, n in reasons:
            for _ in range(n):
                rows.append(_posting(employer, i, filtered_by=why))
                i += 1
        rows.append(_posting(employer, 900, filtered_by="not your kind of job: a nurse", closed_at=NOW))
        rows.append(_posting(employer, 901, filtered_by=None, route="reader"))
        session.add_all(rows)
        await session.commit()
        return rows


def _judged_by(why: str | None) -> str | None:
    """Who stopped a posting, from the made-up reason it was given (the app records it when it stops one)."""
    if not why:
        return None
    return "titles" if why.startswith("not your kind") else "gate"


def _posting(employer, i: int, **more) -> JobPosting:
    title = f"Stopped Title {i}"
    more.setdefault("judged_by", _judged_by(more.get("filtered_by")))
    return JobPosting(
        owner=OWNER, employer_id=employer.id, ats=EXAMPLE_SYSTEM, ext_id=f"S-{i}", company=employer.name, title=title,
        match_key=match_key(employer.name, title), location="Remote", work_mode="remote",
        url=f"https://jobs.example.test/S-{i}", last_seen=NOW, first_seen=NOW - timedelta(minutes=i), filtered_at=NOW,
        links=[], text=f"The start of the posting for {title}. " + "More words. " * 40, **more)


async def _job(state=role_check.WAITING, *, age=timedelta(0), taken_ago=None, touched_ago=None, scope="pursued",
               n_items=2, answers=None, **more) -> str:
    """A check job made `age` ago (taken `taken_ago` ago, last answered `touched_ago` ago), straight in the table."""
    now = datetime.now(UTC)
    job = CheckJob(owner=OWNER, scope=scope, state=state, created_at=now - age, looks=LOOKS,
                   taken_at=None if taken_ago is None else now - taken_ago,
                   touched_at=None if touched_ago is None else now - touched_ago,
                   items=[{"ref": str(uuid.uuid4()), "title": f"Title {i}", "company": "Fabrikam"}
                          for i in range(1, n_items + 1)], answers=answers or {}, meta={"total": n_items}, **more)
    async with db.session_factory()() as session:
        session.add(job)
        await session.commit()
    return str(job.id)


async def _row(job_id) -> CheckJob | None:
    async with db.session_factory()() as session:
        return await session.get(CheckJob, uuid.UUID(job_id))


async def _count() -> int:
    async with db.session_factory()() as session:
        return await session.scalar(select(func.count()).select_from(CheckJob))


async def _get(client, job_id):
    return await client.get(f"/api/v1/profile/check/{job_id}")


async def _ask(client, scope, **body) -> str:
    res = await client.post("/api/v1/profile/check", json={"scope": scope, **body})
    assert res.status_code == 200, res.text
    return res.json()["id"]


def _answer(item, answer="yes", role="none", why="a reason") -> dict:
    return {"id": item["id"], "answer": answer, "role": role, "why": why}


async def _answer_all(door, job_id, plan):
    """Take the batches of a check and answer each item by its title: `plan` maps a title to `(answer, role, why)`;
    the default is yes with no role."""
    while True:
        batch = await mcp_server.jobs_match_next(door)
        if batch.get("done"):
            return
        assert batch["id"] == job_id
        answers = [_answer(i, *plan.get(i.get("title"), ("yes", "none", "a reason"))) for i in batch["items"]]
        reply = await mcp_server.jobs_match_report(door, job_id, answers)
        assert reply["ok"] is True, reply
        if reply["done"]:
            return


async def _snapshot() -> dict:
    """Every column of every role and posting, and the stage moves: what a dry run must leave as it was."""
    async with db.session_factory()() as session:
        out = {}
        for model in (Opportunity, JobPosting):
            names = [c.key for c in model.__table__.columns]
            rows = (await session.execute(select(*[getattr(model, n) for n in names]).order_by(model.id))).all()
            out[model.__tablename__] = [tuple(str(v) for v in r) for r in rows]
        out["moves"] = await session.scalar(text("SELECT count(*) FROM js_stage_moves"))
        return out


# --- whether an agent can take the work now ----------


async def test_a_check_is_refused_with_why_when_no_agent_can_take_it_and_nothing_is_kept(client, knocks):
    await _pursued_scenario()
    res = await client.post("/api/v1/profile/check", json={"scope": "pursued"})
    assert res.status_code == 409
    assert res.json() == {"detail": "No agent has checked in yet.", "why": "No agent has checked in yet."}
    await _runner(seen=3 * 60 * MINUTE)
    assert (await client.post("/api/v1/profile/check", json={"scope": "pursued"})).json()["why"] == (
        "No agent has checked in for 3 hours.")
    await _runner(paused=True)
    assert "paused" in (await client.post("/api/v1/profile/check", json={"scope": "pursued"})).json()["why"]
    assert await _count() == 0 and knocks == []


async def test_a_check_asked_for_while_the_agent_is_busy_is_queued_once_and_says_what_it_waits_for(client, knocks):
    await _pursued_scenario()
    await _runner(checked_in=MINUTE, kind="analyst")
    job_id = await _ask(client, "pursued")
    assert knocks == [(job_id, "check_asked")], "woken, so the runner folds it into its next run"
    assert await _ask(client, "pursued") == job_id, "asked again: the same check, not a second one"
    assert await _count() == 1
    got = (await _get(client, job_id)).json()
    assert got["state"] == "waiting" and got["step"] == (
        "Queued: the agent is busy researching a role, which takes about 3 minutes. This starts as soon as it has "
        "finished.")
    await _runner(checked_in=MINUTE, kind="titles")
    assert "busy judging titles" in (await _get(client, job_id)).json()["step"]
    await _runner(checked_in=MINUTE)  # the session ended
    assert (await _get(client, job_id)).json()["step"] == "Waiting for an agent"
    assert (await client.get("/api/v1/agent/ready")).json() == {"ready": True, "why": None}


async def test_a_check_waits_for_claudes_peak_hours_to_pass_and_says_until_when(client, monkeypatch):
    await _pursued_scenario()
    await _runner()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=TWO_ROLES, settings={})  # the default: peak hours avoided
        await session.commit()
    in_peak = datetime(2026, 7, 1, 13, 0, tzinfo=UTC)  # 6:00 Pacific on a summer day
    async with db.session_factory()() as session:
        await _runner(now=in_peak)
        with pytest.raises(role_check.NotReady, match="Claude's peak hours: no Claude work until "):
            await role_check.start(session, OWNER, role_check.PURSUED, now=in_peak)
    monkeypatch.setattr(night, "blackout", lambda s, now=None: "Claude's peak hours: no Claude work until 3 PM.")
    res = await client.post("/api/v1/profile/check", json={"scope": "pursued"})
    assert res.status_code == 409 and res.json()["why"].startswith("Claude's peak hours")
    assert await _count() == 0


# --- the request ----------


async def test_a_request_is_checked_before_anything_is_made(client, knocks):
    await _runner()
    post = client.post
    assert (await post("/api/v1/profile/check", json={})).status_code == 422
    assert (await post("/api/v1/profile/check", json={"scope": "pursued", "sample": 0})).status_code == 422
    unknown = await post("/api/v1/profile/check", json={"scope": "everything"})
    assert unknown.status_code == 422 and "scope is one of pursued, gated, posting" in unknown.json()["detail"]
    for bad in ("", "x" * 49, " " * 60):  # a posting has a fewest, and no most
        res = await post("/api/v1/profile/check", json={"scope": "posting", "text": bad})
        assert res.status_code == 422, len(bad)
    short = await post("/api/v1/profile/check", json={"scope": "posting", "text": "x" * 49})
    assert short.json()["detail"] == f"paste the whole posting: at least {role_check.MIN_CHARS} characters"
    assert (await post("/api/v1/profile/check", json={"scope": "posting"})).status_code == 422
    nothing = await post("/api/v1/profile/check", json={"scope": "pursued"})
    assert nothing.status_code == 422 and nothing.json()["detail"] == role_check.NOTHING_PURSUED
    none_stopped = await post("/api/v1/profile/check", json={"scope": "gated"})
    assert none_stopped.status_code == 422 and none_stopped.json()["detail"] == role_check.NOTHING_STOPPED
    assert await _count() == 0 and knocks == []


async def test_a_check_needs_a_target_role_to_judge_against(client):
    await _pursued_scenario()
    await _runner()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria={**PROFILE["criteria"], "target_roles": []})
        await session.commit()
    for scope, extra in (("pursued", {}), ("gated", {}), ("posting", {"text": POSTING_TEXT})):
        res = await client.post("/api/v1/profile/check", json={"scope": scope, **extra})
        assert res.status_code == 422 and res.json()["detail"] == role_check.NO_ROLES, scope
    assert await _count() == 0


async def test_a_request_makes_a_waiting_job_with_its_items_and_wakes_the_runner_after_the_commit(client, monkeypatch):
    ids = await _pursued_scenario()
    await _runner()
    knocked = []

    async def knock(key, reason=wake.SEARCH_STARTED):
        async with db.session_factory()() as session:  # the job is already saved when the runner is knocked for
            knocked.append((key, reason, await session.get(CheckJob, uuid.UUID(key))))
        return True

    monkeypatch.setattr(wake, "knock", knock)
    job_id = await _ask(client, "pursued")
    assert [(k, r) for k, r, _ in knocked] == [(job_id, "check_asked")] and knocked[0][2] is not None
    row = await _row(job_id)
    assert (row.owner, row.scope, row.state, row.text) == (OWNER, "pursued", "waiting", "")
    assert row.taken_at is None and row.result is None and row.error is None and row.answers == {}
    assert [i["ref"] for i in row.items] == [str(ids[n]) for n in sorted(PURSUED)], "the newest first"
    assert row.meta == {"total": len(PURSUED)}
    assert [r["id"] for r in row.looks["target_roles"]] == ["builders", "ops"]
    assert (await _get(client, job_id)).json() == {
        "id": job_id, "scope": "pursued", "state": "waiting", "step": "Waiting for an agent"}


async def test_asking_again_for_the_same_open_check_finds_it_and_a_different_one_is_another(client, knocks):
    await _pursued_scenario()
    await _stopped_postings()
    await _runner()
    first = await _ask(client, "pursued")
    assert await _ask(client, "pursued") == first and await _count() == 1, "the same check, still open"
    assert await _ask(client, "gated") != first
    one = await _ask(client, "posting", text=POSTING_TEXT)
    assert await _ask(client, "posting", text=f"  {POSTING_TEXT}  ") == one, "the same posting, trimmed"
    assert await _ask(client, "posting", text=POSTING_TEXT + " And more words about the role.") != one
    assert await _count() == 4
    assert [reason for _k, reason in knocks] == ["check_asked"] * 6, "a waiting job is knocked for again"
    batch = await mcp_server.jobs_match_next(SimpleNamespace(headers={"authorization": f"Bearer {TOKEN}"}))
    assert batch["ok"] and batch["id"] == first, "the oldest waiting job is the session's"
    again = await client.post("/api/v1/profile/check", json={"scope": "pursued"})
    assert again.status_code == 200 and again.json()["id"] == first, "a job a session holds is still found, not refused"


# --- the job ----------


async def test_a_job_is_waiting_then_working_once_a_session_takes_it_then_done_with_its_result(client, door):
    ids = await _pursued_scenario()
    await _runner()
    job_id = await _ask(client, "pursued")
    batch = await mcp_server.jobs_match_next(door)
    assert batch["ok"] is True and batch["id"] == job_id and batch["scope"] == "pursued" and batch["more"] == 0
    working = (await _get(client, job_id)).json()
    assert (working["state"], working["step"]) == ("working", "Checking 7 roles")
    assert working["progress"] == {"done": 0, "total": 7} and "result" not in working and "error" not in working
    row = await _row(job_id)
    assert row.state == "working" and row.taken_at is not None and row.touched_at == row.taken_at
    plan = {"Role Title 1": ("yes", "builders", "builds programs"),
            "Role Title 2": ("yes", "ops", "keeps things going"),
            "Role Title 3": ("yes", "builders", "the same work"), "Role Title 4": ("no", "builders", "a sales job"),
            "Role Title 7": ("no", "none", "a nurse"), "Role Title 9": ("yes", "none", "could be anything"),
            "Role Title 10": ("yes", "none", "no role is this")}
    reported = await mcp_server.jobs_match_report(
        door, job_id, [_answer(i, *plan[i["title"]]) for i in batch["items"]])
    assert reported["ok"] is True and reported["taken"] == 7 and reported["done"] is True and reported["more"] == 0
    done = (await _get(client, job_id)).json()
    assert (done["state"], done["step"]) == ("done", "Done") and "progress" not in done and "error" not in done
    result = done["result"]
    assert result["scope"] == "pursued" and result["checked"] == 7 and result["of"] == 7
    assert result["tabs"] == [{"id": "builders", "name": "Builders", "passed": 2, "stopped": 1},
                              {"id": "ops", "name": "Operations", "passed": 1, "stopped": 0}]
    assert {k: result[k]["count"] for k in ("passed", "stopped", "fits_none")} == {
        "passed": 3, "stopped": 2, "fits_none": 2}
    passed = result["passed"]["items"]
    assert [(r["title"], r["tab"], r["tab_name"]) for r in passed] == [
        ("Role Title 1", "builders", "Builders"), ("Role Title 3", "builders", "Builders"),
        ("Role Title 2", "ops", "Operations")], "listed tab by tab"
    assert passed[0] == {"id": str(ids[1]), "title": "Role Title 1", "company": "Company 1", "stage": "applied",
                         "tab": "builders", "tab_name": "Builders", "why": "builds programs"}
    assert (passed[1]["was"], passed[2]["was"]) == ("ops", "builders"), "what the role was found for is kept"
    stopped = result["stopped"]["items"]
    assert [(r["title"], r.get("tab"), r["why"]) for r in stopped] == [
        ("Role Title 4", "builders", "a sales job"), ("Role Title 7", None, "a nurse")]
    assert [(r["title"], r["stage"], r["why"]) for r in result["fits_none"]["items"]] == [
        ("Role Title 9", "second_impression", "could be anything"),
        ("Role Title 10", "active_discussion", "no role is this")]
    row = await _row(job_id)
    assert row.state == "done" and row.finished_at is not None and row.items == [] and row.answers == {}
    assert (await mcp_server.jobs_match_next(door))["done"] is True


async def test_the_pursued_check_is_the_same_set_as_the_roles_counted_in_the_database(client, door):
    ids = await _pursued_scenario()
    await _runner()
    job_id = await _ask(client, "pursued")
    await _answer_all(door, job_id, {"Role Title 1": ("no", "none", "a sales job")})
    result = (await _get(client, job_id)).json()["result"]
    async with db.session_factory()() as session:
        counted = await session.scalar(text(
            "SELECT count(*) FROM js_opportunities WHERE owner = 'tester' AND status <> 'gated_out' AND ("
            " status IN ('ready_to_launch', 'applied', 'active_discussion') OR packet_complete"
            " OR (status IN ('rejected', 'gone_quiet', 'withdrew') AND id IN ("
            "  SELECT role_id FROM js_stage_moves WHERE owner = 'tester'"
            "  AND to_status IN ('applied', 'active_discussion')))"
            " OR (status IN ('rejected', 'gone_quiet', 'withdrew') AND ended_reason = 'they_said_no'))"))
    listed =[r["id"] for kind in ("passed", "stopped", "fits_none") for r in result[kind]["items"]]
    assert counted == len(PURSUED) == result["checked"] == result["of"] == len(listed)
    assert sorted(listed) == sorted(str(ids[n]) for n in PURSUED)
    assert str(ids[5]) not in listed and str(ids[6]) not in listed and str(ids[8]) not in listed
    assert sum(result[k]["count"] for k in ("passed", "stopped", "fits_none")) == counted


async def test_the_applied_roles_that_fit_no_tab_are_listed(client, door):
    ids = await _pursued_scenario()
    await _runner()
    job_id = await _ask(client, "pursued")
    await _answer_all(door, job_id, {})  # every item: yes, with no role
    fits_none = (await _get(client, job_id)).json()["result"]["fits_none"]
    assert fits_none["count"] == len(PURSUED)
    applied = [r for r in fits_none["items"] if r["stage"] == "applied"]
    assert {r["id"] for r in applied} == {str(ids[1]), str(ids[2])}, "the applied roles are listed (7 ended)"


async def test_a_long_list_is_cut_but_its_count_is_whole(client, door, monkeypatch):
    await _pursued_scenario()
    await _runner()
    monkeypatch.setattr(role_check, "MAX_LISTED", 3)
    job_id = await _ask(client, "pursued")
    await _answer_all(door, job_id, {})
    fits_none = (await _get(client, job_id)).json()["result"]["fits_none"]
    assert fits_none["count"] == 7 and len(fits_none["items"]) == 3


async def test_a_check_is_judged_against_the_roles_as_they_stood_when_it_was_asked(client, door):
    await _pursued_scenario()
    await _runner()
    job_id = await _ask(client, "pursued")
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria={**PROFILE["criteria"], "target_roles": [BUILDERS]})
        await session.commit()
    batch = await mcp_server.jobs_match_next(door)
    assert [r["id"] for r in batch["what_to_look_for"]["target_roles"]] == ["builders", "ops"]
    reply = await mcp_server.jobs_match_report(door, job_id, [_answer(batch["items"][0], "yes", "ops")])
    assert reply["ok"] is True, "the role it was asked against is still the one it is answered against"


async def test_a_session_is_handed_what_the_title_pass_is_handed_and_no_examples(client, door):
    await _pursued_scenario()
    await _runner()
    await _ask(client, "pursued")
    batch = await mcp_server.jobs_match_next(door)
    async with db.session_factory()() as session:
        assert batch["what_to_look_for"] == for_titles(await profile_store.current(session, OWNER))
    assert batch["what_to_look_for"]["company"].startswith("A mid-sized public agency")
    assert "examples" not in batch, "the owner's own roles are what is being checked: they are not examples"
    assert "dry run" in batch["how"] and "jobs.match_report" in batch["how"] and "jobs.match_next" in batch["how"]
    assert [i["id"] for i in batch["items"]] == [str(n) for n in range(1, 8)]
    first = batch["items"][0]
    assert set(first) <= {"id", "title", "employer", "place", "text"}
    assert first["title"] == "Role Title 1" and first["employer"] == "Company 1"
    assert set().union(*(set(i) for i in batch["items"])) <= {"id", "title", "employer", "place", "text"}, (
        "what the app says of a role (its stage, what it was found for) is not shown to the session")


async def test_the_session_is_handed_the_batches_one_at_a_time_and_the_job_tells_how_far_along_it_is(client, door):
    async with db.session_factory()() as session:
        session.add_all([_role(n, "applied") for n in range(1, 121)])
        await session.commit()
    await _runner()
    job_id = await _ask(client, "pursued")
    first = await mcp_server.jobs_match_next(door)
    assert len(first["items"]) == role_check.BATCH == 100 and first["more"] == 20
    assert [i["id"] for i in first["items"]][:2] == ["1", "2"] and first["items"][-1]["id"] == "100"
    assert (await mcp_server.jobs_match_next(door))["items"] == first["items"], "asking again is not a second batch"
    assert (await _get(client, job_id)).json()["step"] == "Checking 120 roles"
    reply = await mcp_server.jobs_match_report(door, job_id, [_answer(i) for i in first["items"]])
    assert reply["ok"] and reply["taken"] == 100 and reply["done"] is False and reply["more"] == 20
    assert (await _get(client, job_id)).json()["step"] == "Checked 100 of 120 roles"
    second = await mcp_server.jobs_match_next(door)
    assert [i["id"] for i in second["items"]] == [str(n) for n in range(101, 121)] and second["more"] == 0
    reply = await mcp_server.jobs_match_report(door, job_id, [_answer(i) for i in second["items"]])
    assert reply["done"] is True
    assert (await _get(client, job_id)).json()["result"]["checked"] == 120


async def test_a_batch_is_held_to_the_payload_budget(client, monkeypatch):
    await _pursued_scenario()
    await _runner()
    job = await _row(await _ask(client, "pursued"))
    left = role_check._unanswered(job)
    whole = role_check._fit_batch(job, left)
    size = payload_chars({"ok": True, "what_to_look_for": job.looks, "items": whole, "more": 0,
                          "how": role_check.CHECK_HOW})
    assert len(whole) == 7
    monkeypatch.setattr(role_check, "AGENT_PAYLOAD_BUDGET_CHARS", size - 20)
    fewer = role_check._fit_batch(job, left)
    assert 0 < len(fewer) < 7 and [i["id"] for i in fewer] == [i["id"] for i in whole][: len(fewer)]


async def test_an_unknown_job_is_a_404_in_plain_words(client):
    for key in (str(uuid.uuid4()), "not-an-id"):
        res = await _get(client, key)
        assert res.status_code == 404 and "gone" in res.json()["detail"]


async def test_a_job_not_taken_in_5_minutes_fails_and_one_without_an_answer_for_5_minutes_fails(client):
    young = await _job(age=4 * MINUTE)
    old = await _job(age=6 * MINUTE)
    quick = await _job(role_check.WORKING, age=20 * MINUTE, taken_ago=9 * MINUTE, touched_ago=4 * MINUTE)
    slow = await _job(role_check.WORKING, age=20 * MINUTE, taken_ago=9 * MINUTE, touched_ago=6 * MINUTE)
    assert (await _get(client, young)).json()["state"] == "waiting"
    failed = (await _get(client, old)).json()
    assert failed["state"] == "failed" and failed["step"] == "Stopped"
    assert failed["error"] == role_check.NOT_TAKEN and "5 minutes" in failed["error"] and "result" not in failed
    assert (await _get(client, quick)).json()["state"] == "working", "the 5 minutes count from the last answer"
    late = (await _get(client, slow)).json()
    assert late["state"] == "failed" and late["error"] == role_check.NOT_FINISHED and "5 minutes" in late["error"]
    assert (await _row(old)).state == role_check.WAITING, "reading a job saves nothing"
    await parse_sweep()
    for key in (old, slow):
        row = await _row(key)
        assert row.items == [] and row.answers == {} and row.text == "", "a failed job keeps nothing it was handed"


async def test_a_batch_answered_in_time_gives_the_session_5_more_minutes(client, door):
    await _runner()
    job_id = await _job(role_check.WORKING, age=20 * MINUTE, taken_ago=9 * MINUTE, touched_ago=4 * MINUTE, n_items=2)
    reply = await mcp_server.jobs_match_report(door, job_id, [{"id": "1", "answer": "no", "why": "a sales job"}])
    assert reply["ok"] and reply["taken"] == 1 and reply["done"] is False and reply["more"] == 1
    soon = datetime.now(UTC) + 4 * MINUTE
    async with db.session_factory()() as session:
        assert (await role_check.get(session, OWNER, job_id, soon)).state == "working"
        later = await role_check.get(session, OWNER, job_id, datetime.now(UTC) + 6 * MINUTE)
        assert later.state == "failed", "and not for ever"
        assert later.error == role_check.NOT_FINISHED


async def test_a_finished_job_is_kept_for_a_day_and_then_gone(client):
    result = {"scope": "pursued", "checked": 0}
    kept = await _job(role_check.DONE, age=23 * 60 * MINUTE, result=result)
    gone = await _job(role_check.DONE, age=25 * 60 * MINUTE, result=result)
    assert (await _get(client, kept)).json()["result"] == result
    assert (await _get(client, gone)).status_code == 404
    assert await _row(gone) is not None, "reading a job deletes nothing"
    await parse_sweep()
    assert await _row(gone) is None and await _row(kept) is not None


async def test_a_waiting_job_that_took_too_long_is_failed_for_the_runner_too_so_it_is_never_handed_out(client, door):
    old = await _job(age=6 * MINUTE)
    # The app's own pass fails it (the ask only reads: docs/PRD.md 47.5); a session's own call is a sign of the agent.
    await settle_housekeeping(OWNER)
    assert (await client.get("/api/v1/agent/work", headers=AUTH)).json() == {"next": None}
    assert (await _row(old)).state == "failed"
    assert (await mcp_server.jobs_match_next(door))["done"] is True
    reply = await mcp_server.jobs_match_report(door, old, [{"id": "1", "answer": "no", "why": "x"}])
    assert reply["ok"] is False and "ended" in reply["reason"]


# --- the answers ----------


async def test_a_session_without_the_scouts_token_is_refused_and_reads_nothing(client):
    job_id = await _job()
    ctx = SimpleNamespace(headers={"authorization": "Bearer wrong"})
    for call in (mcp_server.jobs_match_next(ctx), mcp_server.jobs_match_report(ctx, job_id, [])):
        result = await call
        assert result["ok"] is False and "token" in result["reason"]
    assert (await _row(job_id)).state == "waiting"


async def test_a_report_names_every_problem_keeps_the_answers_that_are_right_and_does_not_end_the_job(client, door):
    await _runner()
    job_id = await _job(n_items=6)
    await mcp_server.jobs_match_next(door)
    bad = [
        {"id": "1", "answer": "maybe", "role": "none", "why": "x"},
        {"id": "2", "answer": "yes", "why": "x"},
        {"id": "3", "answer": "yes", "role": "nonsense", "why": "x"},
        {"id": "4", "answer": "no", "why": " "},
        {"id": "99", "answer": "no", "why": "x"},
        "not an answer",
        {"answer": "no", "why": "x"},
        {"id": "5", "answer": "no", "why": "a sales job"},
        {"id": "5", "answer": "no", "why": "twice"},
    ]
    reply = await mcp_server.jobs_match_report(door, job_id, bad)
    assert reply["ok"] is True and reply["taken"] == 1 and reply["done"] is False and reply["more"] == 5
    problems = reply["problems"]
    for fragment in ("1: answer is yes or no", "2: a yes names the target role it fits",
                     "3: role 'nonsense' is not one", "4: say why in one line", "99: not an item of this check",
                     "each answer is {id, answer, role, why}", "an answer needs the id it was handed",
                     "5: not an item of this check, or already answered"):
        assert any(fragment in p for p in problems), fragment
    row = await _row(job_id)
    assert row.state == "working" and list(row.answers) == ["5"]
    assert row.answers["5"] == {"answer": "no", "role": None, "why": "a sales job"}


async def test_a_report_with_nothing_right_in_it_takes_nothing_and_says_how_to_fix_it(client, door):
    await _runner()
    job_id = await _job(n_items=2)
    await mcp_server.jobs_match_next(door)
    reply = await mcp_server.jobs_match_report(door, job_id, [{"id": "1", "answer": "yes", "why": "x"}])
    assert reply["ok"] is False and "nothing was taken" in reply["reason"]
    assert "jobs.match_report again with the same id" in reply["how_to_fix"]
    for answers in ([], "text", None):
        again = await mcp_server.jobs_match_report(door, job_id, answers)
        assert again["ok"] is False
    assert (await _row(job_id)).answers == {}


async def test_a_why_is_one_line_of_at_most_240_characters_and_a_no_may_name_the_nearest_tab_or_none(client, door):
    await _runner()
    job_id = await _job(n_items=3)
    await mcp_server.jobs_match_next(door)
    reply = await mcp_server.jobs_match_report(door, job_id, [
        {"id": "1", "answer": "No", "role": "OPS", "why": "line one\nline two " + "w" * 400},
        {"id": "2", "answer": "no", "why": "x"},
        {"id": "3", "answer": "yes", "role": "none", "why": "x"}])
    assert reply["ok"] and reply["taken"] == 3
    done = (await _row(job_id)).result
    assert done["stopped"]["items"][0]["tab"] == "ops" and len(done["stopped"]["items"][0]["why"]) == 240
    assert "\n" not in done["stopped"]["items"][0]["why"]
    assert done["tabs"][1] == {"id": "ops", "name": "Operations", "passed": 0, "stopped": 1}
    assert done["fits_none"]["count"] == 1 and done["stopped"]["count"] == 2


async def test_a_report_for_a_job_that_is_not_there_has_ended_or_was_never_taken_is_refused(client, door):
    one = [{"id": "1", "answer": "no", "why": "x"}]
    assert "no check job has that id" in (await mcp_server.jobs_match_report(door, str(uuid.uuid4()), one))["reason"]
    assert "no check job has that id" in (await mcp_server.jobs_match_report(door, "nonsense", one))["reason"]
    waiting = await _job()
    assert "not been handed" in (await mcp_server.jobs_match_report(door, waiting, one))["reason"]
    done = await _job(role_check.DONE, age=MINUTE, result={"scope": "pursued"})
    ended = await mcp_server.jobs_match_report(door, done, one)
    assert ended["ok"] is False and "stop" in ended["reason"]


# --- the three checks ----------


async def test_the_gated_check_samples_only_postings_the_title_check_stopped(client, door):
    rows = await _stopped_postings()
    await _runner()
    job_id = await _ask(client, "gated")
    row = await _row(job_id)
    stopped_by = {i["ref"]: i["stopped_by"] for i in row.items}
    assert len(stopped_by) == 10 and row.meta == {"total": 10}
    wanted = {str(p.id) for p in rows if p.filtered_by and p.closed_at is None
              and p.filtered_by.startswith("not your kind")}
    assert set(stopped_by) == wanted, "no code gate, no closed posting and no posting that passed"
    assert all(why.startswith("not your kind of job") for why in stopped_by.values())
    batch = await mcp_server.jobs_match_next(door)
    assert batch["scope"] == "gated" and len(batch["items"]) == 10
    assert set(batch["items"][0]) == {"id", "title", "employer", "place", "text"}
    assert "stopped" not in str(batch["items"]), "the session is not told how it was stopped"
    plan = {"Stopped Title 1": ("yes", "builders", "the same work"), "Stopped Title 2": ("yes", "ops", "keeps things"),
            "Stopped Title 3": ("yes", "builders", "builds programs"), "Stopped Title 4": ("no", "none", "sales")}
    await _answer_all(door, job_id, {**plan})
    result = (await _get(client, job_id)).json()["result"]
    assert result["scope"] == "gated" and result["checked"] == 10 and result["of"] == 10
    assert result["tabs"] == [{"id": "builders", "name": "Builders", "passed": 2, "stopped": 0},
                              {"id": "ops", "name": "Operations", "passed": 1, "stopped": 0}]
    passed = result["passed"]["items"]
    assert [(r["title"], r["tab"]) for r in passed] == [
        ("Stopped Title 1", "builders"), ("Stopped Title 3", "builders"), ("Stopped Title 2", "ops")]
    assert passed[0]["stopped_by"] == "not your kind of job: a sales job" and passed[0]["company"] == "Fabrikam"
    assert result["stopped"]["count"] == 1 and result["fits_none"]["count"] == 6
    assert sum(result[k]["count"] for k in ("passed", "stopped", "fits_none")) == 10


async def test_the_gated_check_takes_a_sample_spread_over_the_recent_stops_of_the_size_asked(client):
    await _stopped_postings(title_stops=60, code_stops=0)
    await _runner()
    job_id = await _ask(client, "gated", sample=10)
    row = await _row(job_id)
    assert row.meta == {"total": 60} and len(row.items) == 10
    assert [i["title"] for i in row.items] == [f"Stopped Title {1 + 5 * k}" for k in range(10)], "spread, newest first"
    second = await _ask(client, "gated", sample=1)
    assert second == job_id, "the same check, still open"
    async with db.session_factory()() as session:
        items, total = await role_check.gated_items(session, OWNER, role_check.DEFAULT_SAMPLE)
        assert role_check.DEFAULT_SAMPLE == 200 and len(items) == 60 and total == 60, "a sample of 200 takes all 60"


async def test_a_pasted_posting_is_matched_to_a_tab_or_none_with_a_line_why(client, door):
    await _runner()
    for answer, role, outcome, matched, tab in (
        ("yes", "ops", "passed", True, {"id": "ops", "name": "Operations"}),
        ("yes", "none", "fits_none", False, None),
        ("no", "builders", "stopped", False, None),
        ("no", "none", "stopped", False, None),
    ):
        text_ = POSTING_TEXT + f" Variant {outcome} {role}."
        job_id = await _ask(client, "posting", text=text_)
        batch = await mcp_server.jobs_match_next(door)
        assert batch["id"] == job_id and batch["scope"] == "posting" and "pasted" in batch["how"]
        assert batch["items"] == [{"id": "1", "text": text_.strip()}]
        reply = await mcp_server.jobs_match_report(
            door, job_id, [{"id": "1", "answer": answer, "role": role, "why": f"because {outcome}"}])
        assert reply["ok"] and reply["done"] is True
        got = (await _get(client, job_id)).json()
        assert got["state"] == "done" and got["result"] == {
            "scope": "posting", "matched": matched, "outcome": outcome, "tab": tab, "why": f"because {outcome}"}
        assert (await _row(job_id)).text == "", "the posting is not kept once it has been read"


async def test_a_posting_the_agent_never_answers_fails_and_keeps_no_text(client):
    job_id = await _job(role_check.WORKING, scope="posting", age=20 * MINUTE, taken_ago=10 * MINUTE,
                        touched_ago=6 * MINUTE, n_items=1, text=POSTING_TEXT)
    got = (await _get(client, job_id)).json()
    assert got["state"] == "failed"
    await parse_sweep()
    assert (await _row(job_id)).text == ""


async def test_the_dry_run_stores_nothing_on_a_posting_or_a_role(client, door):
    await _pursued_scenario()
    await _stopped_postings()
    await _runner()
    before = await _snapshot()
    assert len(before["js_opportunities"]) == 10 and len(before["js_postings"]) == 16
    plan = {"Role Title 1": ("yes", "builders", "x"), "Role Title 4": ("no", "ops", "x"),
            "Stopped Title 1": ("yes", "ops", "x"), "Stopped Title 2": ("no", "none", "x")}
    for scope, extra in (("pursued", {}), ("gated", {}), ("posting", {"text": POSTING_TEXT})):
        job_id = await _ask(client, scope, **extra)
        await _answer_all(door, job_id, plan)
        assert (await _get(client, job_id)).json()["state"] == "done", scope
    assert await _snapshot() == before, "no target role, reason, route, stage or row was written"


# --- what the runner is handed ----------


async def test_a_waiting_check_is_handed_to_the_runner_right_after_a_pasted_posting_and_before_a_search(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=TWO_ROLES)
        assert await session_kinds.next_kind(session, OWNER) is None
        await run.start_round(session, OWNER, started_by="manual")
        await session.commit()
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.SEARCH
    await _job()
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.CHECKER == "checker", (
            "the owner is at the screen")
    parse = ParseJob(owner=OWNER, text=POSTING_TEXT, state="waiting")
    async with db.session_factory()() as session:
        session.add(parse)
        await session.commit()
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.PARSER, "a posting to split goes first"
        parse.state, parse.taken_at = "reading", datetime.now(UTC)
        await session.commit()
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.CHECKER
    async with db.session_factory()() as session:
        job = (await session.scalars(select(CheckJob))).one()
        job.state, job.taken_at, job.touched_at = role_check.WORKING, datetime.now(UTC), datetime.now(UTC)
        await session.commit()
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.SEARCH, (
            "a job a session holds is not handed out again")


async def test_no_check_work_is_handed_out_when_paused_or_in_peak_hours(js, monkeypatch):
    await _job()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=TWO_ROLES)
        await session.commit()
    await _runner(paused=True)
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) is None, "paused"
    await _runner()
    with monkeypatch.context() as peak:
        peak.setattr(night, "in_peak", lambda s, now=None: True)
        async with db.session_factory()() as session:
            assert await session_kinds.next_kind(session, OWNER) is None, "Claude's peak hours"
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.CHECKER


async def test_a_check_left_waiting_with_the_runner_between_sessions_wakes_it_with_the_check_reason(js):
    await _job()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=TWO_ROLES)
        assert await missions.schedule_pass(session, OWNER) == [("waiting", wake.CHECK_ASKED)]
    assert wake.CHECK_ASKED == "check_asked"


async def test_the_runner_is_told_checker_while_a_check_waits_and_nothing_once_it_is_taken(client, door):
    await _job()
    assert (await client.get("/api/v1/agent/work", headers=AUTH)).json()["next"] == "checker"
    await mcp_server.jobs_match_next(door)
    assert (await client.get("/api/v1/agent/work", headers=AUTH)).json() == {"next": None}


async def test_a_checker_session_is_a_kind_the_heartbeat_and_the_agents_page_know(client):
    await _runner()
    started = await client.post("/api/v1/agent/heartbeat", json={"session_id": "checker-abc", "state": "started"},
                                headers=AUTH)
    assert started.json() == {"ok": True, "stop": False}
    async with db.session_factory()() as session:
        assert (await roster.connection_by_slug(session, OWNER, roster.SCOUT)).session_kind == "checker"
    agent = (await client.get(api_url("agents"))).json()[0]
    assert agent["status"] == "working" and agent["current"]["role"] == "checker"
    assert agent["current"]["task_title"] == "Checking your roles"
    assert (await client.get("/api/v1/agent/ready")).json() == {"ready": True, "why": None}, "busy, but not refused"
    async with db.session_factory()() as session:
        assert "busy checking your roles" in await job_queue.queued(session, OWNER)
    await client.post("/api/v1/agent/heartbeat", json={"session_id": "checker-abc", "state": "ended"}, headers=AUTH)
    assert (await client.get("/api/v1/agent/ready")).json()["ready"] is True
    async with db.session_factory()() as session:
        assert await job_queue.queued(session, OWNER) is None
    assert (await client.get(api_url("agents"))).json()[0]["status"] == "idle"
