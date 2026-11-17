"""Find Gaps (docs/PRD.md path 38): the dashboard's bar from the analyst's grades, the sample of stored postings (ten at
most, one from each employer, five at least), the run's round trip through the process and the three tools, the themes
(counted, sorted, graded against the whole master, each with the postings that asked it), the latest run kept for each
role and stale when the master changes, Check again and New sample, what the owner says of a theme, the line drafted
from their words, the peak-hours rule, and that nothing here writes to the resume, a packet or the Pipeline.
Everything is made up."""

import copy
import random
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app import db
from app.harness import instructions as harness_instructions
from app.jobsearch import process
from app.jobsearch.doors import scout as mcp_server
from app.jobsearch.gaps import drafts as gap_drafts
from app.jobsearch.gaps import runs as find_gaps
from app.jobsearch.gaps import sample as gap_sample
from app.jobsearch.gaps import tools as gap_tools
from app.jobsearch.main import app
from app.jobsearch.models import GapAnswerRow, GapDraft, GapRun, JobPosting, Opportunity, Packet
from app.jobsearch.profile import store as profile_store
from app.jobsearch.work import hours as night
from app.jobsearch.work import instruction_files, session_kinds
from app.settings import settings
from tests.builders import MINUTE, RESUME, criteria, parse_runner, save_documents, tab
from tests.support import OWNER, load_profile

NOW = datetime(2026, 10, 9, 22, 0, tzinfo=UTC)
RESEARCHED = {
    "required": [{"item": "a", "class": "P"}, {"item": "b", "class": "P"}, {"item": "c", "class": "T"},
                 {"item": "d", "class": "U"}],
}


@pytest.fixture
async def client(js, monkeypatch, knocks):
    monkeypatch.setattr(settings, "owner", OWNER)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=criteria(tab("builders"), tab("planners")))
        await save_documents(session)
        await session.commit()
    await parse_runner(seen=MINUTE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _stock(employers: int, per: int = 1, role: str = "builders", text: str = "Required: five years of programs."):
    """Roles in the Pipeline that kept their text, matched to a target role: `per` of them for each of `employers`."""
    async with db.session_factory()() as session:
        for i in range(employers):
            for j in range(per):
                company = f"Employer {i + 1}"
                session.add(Opportunity(
                    owner=OWNER, slug=f"e{i}-{j}-{role}", company=company, title=f"Programs Lead {j}", origin="scout",
                    match_key=f"{company.lower()}|programs lead {j} {role}", status="second_impression",
                        target_role=role,
                    url=f"https://jobs.example.test/{i}/{j}", description=f"{text} Posting {i}-{j}."))
        await session.commit()


async def _start(role: str = "builders", **more) -> str:
    async with db.session_factory()() as session:
        run = await find_gaps.start_role(session, OWNER, role, **more)
        await session.commit()
        return str(run.id)


async def _row(run_id: str) -> GapRun:
    async with db.session_factory()() as session:
        return await session.get(GapRun, uuid.UUID(run_id))


async def _view(run_id: str) -> dict:
    async with db.session_factory()() as session:
        run = await find_gaps.get(session, OWNER, run_id)
        out = await find_gaps.view(session, OWNER, run)
        await session.commit()
        return out


async def _take() -> dict:
    async with db.session_factory()() as session:
        reply = await gap_tools.task_reply(session, OWNER)
        await session.commit()
        return reply


def _theme(name: str, asked: list[str], grade: str = "P", cited: tuple = ("b-risk",), kind: str = "required", **more):
    return {"name": name, "kind": kind, "asked_by": asked, "grade": grade, "from": list(cited) if grade != "U" else [],
            "why": "", **more}


async def _report(run_id: str, themes, postings=None) -> dict:
    async with db.session_factory()() as session:
        reply = await gap_tools.report_reply(session, OWNER, run_id, themes, postings)
        await session.commit()
        return reply


def _refs(n: int) -> list[str]:
    return [f"p{i}" for i in range(1, n + 1)]


async def _finished(role: str = "builders", themes=None, **more) -> str:
    """A run for a role that has stored postings, asked for, taken and reported: its id."""
    run_id = await _start(role, **more)
    await _take()
    await _report(run_id, themes or [_theme("Programs at scale", _refs(10)),
                                     _theme("Data catalog tooling", _refs(6), "U", kind="preferred")])
    return run_id


# --- the dashboard ----------


async def test_the_dashboard_is_a_row_for_each_target_role_with_the_analysts_grades_counted_from_the_rows(client):
    async with db.session_factory()() as session:
        for n, state in enumerate(("done", "done", "waiting")):
            session.add(Opportunity(
                owner=OWNER, slug=f"r{n}", company=f"Co {n}", title="Lead", match_key=f"co {n}|lead",
                    status="second_impression",
                target_role="builders", analysis_state=state,
                analysis={"required": [{"item": "x", "class": c} for c in ("PPTU" if n == 0 else "PU")]}))
        session.add(Opportunity(owner=OWNER, slug="other", company="Co 9", title="Lead", match_key="co 9|lead",
                                status="second_impression", target_role="planners", analysis_state=None))
        await session.commit()
    rows = (await client.get("/api/v1/find-gaps")).json()["roles"]
    assert [r["id"] for r in rows] == ["builders", "planners"], "in the order of Search Settings' tabs"
    builders, planners = rows
    assert builders["name"] == "Builders" and builders["researched"] == 2, "the one still waiting is not researched"
    assert builders["counts"] == {"P": 3, "T": 1, "U": 2}
    assert builders["percent"] == {"P": 50, "T": 17, "U": 33}, "whole percents that add to 100"
    assert sum(builders["percent"].values()) == 100
    assert planners["researched"] == 0 and planners["percent"] is None
    assert builders["last_run"] is None and planners["last_run"] is None
    assert (await client.get("/api/v1/find-gaps")).json()["active"] is None



def _python_counts(rows: list[tuple[str, dict]]) -> dict[str, dict]:
    """The counts as the page took them before they moved into the database: walked in Python, row by row."""
    out: dict[str, dict] = {}
    for role_id, found in rows:
        one = out.setdefault(role_id, {"roles": 0, "counts": {g: 0 for g in find_gaps.GRADES}})
        one["roles"] += 1
        for item in (found or {}).get("required") or []:
            if isinstance(item, dict) and item.get("class") in find_gaps.GRADES:
                one["counts"][item["class"]] += 1
    return out


async def test_the_researched_counts_equal_the_python_walk_and_come_back_as_one_row_for_each_role_and_grade(client):
    analyses = [
        ("builders", {"required": [{"item": "a", "class": "P"}, {"item": "b", "class": "P"},
                                   {"item": "c", "class": "T"}]}),
        ("builders", {"required": []}),
        ("builders", {}),
        ("builders", {"required": [{"item": "a", "class": "U"}, {"item": "b", "class": "X"}, {"item": "c"}, "text"]}),
        ("builders", {"required": [{"item": "a", "class": "P"}]}),
        ("planners", {"required": [{"item": "a", "class": "T"}, {"item": "b", "class": "T"}]}),
        ("planners", {"required": {"item": "a", "class": "P"}}),
    ]
    async with db.session_factory()() as session:
        for n, (role, found) in enumerate(analyses):
            session.add(Opportunity(
                owner=OWNER, slug=f"c{n}", company=f"Co {n}", title="Lead", match_key=f"co {n}|lead",
                status="second_impression", target_role=role, analysis_state="done", analysis=found))
        await session.commit()
    seen: list[str] = []
    from sqlalchemy import event
    engine = db.engine().sync_engine

    def noted(_conn, cursor, statement, _parameters, _context, _many):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", noted)
    try:
        async with db.session_factory()() as session:
            got = await find_gaps.researched(session, OWNER, ["builders", "planners", "chiefs"])
    finally:
        event.remove(engine, "before_cursor_execute", noted)
    # A required entry that is not a list reads as no items.
    expected = _python_counts([(r, f if isinstance(f.get("required", []), list) else {}) for r, f in analyses])
    assert got["builders"] == expected["builders"] == {"roles": 5, "counts": {"P": 3, "T": 1, "U": 1}}
    assert got["planners"] == expected["planners"] == {"roles": 2, "counts": {"P": 0, "T": 2, "U": 0}}
    assert got["chiefs"] == {"roles": 0, "counts": {"P": 0, "T": 0, "U": 0}}
    assert len([s for s in seen if s.lstrip().upper().startswith("SELECT")]) == 2, "two counts, one row each"


async def test_the_picker_list_leaves_the_dashboard_and_is_read_in_the_same_order(client):
    async with db.session_factory()() as session:
        for n, (company, title, text) in enumerate([
                ("beta co", "Zed", "Text."), ("Alpha Co", "yak", "Text."), ("Alpha Co", "Able", "Text."),
                ("Alpha Co", "Bare", None), ("Alpha Co", "Blank", "   "), ("Gamma Co", "Any", "Text.")]):
            session.add(Opportunity(
                owner=OWNER, slug=f"p{n}", company=company, title=title, match_key=f"{company}|{title}",
                status="second_impression", description=text))
        await session.commit()
    assert "jobs" not in (await client.get("/api/v1/find-gaps")).json()
    got = (await client.get("/api/v1/find-gaps/jobs")).json()
    assert [(j["company"], j["title"]) for j in got] == [
        ("Alpha Co", "Able"), ("Alpha Co", "yak"), ("beta co", "Zed"), ("Gamma Co", "Any")]
    assert all(set(j) == {"id", "company", "title"} for j in got)


# --- the sample ----------


async def test_a_run_for_a_role_with_fourteen_employers_reads_ten_each_from_a_different_employer(client):
    await _stock(14, per=2)
    run_id = await _start()
    run = await _row(run_id)
    assert run.total == 10 and len(run.postings) == 10 and run.available == 14
    assert len({p["employer"] for p in run.postings}) == 10, "at most one from each employer"
    assert [p["ref"] for p in run.postings] == _refs(10)
    assert (await _view(run_id))["note"] is None


async def test_a_role_with_seven_employers_reads_seven_and_the_page_says_so(client):
    await _stock(7)
    run_id = await _start()
    assert (await _row(run_id)).total == 7
    assert (await _view(run_id))["note"] == "Only 7 employers have a posting for this role; this reads 7."


async def test_a_role_with_four_employers_does_not_run_and_says_why(client, knocks):
    await _stock(4, per=3)
    res = await client.post("/api/v1/find-gaps/runs", json={"role_id": "builders"})
    assert res.status_code == 422
    assert res.json()["detail"] == ("Only 4 employers have a posting stored for Builders; Find Gaps needs at least 5, "
                                    "each from a different employer.")
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(GapRun)) == 0
    assert knocks == []
    await _stock(5, role="planners")
    assert (await client.post("/api/v1/find-gaps/runs", json={"role_id": "planners"})).status_code == 200, "five run"


async def test_a_stored_posting_is_a_role_or_a_read_posting_with_its_text_counted_once_and_only_for_that_role(client):
    async with db.session_factory()() as session:
        role = Opportunity(owner=OWNER, slug="one", company="Alpha", title="Lead", match_key="alpha|lead",
                           status="second_impression", target_role="builders", description="Required: programs.")
        session.add(role)
        await session.flush()
        for company, text, link, tab_id in (("Alpha", "Same posting, as read.", role.id, "builders"),
                                            ("Beta", "Beta's text.", None, "builders"),
                                            ("Gamma", None, None, "builders"), ("Delta", "  ", None, "builders"),
                                            ("Epsilon", "Another tab's.", None, "planners")):
            session.add(JobPosting(owner=OWNER, employer_id=role.id, ats="x", ext_id=company, company=company,
                                   title="Lead", text=text, target_role=tab_id, opportunity_id=link,
                                   first_seen=NOW, last_seen=NOW))
        session.add(Opportunity(owner=OWNER, slug="two", company="Zeta", title="Lead", match_key="zeta|lead",
                                status="second_impression", target_role="builders", description=None))
        await session.commit()
        found = await gap_sample.stored(session, OWNER, "builders")
    assert sorted((s.employer, s.source) for s in found) == [("Alpha", "role"), ("Beta", "posting")]


async def test_a_new_sample_prefers_the_employers_the_last_one_did_not_read_and_check_again_keeps_the_same(client):
    await _stock(14)
    first = await _finished(rng=random.Random(3))
    async with db.session_factory()() as session:
        before = (await session.get(GapRun, uuid.UUID(first))).postings
        again = await find_gaps.check_again(session, OWNER, first)
        await session.commit()
        assert [(p["ref"], p["source_id"], p["text"]) for p in again.postings] == [
            (p["ref"], p["source_id"], p["text"]) for p in before], "the same postings, with no new draw"
        assert again.again is True and again.available == 14
    await _take()
    await _report(str(again.id), [_theme("Programs at scale", _refs(10))])
    async with db.session_factory()() as session:
        sampled = await find_gaps.new_sample(session, OWNER, str(again.id), rng=random.Random(9))
        await session.commit()
        now_read = {p["employer"] for p in sampled.postings}
    read_before = {p["employer"] for p in before}
    assert len(now_read) == 10
    assert {f"Employer {i}" for i in range(1, 15)} - read_before <= now_read, \
        "every employer the sample before did not read is read now"


# --- the run, through the process and the tools ----------


async def test_a_run_waits_in_the_process_for_the_analysts_step_and_its_instruction_file_and_template(client, knocks):
    await _stock(10)
    res = await client.post("/api/v1/find-gaps/runs", json={"role_id": "builders"})
    run_id = res.json()["id"]
    assert knocks == [(run_id, "gaps_asked")]
    run = await _row(run_id)
    assert (run.state, run.kind, run.role_id, run.title) == ("waiting", "role", "builders", "Builders")
    async with db.session_factory()() as session:
        where = await process.engine().where_is(session, run.instance_id)
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.GAPS
    assert where["status"] == "waiting" and where["step"] == process.FINDER_STEP
    (wait,) = where["waiting_for"]
    assert wait["payload"]["role"] == "analyst" and wait["payload"]["instructions"] == "jobsearch.gaps"
    assert wait["payload"]["template"] == "jobsearch.gaps.report"
    assert (await client.get(f"/api/v1/find-gaps/runs/{run_id}")).json()["step"] == "Waiting for an agent"
    again = await client.post("/api/v1/find-gaps/runs", json={"role_id": "planners"})
    assert again.status_code == 409 and again.json()["detail"] == find_gaps.ALREADY, "one run at a time"


async def test_the_session_takes_the_run_reads_its_postings_a_few_at_a_time_and_the_whole_master(client):
    await _stock(10)
    run_id = await _start()
    task = await _take()
    assert task["mode"] == "find" and task["id"] == run_id and task["role"] == "Builders" and task["total"] == 10
    assert task["read_next"] == ["postings", "resume"] and "jobs.gaps_report" in task["how"]
    assert "Finding the gaps" in task["instructions"] and "themes:" in task["response_template"]
    run = await _row(run_id)
    assert run.state == "working" and run.taken_at is not None and run.touched_at is not None
    assert run.resume_version == 1 and run.instructions_version == 1, "the versions it was graded and sent with"
    assert (await _take())["id"] == run_id, "a session that asks again is given the run it holds"
    async with db.session_factory()() as session:
        seen = []
        for page in (0, 1, 2):
            reply = await gap_tools.task_reply(session, OWNER, "postings", page)
            seen += [p["ref"] for p in reply["postings"]]
            assert set(reply["postings"][0]) == {"ref", "employer", "title", "text"}
            assert reply["more"] == {0: 6, 1: 2, 2: 0}[page]
        assert seen == _refs(10)
        resume = (await gap_tools.task_reply(session, OWNER, "resume"))["resume"]
    role = resume["roles"][0]
    assert role["tools"] == ["Visio", "Miro"] and role["evidence"][0]["id"] == "e-board", "evidence and tools count"
    assert "notes" not in resume and resume["achievements"][0]["id"] == "a-statewide"
    assert (await _take()) ["id"] == run_id


async def test_progress_says_how_many_postings_are_read_it_only_goes_up_and_stops_at_the_total(client):
    await _stock(10)
    run_id = await _start()
    await _take()
    assert (await _view(run_id))["step"] == "Starting"
    for said, shown in ((3, 3), (2, 3), (99, 10)):
        async with db.session_factory()() as session:
            assert (await find_gaps.progress(session, OWNER, run_id, said))["read"] == shown
            await session.commit()
    assert (await _view(run_id))["step"] == "10 of 10 read"
    async with db.session_factory()() as session:
        with pytest.raises(find_gaps.GapError, match="whole number"):
            await find_gaps.progress(session, OWNER, run_id, "many")  # type: ignore[arg-type]
    assert (await client.get("/api/v1/find-gaps")).json()["active"] == {
        "id": run_id, "role_id": "builders", "title": "Builders", "kind": "role", "state": "working",
        "step": "10 of 10 read", "read": 10, "total": 10}


async def test_a_run_ending_adds_nothing_to_notifications_or_the_trail(client):
    from app.models.work import Notification, TrailEvent

    await _stock(10)
    async with db.session_factory()() as session:
        before = [await session.scalar(select(func.count()).select_from(t)) for t in (Notification, TrailEvent)]
    await _finished()
    async with db.session_factory()() as session:
        after = [await session.scalar(select(func.count()).select_from(t)) for t in (Notification, TrailEvent)]
    assert after == before


async def test_the_themes_are_counted_sorted_and_graded_against_the_master_with_the_lines_that_back_them(client):
    await _stock(10)
    run_id = await _start()
    await _take()
    themes = [
        _theme("Board communication", _refs(3), cited=("e-board",)),
        _theme("Programs at scale", _refs(10), cited=("b-risk", "b-kb")),
        _theme("Governance and risk", _refs(7), "T", cited=("northwind-lead",), kind="responsibility",
               why="close work"),
        _theme("Data catalog tooling", _refs(7)[2:], "U", kind="preferred"),
    ]
    reply = await _report(run_id, themes)
    assert reply == {"ok": True, "taken": True, "themes": 4}
    view = await _view(run_id)
    assert view["state"] == "done" and view["step"] == "Done" and view["checked_at"] is not None
    assert [(t["name"], t["asked"], t["of"]) for t in view["themes"]] == [
        ("Programs at scale", 10, 10), ("Governance and risk", 7, 10), ("Data catalog tooling", 5, 10),
        ("Board communication", 3, 10)], "most postings first"
    by = {t["name"]: t for t in view["themes"]}
    assert by["Programs at scale"]["words"] == "Proven" and by["Data catalog tooling"]["words"] == "Unproven"
    governance = by["Governance and risk"]
    assert governance["words"] == "Transferable" and governance["kind"] == "responsibility"
    board = "Board deck: Presented the risk register each quarter"
    assert by["Board communication"]["lines"] == [{"id": "e-board", "text": board}], "graded on a line of evidence"
    assert [line["text"] for line in by["Programs at scale"]["lines"]] == [
        "Led the risk working group across client engagements", "Built the practice knowledge base"]
    assert by["Data catalog tooling"]["lines"] == [], "an Unproven theme has none"
    assert by["Data catalog tooling"]["postings"] == ["p3", "p4", "p5", "p6", "p7"], "exactly the postings that asked"
    assert [p["ref"] for p in view["postings"]] == _refs(10)
    assert len({p["employer"] for p in view["postings"]}) == 10
    assert "text" not in view["postings"][0]
    row = await _row(run_id)
    assert row.state == "done" and row.read == 10


async def test_a_report_that_cannot_be_taken_is_refused_with_every_problem_and_the_run_stays_open(client):
    await _stock(10)
    run_id = await _start()
    await _take()
    bad = [
        _theme("Needs a line", ["p1"], "P", cited=()),
        _theme("Cites a line it has not", ["p1"], "T", cited=("made-up",)),
        _theme("Unproven with a line", ["p1"], "U") | {"from": ["b-risk"]},
        _theme("Posting nobody has", ["p99"]),
        _theme("Kind is wrong", ["p1"], kind="nice"),
        _theme("Grade is wrong", ["p1"], "Z"),
        _theme("Twice", ["p1"]), _theme("twice", ["p2"]),
    ]
    with pytest.raises(find_gaps.GapError) as refused:
        await _report(run_id, bad)
    said = str(refused.value)
    for part in ("is P but cites no master line", "made-up, which the master doesn't have",
                 "is U but cites master lines", "p99, which no posting has", "kind must be one of",
                 "grade must be P, T or U", "named alike"):
        assert part in said, part
    assert (await _row(run_id)).state == "working" and (await _row(run_id)).themes == []
    for nothing in (None, [], "themes"):
        with pytest.raises(find_gaps.GapError, match="at least one"):
            await _report(run_id, nothing)
    assert (await _report(run_id, [_theme("Fine", ["p1"])]))["taken"] is True
    with pytest.raises(find_gaps.GapError, match="has ended"):
        await _report(run_id, [_theme("Fine", ["p1"])])


async def test_each_role_keeps_its_latest_run_and_a_run_that_finishes_replaces_the_one_before(client):
    await _stock(10)
    first = await _finished()
    assert (await client.get("/api/v1/find-gaps")).json()["roles"][0]["last_run"]["id"] == first
    second = await _finished(themes=[_theme("Something else", ["p1"])])
    async with db.session_factory()() as session:
        ids = [str(i) for i in (await session.scalars(select(GapRun.id).where(GapRun.role_id == "builders"))).all()]
    assert ids == [second], "the earlier run is gone"
    row = (await client.get("/api/v1/find-gaps")).json()["roles"][0]
    assert row["last_run"]["id"] == second and row["last_run"]["at"] is not None
    assert (await client.get(f"/api/v1/find-gaps/runs/{first}")).status_code == 404
    other = await client.get("/api/v1/find-gaps")
    assert other.json()["roles"][1]["last_run"] is None, "another role's runs are its own"


async def test_a_failed_run_leaves_the_latest_one_in_place_and_says_why(client):
    await _stock(10)
    first = await _finished()
    async with db.session_factory()() as session:
        run = await find_gaps.new_sample(session, OWNER, first)
        await session.commit()
        failed_id = str(run.id)
    async with db.session_factory()() as session:  # nobody took it for ten minutes
        await find_gaps.expire(session, OWNER, datetime.now(UTC) + timedelta(minutes=10))
        await session.commit()
    row = await _row(failed_id)
    assert row.state == "failed" and row.error == find_gaps.NOT_TAKEN
    assert (await _view(first))["state"] == "done", "the run before it stays"
    assert (await client.get("/api/v1/find-gaps")).json()["failed"]["error"] == find_gaps.NOT_TAKEN
    async with db.session_factory()() as session:  # the process behind it is cancelled
        assert (await process.engine().where_is(session, row.instance_id))["status"] != "waiting"


async def test_a_run_a_session_never_finishes_fails_and_a_session_that_ends_with_it_open_fails_it_at_once(client):
    await _stock(10)
    run_id = await _start()
    await _take()
    async with db.session_factory()() as session:
        await find_gaps.expire(session, OWNER, datetime.now(UTC) + timedelta(minutes=19))
        assert (await session.get(GapRun, uuid.UUID(run_id))).state == "working"
        await find_gaps.expire(session, OWNER, datetime.now(UTC) + timedelta(minutes=21))
        await session.commit()
    assert (await _row(run_id)).error == find_gaps.NOT_FINISHED
    await _stock(5, role="planners")
    second = await _start("planners")
    await _take()
    async with db.session_factory()() as session:
        assert await gap_tools.session_ended(session, OWNER) == 1
        await session.commit()
    assert (await _row(second)).state == "failed" and "session ended" in (await _row(second)).error


# --- the master changes: Check again, New sample ----------


async def test_saving_a_master_version_makes_the_run_stale_and_only_new_sample_draws_new_postings(client):
    await _stock(14)
    run_id = await _finished()
    assert (await _view(run_id))["stale"] is False
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "resume", {**RESUME, "summary": "Changed."}, by="test")
        await session.commit()
    assert (await _view(run_id))["stale"] is True, "Your resume changed since this check"
    before = (await _row(run_id)).postings
    res = await client.post(f"/api/v1/find-gaps/runs/{run_id}/check-again")
    again_id = res.json()["id"]
    assert (await _row(again_id)).postings == before, "Check again keeps the same postings"
    task = await _take()
    assert task["mode"] == "check" and task["previous_themes"] == [
        {"name": "Programs at scale", "kind": "required"}, {"name": "Data catalog tooling", "kind": "preferred"}]
    await _report(again_id, [_theme("Programs at scale", _refs(10)), _theme("New ask", _refs(4), "U")])
    fresh = await _view(again_id)
    assert fresh["stale"] is False and [t["name"] for t in fresh["themes"]] == ["Programs at scale", "New ask"]
    assert (await client.get(f"/api/v1/find-gaps/runs/{run_id}")).status_code == 404, "the new check replaced it"
    sampled = await client.post(f"/api/v1/find-gaps/runs/{again_id}/new-sample")
    assert sampled.status_code == 200
    assert (await _row(sampled.json()["id"])).again is False


async def test_check_again_and_new_sample_are_for_a_finished_run_of_a_target_role_only(client):
    await _stock(10)
    run_id = await _start()
    for path in ("check-again", "new-sample"):
        res = await client.post(f"/api/v1/find-gaps/runs/{run_id}/{path}")
        assert res.status_code == 422 and res.json()["detail"] == find_gaps.NOT_READY_RUN
        assert (await client.post(f"/api/v1/find-gaps/runs/{uuid.uuid4()}/{path}")).status_code == 404


# --- a title that is not a target role ----------


def _found(n: int = 6) -> list[dict]:
    return [{"employer": f"Firm {i}", "title": "Head of Programs", "url": f"https://careers.example.test/{i}"}
            for i in range(1, n + 1)]


async def test_a_title_that_is_not_a_target_role_is_searched_on_the_web_and_stores_no_pipeline_role(client, knocks):
    res = await client.post("/api/v1/find-gaps/runs", json={"title": "  Head   of Programs "})
    run_id = res.json()["id"]
    assert knocks == [(run_id, "gaps_asked")]
    run = await _row(run_id)
    assert (run.kind, run.role_id, run.title, run.total, run.postings) == ("new", None, "Head of Programs", 10, [])
    task = await _take()
    assert task["mode"] == "search" and task["read_next"] == ["resume"] and "WebSearch" in task["how"]
    async with db.session_factory()() as session:
        assert (await gap_tools.task_reply(session, OWNER, "postings"))["postings"] == []
    assert (await _view(run_id))["step"] == "Looking for postings"
    with pytest.raises(find_gaps.GapError, match="at least 5 postings"):
        await _report(run_id, [_theme("Programs", ["p1"])], _found(4))
    twice = _found(5) + [{"employer": "FIRM 1", "title": "x", "url": "https://careers.example.test/x"}]
    with pytest.raises(find_gaps.GapError, match="there twice"):
        await _report(run_id, [_theme("Programs", ["p1"])], twice)
    for url in ("javascript:alert(1)", "http://localhost/job", "http://10.0.0.5/job", ""):
        with pytest.raises(find_gaps.GapError, match="public address"):
            await _report(run_id, [_theme("Programs", ["p1"])], [*_found(5), {"employer": "Odd", "url": url}])
    await _report(run_id, [_theme("Programs", _refs(6)), _theme("Budgets", _refs(2), "U")], _found(6))
    view = await _view(run_id)
    assert view["state"] == "done" and view["can_repeat"] is False and view["note"] is None
    assert [(p["employer"], p["url"]) for p in view["postings"]][:1] == [("Firm 1", "https://careers.example.test/1")]
    assert [(t["name"], t["asked"]) for t in view["themes"]] == [("Programs", 6), ("Budgets", 2)]
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(Opportunity)) == 0
        assert await session.scalar(select(func.count()).select_from(JobPosting)) == 0
    for path in ("check-again", "new-sample"):
        assert (await client.post(f"/api/v1/find-gaps/runs/{run_id}/{path}")).status_code == 422


async def test_a_title_that_is_one_of_the_target_roles_is_that_roles_run_and_a_blank_one_is_refused(client):
    await _stock(6, role="planners")
    res = await client.post("/api/v1/find-gaps/runs", json={"title": "planners"})
    assert res.status_code == 200
    assert (await _row(res.json()["id"])).role_id == "planners"
    assert (await client.post("/api/v1/find-gaps/runs", json={"title": "   "})).status_code == 422
    assert (await client.post("/api/v1/find-gaps/runs", json={})).status_code == 422
    assert (await client.post("/api/v1/find-gaps/runs", json={"role_id": "nope"})).status_code == 404


async def test_a_run_for_a_new_title_goes_after_a_day_and_so_does_a_failed_one(client):
    async with db.session_factory()() as session:
        for n, (kind, state, role) in enumerate((("new", "done", None), ("role", "failed", "builders"),
                                                 ("role", "done", "builders"))):
            session.add(GapRun(owner=OWNER, role_id=role, title=f"T{n}", kind=kind, state=state,
                               created_at=NOW - timedelta(days=2)))
        await session.commit()
        await find_gaps.expire(session, OWNER, NOW)
        await session.commit()
        left = (await session.scalars(select(GapRun.title))).all()
    assert left == ["T2"], "a target role's latest run stays"


# --- what the owner says of a theme ----------


async def _snapshot() -> dict:
    """What a theme's answer must never touch: the resume's version, every role's score and research, the packets."""
    async with db.session_factory()() as session:
        roles = [(r.slug, r.fit, r.status, r.analysis, r.fit_breakdown) for r in (
            await session.scalars(select(Opportunity).order_by(Opportunity.slug))).all()]
        return {"resume": await profile_store.newest_version(session, OWNER, "resume"), "roles": roles,
                "packets": await session.scalar(select(func.count()).select_from(Packet)),
                "profile": await profile_store.counts(session, OWNER)}


async def test_not_relevant_hides_a_theme_for_that_role_on_later_runs_and_n_hidden_brings_it_back(client):
    await _stock(10)
    await _stock(10, role="planners")
    async with db.session_factory()() as session:
        session.add(Opportunity(owner=OWNER, slug="researched", company="Zed", title="Lead", match_key="zed|lead",
                                status="second_impression", fit=3, target_role="builders", analysis_state="done",
                                analysis=RESEARCHED))
        await session.commit()
    run_id = await _finished()
    before = await _snapshot()
    res = await client.put(f"/api/v1/find-gaps/runs/{run_id}/themes/data-catalog-tooling/answer",
                           json={"answer": "not_relevant"})
    assert res.status_code == 200 and res.json()["answer"] == "not_relevant"
    view = (await client.get(f"/api/v1/find-gaps/runs/{run_id}")).json()
    assert [t["name"] for t in view["themes"]] == ["Programs at scale"]
    assert view["hidden"] == [{"id": res.json()["id"], "name": "Data catalog tooling", "kind": "preferred"}]
    # the next run for the role, which finds the same ask in the agent's words, still hides it
    again = (await client.post(f"/api/v1/find-gaps/runs/{run_id}/new-sample")).json()["id"]
    task = await _take()
    assert task["answered"] == [{"theme": "Data catalog tooling", "kind": "preferred", "answer": "not_relevant"}]
    await _report(again, [_theme("Programs at scale", _refs(10)), _theme("Data catalog tooling", _refs(4), "U")])
    assert [t["name"] for t in (await _view(again))["themes"]] == ["Programs at scale"]
    # no other role is affected
    other = await _finished("planners")
    assert {t["name"] for t in (await _view(other))["themes"]} == {"Programs at scale", "Data catalog tooling"}
    assert (await _view(other))["hidden"] == []
    # bringing it back shows it again
    hidden_id = view["hidden"][0]["id"]
    assert (await client.delete(f"/api/v1/find-gaps/answers/{hidden_id}")).status_code == 200
    back = await _view(again)
    assert [t["name"] for t in back["themes"]] == ["Programs at scale", "Data catalog tooling"] and back["hidden"] == []
    assert (await client.delete(f"/api/v1/find-gaps/answers/{hidden_id}")).status_code == 404
    assert await _snapshot() == before, "no resume, packet, grade or score changed"


async def test_i_havent_marks_the_theme_a_real_gap_and_it_carries_over_and_changes_no_document(client):
    await _stock(10)
    run_id = await _finished()
    before = await _snapshot()
    res = await client.put(f"/api/v1/find-gaps/runs/{run_id}/themes/data-catalog-tooling/answer",
                           json={"answer": "not_done"})
    assert res.status_code == 200
    themes = {t["name"]: t for t in (await _view(run_id))["themes"]}
    assert themes["Data catalog tooling"]["answer"] == "not_done" and themes["Data catalog tooling"]["answer_id"]
    assert themes["Programs at scale"]["answer"] is None
    assert await _snapshot() == before
    bad = await client.put(f"/api/v1/find-gaps/runs/{run_id}/themes/data-catalog-tooling/answer", json={"answer": "x"})
    assert bad.status_code == 422
    missing = await client.put(f"/api/v1/find-gaps/runs/{run_id}/themes/no-such/answer", json={"answer": "not_done"})
    assert missing.status_code == 404 and missing.json()["detail"] == find_gaps.NO_SUCH_THEME
    answer_id = themes["Data catalog tooling"]["answer_id"]
    assert (await client.delete(f"/api/v1/find-gaps/answers/{answer_id}")).status_code == 200
    assert (await _view(run_id))["themes"][1]["answer"] is None, "taken back"
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(GapAnswerRow)) == 0


async def test_a_run_a_check_and_every_answer_but_add_to_resume_leave_the_masters_version_unchanged(client):
    await _stock(10)
    before = await _snapshot()
    run_id = await _finished()
    for answer in ("not_done", "not_relevant"):
        await client.put(f"/api/v1/find-gaps/runs/{run_id}/themes/programs-at-scale/answer", json={"answer": answer})
    await client.post(f"/api/v1/find-gaps/runs/{run_id}/check-again")
    assert await _snapshot() == before


# --- the line drafted from the owner's words, and Add to resume ----------

WORDS = "At my last job I picked and rolled out a data catalog for finance and about 40 reports."


async def test_the_owners_words_become_a_drafted_line_that_holds_no_number_they_did_not_say(client, knocks):
    await _stock(10)
    run_id = await _finished()
    res = await client.post(f"/api/v1/find-gaps/runs/{run_id}/themes/data-catalog-tooling/draft", json={"words": WORDS})
    draft_id = res.json()["id"]
    assert res.status_code == 200 and knocks[-1] == (draft_id, "gaps_asked")
    assert (await client.get(f"/api/v1/find-gaps/drafts/{draft_id}")).json() == {
        "id": draft_id, "state": "waiting", "step": "Waiting for an agent"}
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.GAPS
    task = await _take()
    assert task["mode"] == "draft" and task["id"] == draft_id and task["words"] == WORDS
    assert task["theme"] == {"name": "Data catalog tooling", "kind": "preferred"}
    assert "begins with a verb" in task["how"]
    assert (await client.get(f"/api/v1/find-gaps/drafts/{draft_id}")).json()["step"] == "Drafting the line"
    async with db.session_factory()() as session:
        for bad, said in (("", "as text"), ("x" * 1501, "at most 1500"),
                          ("Picked and rolled out a catalog for 140 reports.", "holds 140")):
            with pytest.raises(find_gaps.GapError, match=said):
                await gap_tools.report_reply(session, OWNER, draft_id, draft=bad)
        line = "Picked and rolled out a data catalog for finance and about 40 reports."
        assert (await gap_tools.report_reply(session, OWNER, draft_id, draft=line))["taken"] is True
        await session.commit()
    done = (await client.get(f"/api/v1/find-gaps/drafts/{draft_id}")).json()
    assert done == {"id": draft_id, "state": "done", "step": "Done", "draft": line}
    async with db.session_factory()() as session:
        row = await session.get(GapDraft, uuid.UUID(draft_id))
        assert row.words == "", "the owner's words are dropped when it ends"
        with pytest.raises(find_gaps.GapError, match="has ended"):
            await gap_tools.report_reply(session, OWNER, draft_id, draft=line)


async def test_a_draft_needs_enough_words_a_finished_run_and_a_theme_of_it_and_a_line_nobody_took_fails(client):
    await _stock(10)
    run_id = await _finished()
    path = f"/api/v1/find-gaps/runs/{run_id}/themes"
    assert (await client.post(f"{path}/data-catalog-tooling/draft", json={"words": "short"})).status_code == 422
    assert (await client.post(f"{path}/no-such/draft", json={"words": WORDS})).status_code == 404
    waiting = (await client.post("/api/v1/find-gaps/runs", json={"title": "Some other title"})).json()["id"]
    unfinished = await client.post(f"/api/v1/find-gaps/runs/{waiting}/themes/x/draft", json={"words": WORDS})
    assert unfinished.status_code == 404
    draft_id = (await client.post(f"{path}/data-catalog-tooling/draft", json={"words": WORDS})).json()["id"]
    async with db.session_factory()() as session:
        await gap_drafts.expire(session, OWNER, datetime.now(UTC) + timedelta(minutes=6))
        await session.commit()
    failed = (await client.get(f"/api/v1/find-gaps/drafts/{draft_id}")).json()
    assert failed["state"] == "failed" and failed["error"] == gap_drafts.NOT_TAKEN
    assert (await client.get(f"/api/v1/find-gaps/drafts/{uuid.uuid4()}")).status_code == 404


async def test_add_to_resume_is_the_resumes_own_save_with_a_why_a_history_line_and_an_undo(client):
    """The page builds the new master from the one it loaded and saves it through PUT /profile/resume with the version
    it loaded; nothing else in Find Gaps writes the resume."""
    before = (await client.get("/api/v1/documents")).json()
    version = before["versions"]["resume"]
    resume = copy.deepcopy(before["resume"])
    resume["roles"][0]["evidence"].append({"id": "gap-catalog", "title": "Data catalog tooling",
                                           "text": "Picked and rolled out a data catalog."})
    saved = await client.put("/api/v1/profile/resume", json={"data": resume, "why": "Added from Find Gaps",
                                                              "base_version": version})
    assert saved.status_code == 200 and saved.json() == {"saved": True, "version": version + 1}
    history = (await client.get("/api/v1/profile/resume/history")).json()["versions"]
    assert history[0]["why"] == "Added from Find Gaps" and history[0]["version"] == version + 1
    assert history[0]["changed"] == ["changed roles: northwind-lead"]
    stale = await client.put("/api/v1/profile/resume", json={"data": resume, "why": "Added from Find Gaps",
                                                              "base_version": version})
    assert stale.status_code == 409, "the page's copy is out of date: nothing is stored"
    undone = await client.post(f"/api/v1/profile/resume/versions/{version}/restore")
    assert undone.status_code == 200
    now = (await client.get("/api/v1/documents")).json()
    assert now["resume"] == before["resume"], "Undo restores the one before"
    assert now["versions"]["resume"] == version + 2, "and is itself in the history"


# --- peak hours, the agent's work, the files ----------


async def test_in_peak_hours_every_request_is_refused_with_the_sentence_import_your_resume_gives(client, monkeypatch):
    await _stock(10)
    run_id = await _finished()
    sentence = "Claude's peak hours: no Claude work until 5:00 PM."
    with monkeypatch.context() as peak:
        peak.setattr(night, "blackout", lambda s, now=None: sentence)
        expected = (await client.post("/api/v1/profile/parse/resume", json={"text": "x" * 60})).json()
        assert expected == {"detail": sentence, "why": sentence}, "what Import your resume says"
        for path, body in (("/api/v1/find-gaps/runs", {"role_id": "builders"}),
                           ("/api/v1/find-gaps/runs", {"title": "A"}),
                           (f"/api/v1/find-gaps/runs/{run_id}/check-again", None),
                           (f"/api/v1/find-gaps/runs/{run_id}/new-sample", None),
                           (f"/api/v1/find-gaps/runs/{run_id}/themes/programs-at-scale/draft", {"words": WORDS})):
            res = await client.post(path, json=body) if body else await client.post(path)
            assert res.status_code == 409 and res.json() == expected, path
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(GapRun)) == 1, "nothing was started"
    assert (await client.post("/api/v1/find-gaps/runs", json={"role_id": "builders"})).status_code == 200, "outside it"


async def test_an_agent_that_is_not_there_or_is_paused_is_refused_with_why(client):
    await _stock(10)
    await parse_runner(seen=3 * 60 * MINUTE)
    res = await client.post("/api/v1/find-gaps/runs", json={"role_id": "builders"})
    assert res.status_code == 409 and res.json()["why"] == "No agent has checked in for 3 hours."
    await parse_runner(seen=MINUTE, paused=True)
    why = (await client.post("/api/v1/find-gaps/runs", json={"role_id": "builders"})).json()["why"]
    assert why == "The agent is paused: it is handed no work until you resume it."
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(GapRun)) == 0


# --- an agent busy with other work queues the run; it does not refuse it ----------

QUEUED_BEHIND_RESEARCH = ("Queued: the agent is busy researching a role, which takes about 3 minutes. "
                          "This starts as soon as it has finished.")


async def test_a_busy_agent_does_not_refuse_a_run_it_is_queued_woken_for_and_the_page_is_told_so(client, knocks):
    """The catch-up researches roles one after another for hours (hundreds, about 3 minutes each): Find gaps was off the
    whole time, with "The agent is busy researching a role"."""
    await _stock(10)
    await parse_runner(seen=MINUTE, checked_in=MINUTE, kind="analyst")
    assert (await client.get("/api/v1/agent/ready")).json() == {"ready": True, "why": None}
    res = await client.post("/api/v1/find-gaps/runs", json={"role_id": "builders"})
    assert res.status_code == 200
    run_id = res.json()["id"]
    assert knocks == [(run_id, "gaps_asked")], "the runner is knocked for: it folds the wake into its next run"
    row = await _row(run_id)
    assert row.state == "waiting" and row.taken_at is None
    assert (await client.get(f"/api/v1/find-gaps/runs/{run_id}")).json()["step"] == QUEUED_BEHIND_RESEARCH
    active = (await client.get("/api/v1/find-gaps")).json()["active"]
    assert (active["id"], active["state"], active["step"]) == (run_id, "waiting", QUEUED_BEHIND_RESEARCH)
    await parse_runner(seen=MINUTE, checked_in=MINUTE, kind="scout")
    assert "busy with a search. This starts as soon as it has finished." in (
        await client.get(f"/api/v1/find-gaps/runs/{run_id}")).json()["step"], "not told how long: the app can't tell"
    await parse_runner(seen=MINUTE, checked_in=MINUTE)  # the session ended: between two sessions
    assert (await client.get(f"/api/v1/find-gaps/runs/{run_id}")).json()["step"] == "Waiting for an agent"
    assert (await _take())["id"] == run_id and (await _row(run_id)).state == "working"
    assert (await client.get(f"/api/v1/find-gaps/runs/{run_id}")).json()["step"] == "Starting", "no longer queued"


async def test_check_again_and_new_sample_are_queued_behind_a_busy_agent_too(client):
    await _stock(14)
    first = await _finished(rng=random.Random(3))
    await parse_runner(seen=MINUTE, checked_in=MINUTE, kind="analyst")
    again = await client.post(f"/api/v1/find-gaps/runs/{first}/check-again")
    assert again.status_code == 200 and (await _row(again.json()["id"])).state == "waiting"
    await _take()
    await _report(again.json()["id"], [_theme("Programs at scale", _refs(10))])
    sample = await client.post(f"/api/v1/find-gaps/runs/{again.json()['id']}/new-sample")
    assert sample.status_code == 200 and (await _row(sample.json()["id"])).state == "waiting"


async def test_a_run_asked_for_while_a_role_is_researched_starts_before_the_catch_up_s_next_role(client, monkeypatch):
    """`session_kinds.next_kind` hands Find Gaps out ahead of the roles, and the catch-up's next role is handed on by
    the app's own pass (`housekeeping.settle`), never by the walk: the run starts when the session that is on a role
    ends, not after the last of the roles."""
    from app.jobsearch.missions import catch_up

    handed: list[str] = []
    hand_on = catch_up.hand_on

    async def spy(session, owner):
        handed.append(owner)
        await hand_on(session, owner)

    monkeypatch.setattr(catch_up, "hand_on", spy)
    await _stock(10)
    async with db.session_factory()() as session:  # roles with the analyst, as the catch-up leaves them
        for n in (1, 2):
            session.add(Opportunity(
                owner=OWNER, slug=f"catch-{n}", company=f"Catch {n}", title="Analyst", origin="tracker",
                match_key=f"catch {n}|analyst", status="second_impression", analysis_state="waiting",
                analysis_asked_at=datetime.now(UTC) - timedelta(minutes=n)))
        await session.commit()
    await parse_runner(seen=MINUTE, checked_in=MINUTE, kind="analyst")
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.ANALYSIS, (
            "the roles come one after another")
    assert handed == [], "a look hands nothing on"
    run_id = (await client.post("/api/v1/find-gaps/runs", json={"role_id": "builders"})).json()["id"]
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.GAPS, "the owner's run goes first"
    assert handed == [], "the catch-up's next role is not handed out ahead of it"
    await _take()
    await _report(run_id, [_theme("Programs at scale", _refs(10))])
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.ANALYSIS, (
            "and the catch-up goes on once it is done")
    assert handed == [], "and still no look hands on a role"


async def test_a_queued_run_is_never_started_twice(client, knocks):
    await _stock(10)
    await _stock(6, role="planners")
    await parse_runner(seen=MINUTE, checked_in=MINUTE, kind="analyst")
    first = (await client.post("/api/v1/find-gaps/runs", json={"role_id": "builders"})).json()["id"]
    for body in ({"role_id": "builders"}, {"role_id": "planners"}, {"title": "Some other title"}):
        again = await client.post("/api/v1/find-gaps/runs", json=body)
        assert again.status_code == 409 and again.json()["detail"] == find_gaps.ALREADY, body
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(GapRun)) == 1
    assert knocks == [(first, "gaps_asked")], "and the runner is not knocked for the ones that were refused"


async def test_in_peak_hours_a_busy_agent_still_refuses_with_the_peak_hours_sentence(client, monkeypatch):
    await _stock(10)
    run_id = await _finished()
    await parse_runner(seen=MINUTE, checked_in=MINUTE, kind="analyst")
    sentence = "Claude's peak hours: no Claude work until 5:00 PM."
    with monkeypatch.context() as peak:
        peak.setattr(night, "blackout", lambda s, now=None: sentence)
        assert (await client.get("/api/v1/agent/ready")).json() == {"ready": False, "why": sentence}
        for path, body in (("/api/v1/find-gaps/runs", {"role_id": "builders"}),
                           (f"/api/v1/find-gaps/runs/{run_id}/check-again", None),
                           (f"/api/v1/find-gaps/runs/{run_id}/themes/programs-at-scale/draft", {"words": WORDS})):
            res = await client.post(path, json=body) if body else await client.post(path)
            assert res.status_code == 409 and res.json() == {"detail": sentence, "why": sentence}, path
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(GapRun)) == 1, "nothing was started"
        assert await session.scalar(select(func.count()).select_from(GapDraft)) == 0
    assert (await client.post("/api/v1/find-gaps/runs", json={"role_id": "builders"})).status_code == 200, "outside it"


async def test_a_run_waits_out_a_long_session_and_fails_five_minutes_after_the_agent_was_last_at_work(client):
    """The five minutes to be taken count from the agent's last word in a session: a role being researched, or a search,
    is no reason to fail what waits behind it, and its end is where the five minutes begin."""
    await _stock(10)
    await parse_runner(seen=MINUTE, checked_in=MINUTE, kind="scout")
    run_id = await _start()
    async with db.session_factory()() as session:  # asked for 40 minutes ago, behind a session that has gone on since
        (await session.get(GapRun, uuid.UUID(run_id))).created_at = datetime.now(UTC) - timedelta(minutes=40)
        await session.commit()
    assert (await _view(run_id))["state"] == "waiting"
    await parse_runner(seen=4 * MINUTE)  # the session ended 4 minutes ago
    assert (await _view(run_id))["state"] == "waiting"
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.GAPS, "and the runner is still handed it"
    await parse_runner(seen=6 * MINUTE)  # 6 minutes: nobody took it
    failed = await _view(run_id)
    assert failed["state"] == "failed" and failed["error"] == find_gaps.NOT_TAKEN


async def test_a_line_asked_for_while_the_agent_is_busy_is_queued_too(client, knocks):
    await _stock(10)
    run_id = await _finished()
    await parse_runner(seen=MINUTE, checked_in=MINUTE, kind="analyst")
    res = await client.post(f"/api/v1/find-gaps/runs/{run_id}/themes/data-catalog-tooling/draft", json={"words": WORDS})
    assert res.status_code == 200
    draft_id = res.json()["id"]
    assert knocks[-1] == (draft_id, "gaps_asked")
    assert (await client.get(f"/api/v1/find-gaps/drafts/{draft_id}")).json() == {
        "id": draft_id, "state": "waiting", "step": QUEUED_BEHIND_RESEARCH}
    async with db.session_factory()() as session:  # asked 12 minutes ago, behind a session that has gone on since
        (await session.get(GapDraft, uuid.UUID(draft_id))).created_at = datetime.now(UTC) - timedelta(minutes=12)
        await session.commit()
    assert (await client.get(f"/api/v1/find-gaps/drafts/{draft_id}")).json()["state"] == "waiting"
    await parse_runner(seen=6 * MINUTE)  # the session ended 6 minutes ago and nobody took it
    failed = (await client.get(f"/api/v1/find-gaps/drafts/{draft_id}")).json()
    assert failed["state"] == "failed" and failed["error"] == gap_drafts.NOT_TAKEN


async def test_a_session_works_through_the_door_and_with_nothing_waiting_it_is_told_to_stop(js, door):
    nothing = await mcp_server.jobs_gaps(door)
    assert nothing["ok"] is True and nothing["done"] is True and nothing["task"] is None
    assert (await mcp_server.jobs_gaps(door, part="postings"))["note"].startswith("You hold no run")
    refused = await mcp_server.jobs_gaps(door, part="nonsense")
    assert refused["ok"] is False and "the parts are task, postings, resume" in refused["reason"]
    assert (await mcp_server.jobs_gaps_progress(door, id="nope", read=1))["ok"] is False
    assert (await mcp_server.jobs_gaps_report(door, id="nope", themes=[]))["ok"] is False
    assert (await mcp_server.jobs_gaps_report(door, id=str(uuid.uuid4()), draft="A line."))["ok"] is False


async def test_an_edit_to_the_instructions_is_used_by_the_next_run_and_the_run_says_which_version(client):
    await _stock(10)
    first = await _finished()
    assert (await _view(first))["instructions_version"] == 1
    async with db.session_factory()() as session:
        text = await instruction_files.read(session, OWNER, "instructions", "jobsearch.gaps")
        await instruction_files.save(session, OWNER, "instructions", "jobsearch.gaps", text + "\nAlso: be brief.",
                                     "mine")
        await session.commit()
    second = (await client.post(f"/api/v1/find-gaps/runs/{first}/new-sample")).json()["id"]
    task = await _take()
    assert task["instructions"].endswith("Also: be brief.")
    await _report(second, [_theme("Programs at scale", _refs(10))])
    assert (await _view(second))["instructions_version"] == 2


def test_the_process_and_its_instruction_file_and_template_are_the_analysts_with_no_way_to_sign_in_or_apply():
    library = process.build_library()
    steps = {e.id: e for e in library.get(process.FIND_GAPS).elements.values()}
    finder = steps[process.FINDER_STEP]
    assert (finder.role, finder.instructions, finder.template) == ("analyst", "jobsearch.gaps", "jobsearch.gaps.report")
    assert {steps[s].action for s in ("Gather_sample", "Settle_run")} == {"jobs.gaps_gather", "jobs.gaps_settle"}
    text = harness_instructions.read("instructions", "jobsearch.gaps").lower()
    assert "never apply, never fill a form, never solve a captcha and never get around a block" in text
    for tool in ("jobs.gaps", "jobs.gaps_progress", "jobs.gaps_report"):
        assert tool in text
    assert "websearch" in text and "webfetch" in text and "read-only" in text
    for other in ("jobs.profile", "jobs.report`", "jobs.analysis", "jobs.packet"):
        assert other not in text, f"a Find Gaps session has no {other}"
    assert "add no claim, number, scope" in text, "the drafted line holds only what the owner said"
    assert "jobsearch.gaps.report" in harness_instructions.names("templates")


async def test_the_runner_is_told_gaps_work_waits_and_the_agents_page_says_what_it_is_at(client):
    from app.agents import roster

    await _stock(10)
    run_id = await _start()
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) == "gaps"
    await _take()
    await parse_runner(seen=MINUTE, checked_in=MINUTE, kind="gaps")
    async with db.session_factory()() as session:
        conn = await roster.connection_by_slug(session, OWNER, roster.SCOUT)
        assert (await roster.work_of(session, OWNER, conn)).title == "Finding gaps for Builders"
    agent = (await client.get("/api/v1/agents")).json()[0]
    assert agent["status"] == "working" and agent["current"]["task_title"] == "Finding gaps for Builders"
    assert (await _row(run_id)).state == "working"
