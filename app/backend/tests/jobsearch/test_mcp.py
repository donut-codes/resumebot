"""The scout's door: three tools, one bearer token, every reply whole, refusals as ordinary
results. The tools are called the way the server calls them (a context with request headers)."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.doors import scout as mcp_server
from app.jobsearch.missions import search_run as run
from app.jobsearch.models import Opportunity, SearchRun
from app.jobsearch.profile import schema as p
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile import views as pv
from app.planengine.budget import AGENT_PAYLOAD_BUDGET_CHARS, REPLY_CEILING_CHARS, payload_chars
from tests.support import GRADED_ON, PROFILE, TOKEN, load_profile, reported

OWNER = "owner"  # the setting's default: the tools act for it


async def _profile_and_search(**parts):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, **parts)
        await session.commit()


async def _start():
    async with db.session_factory()() as session:
        started = await run.start_round(session, OWNER, focus="banks")
        await session.commit()
        return started.id


# --- the contract ------------------------------------------------------------------------------------------------


SITE_TOOLS = {"work.next", "task.next", "task.complete", "task.escalate", "plan.propose",
              "plan.revise", "item.lookup", "review.read", "lesson.list", "lesson.record", "lesson.retire",
              "act.complete", "skill.try", "doc.read"}


async def _listed_for(headers) -> set[str]:
    """What a session is listed over the wire: the server's `tools/list` handler, given the request's headers."""
    ctx = SimpleNamespace(request=SimpleNamespace(headers=headers))
    return {t.name for t in (await mcp_server.mcp._handle_list_tools(ctx, None)).tools}


async def test_a_session_is_listed_only_the_tools_its_kind_names_and_one_that_names_none_is_listed_all():
    every = {t.name for t in await mcp_server.mcp.list_tools()}
    assert await _listed_for({"x-tools": "jobs.analysis,jobs.analysis_submit"}) == {
        "jobs.analysis", "jobs.analysis_submit"}
    # Claude Code's own spelling of a name (every character outside letters, digits, _ and - is _) is read as the tool's
    assert await _listed_for({"X-Tools": "jobs_analysis, jobs_analysis_submit ,jobs_profile"}) == {
        "jobs.analysis", "jobs.analysis_submit", "jobs.profile"}
    assert await _listed_for({"x-tools": "jobs.nothing_of_the_kind"}) == set()
    assert await _listed_for({}) == every and await _listed_for(None) == every
    assert await _listed_for({"x-tools": " , "}) == every, "an empty list names none"
    assert await _listed_for({"x-tools": "jobs.analysis"}) < every
    assert await mcp_server.mcp._handle_list_tools(SimpleNamespace(), None) is not None, "no request: all of them"


async def test_the_server_lists_exactly_the_job_search_tools_and_the_site_tools_with_their_arguments():
    listed = {t.name: t for t in await mcp_server.mcp.list_tools()}
    assert set(listed) == {"jobs.profile", "jobs.titles_next", "jobs.titles_report",
                           "jobs.report", "jobs.analysis", "jobs.analysis_submit", "jobs.coach", "jobs.coach_submit",
                           "jobs.boards", "jobs.board_found",
                           "jobs.parse_next", "jobs.parse_report", "jobs.match_next", "jobs.match_report",
                           "jobs.gaps", "jobs.gaps_progress", "jobs.gaps_report", "jobs.employer",
                           "jobs.employer_report", "jobs.mail", "jobs.mail_report"} | SITE_TOOLS
    assert not [n for n in listed if "check" in n], "no tool writes a check result"
    assert set(listed["task.next"].input_schema["properties"]) == {"project_id"}
    assert set(listed["task.complete"].input_schema["properties"]) == {
        "task_id", "outcome", "summary", "evidence", "off_plan_reason", "result", "tried", "next", "recipe"}
    assert set(listed["task.escalate"].input_schema["properties"]) == {
        "task_id", "question", "checked", "reason", "options", "recommended"}
    assert set(listed["act.complete"].input_schema["properties"]) == {
        "project_id", "task_id", "summary", "resolution", "skill"}
    assert set(listed["skill.try"].input_schema["properties"]) == {"task_id", "recipe"}
    assert set(listed["lesson.record"].input_schema["properties"]) == {
        "project_id", "task_id", "text", "applies_to", "source", "subject"}
    assert set(listed["work.next"].input_schema.get("properties") or {}) == set()
    assert set(listed["jobs.board_found"].input_schema["properties"]) == {"company", "url", "not_found"}
    assert set(listed["jobs.titles_next"].input_schema.get("properties") or {}) == set()
    assert set(listed["jobs.titles_report"].input_schema["properties"]) == {"answers"}
    assert listed["jobs.titles_report"].input_schema["required"] == ["answers"]
    assert set(listed["jobs.parse_next"].input_schema.get("properties") or {}) == set()
    assert set(listed["jobs.parse_report"].input_schema["properties"]) == {"id", "fields"}
    assert listed["jobs.parse_report"].input_schema["required"] == ["id", "fields"]
    assert set(listed["jobs.match_next"].input_schema.get("properties") or {}) == set()
    assert set(listed["jobs.match_report"].input_schema["properties"]) == {"id", "answers"}
    assert listed["jobs.match_report"].input_schema["required"] == ["id", "answers"]
    assert not listed["jobs.employer"].input_schema.get("properties"), "it takes the next employer waiting: no argument"
    assert set(listed["jobs.employer_report"].input_schema["properties"]) == {"report"}
    assert listed["jobs.employer_report"].input_schema["required"] == ["report"]
    assert set(listed["jobs.gaps"].input_schema["properties"]) == {"part", "page"}
    assert set(listed["jobs.gaps_progress"].input_schema["properties"]) == {"id", "read"}
    assert listed["jobs.gaps_progress"].input_schema["required"] == ["id", "read"]
    assert set(listed["jobs.gaps_report"].input_schema["properties"]) == {"id", "themes", "postings", "draft"}
    assert listed["jobs.gaps_report"].input_schema["required"] == ["id"]
    assert set(listed["jobs.profile"].input_schema.get("properties") or {}) == set()
    assert set(listed["jobs.report"].input_schema["properties"]) == {"roles", "done"}
    assert listed["jobs.report"].input_schema["required"] == ["roles"]
    for tool in listed.values():
        assert tool.description and len(tool.description) > 60


@pytest.mark.parametrize("header", [None, "", "Bearer", "Bearer wrong-token", "Basic abc", f"bearer {TOKEN}x"])
async def test_a_call_without_the_scouts_token_is_refused_and_reads_nothing(js, door, header):
    ctx = SimpleNamespace(headers={"authorization": header} if header is not None else {})
    for call in (mcp_server.jobs_profile(ctx), mcp_server.jobs_report(ctx, [], True)):
        result = await call
        assert result["ok"] is False and "token" in result["reason"]


async def test_no_token_file_means_nothing_gets_in(js, door, monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_server.settings, "agent_token_file", str(tmp_path / "missing"))
    assert (await mcp_server.jobs_profile(door))["ok"] is False


# --- jobs.profile ------------------------------------------------------------------------------------------------


async def test_profile_with_no_search_open_says_to_stop_and_still_gives_the_profile(js, door):
    await _profile_and_search()
    reply = await mcp_server.jobs_profile(door)
    assert reply["ok"] is True and reply["task"] is None and "Stop here" in reply["note"]
    profile = reply["profile"]
    looks = profile["what_to_look_for"]
    assert list(looks) == ["company", "target_roles"], "the company and each target role"
    [role] = looks["target_roles"]
    assert list(role) == ["id", "name", "titles", "description", "responsibilities", "qualifications", "preferred",
                          "compensation"], "every part of the role, in the order a posting reads"
    assert role["description"].startswith("Builds and runs programs") and role["titles"] == ["Director of Programs"]
    assert role["responsibilities"].startswith("Plans the year")
    assert not {"the_work", "titles", "job_titles"} & set(profile)
    assert not {"search", "positioning", "keywords_for_scoring", "employers_to_check"} & set(profile), (
        "where to look and how the owner presents themselves are not the reader's to judge by")
    gates = {g["id"]: g for g in profile["hard_gates"]}
    assert gates["compensation"]["says"] == "Pay must be at least $95,000 a year, on the estimated ask."
    assert "report the role anyway" in gates["compensation"]["who_checks"]
    assert gates["travel"]["who_checks"].startswith("you: answer pass, fail or unknown")
    asks = {a["id"]: a for s in profile["scoring"]["sections"] for a in s["asks"]}
    assert "leave it out" in asks["aging"]["who_checks"] and "who_checks" not in asks["promotion"]


async def test_profile_with_a_search_open_hands_over_the_task_and_the_response_shape(js, door):
    await _profile_and_search()
    run_id = await _start()
    reply = await mcp_server.jobs_profile(door)
    task = reply["task"]
    assert task["run_id"] == str(run_id) and task["focus"] == "banks" and task["reported_so_far"] == 0
    assert "jobs.report" in task["report_with"]
    assert task["step"] == "The scout finds job boards"
    assert "roles:" in task["response_template"] and "done: true" in task["response_template"]
    assert reply["note"] is None


async def test_profile_reply_is_the_owners_whole_text_however_long(js, door):
    """The profile is the owner's own text, not a batch of items: a long one is sent whole, loose notes too,
    and nothing says it was cut."""
    notes = [{"id": f"n{n}", "text": "an old note " * 200} for n in range(30)]
    await _profile_and_search(criteria={**PROFILE["criteria"], "notes": notes})
    await _start()
    reply = await mcp_server.jobs_profile(door)
    assert AGENT_PAYLOAD_BUDGET_CHARS < payload_chars(reply) < REPLY_CEILING_CHARS
    placed = reply["profile"]["not_placed_yet"]
    assert "cut_to_fit" not in reply["profile"] and sum(n.startswith("an old note") for n in placed) == 30
    looks = reply["profile"]["what_to_look_for"]
    role = looks["target_roles"][0]
    assert role["description"] and role["titles"] and role["qualifications"], "what the owner looks for is never cut"


async def test_profile_before_it_is_loaded_is_a_plain_refusal(js, door):
    reply = await mcp_server.jobs_profile(door)
    assert reply["ok"] is False and "what to look for" in reply["reason"]


# --- jobs.report -------------------------------------------------------------------------------------------------


async def test_report_stages_batches_then_done_runs_the_round_and_tells_the_scout_what_came_of_it(js, door):
    await _profile_and_search()
    run_id = await _start()
    first = await mcp_server.jobs_report(door, [reported("Fabrikam", "Lead", fit=5)])
    assert first == {"ok": True, "staged": 1, "total": 1, "done": False}
    last = await mcp_server.jobs_report(door, [reported("Globex", "VP", fit=5)], done=True)
    assert last["ok"] is True and last["done"] is True and last["total"] == 2
    assert last["counts"] == {"reported": 2, "kept": 1, "screened_out": 1, "duplicates": 0}
    assert last["summary"].startswith("Reviewed 2 roles: 1 kept, 1 gated out")
    async with db.session_factory()() as session:
        assert (await session.get(SearchRun, run_id)).status == "done"
        assert len((await session.scalars(select(Opportunity))).all()) == 2
    again = await mcp_server.jobs_report(door, [reported("X", "Y")])
    assert again["ok"] is False and "no search is open" in again["reason"]


async def test_report_refuses_a_bad_batch_whole_with_every_problem_and_how_to_fix_it(js, door):
    await _profile_and_search()
    await _start()
    reply = await mcp_server.jobs_report(door, [{"company": "A", "title": "B"}, reported("Fine", "Role")])
    assert reply["ok"] is False and reply["reason"].startswith("nothing was saved") and "role 1" in reply["reason"]
    assert reply["how_to_fix"]
    too_big = await mcp_server.jobs_report(door, [reported(f"C{i}", "T") for i in range(run.MAX_BATCH + 1)])
    assert "at most" in too_big["reason"]


async def test_free_text_the_scout_sends_is_capped_not_stored_whole(js, door):
    await _profile_and_search()
    await _start()
    long = "x" * 5000
    reply = await mcp_server.jobs_report(door, [reported("Fabrikam", "Lead", fit_evidence=long)])
    assert reply["ok"] is False and "too long" in reply["reason"]


# --- a tool is one function under @door.tool; what an agent is shown is that function's own arguments ----------


async def test_a_tool_shows_the_agent_its_arguments_and_nothing_of_the_door():
    listed = {t.name: t for t in await mcp_server.mcp.list_tools()}
    schema = listed["jobs.board_found"].input_schema
    assert set(schema["properties"]) == {"company", "url", "not_found"}, "no session, owner or ctx"
    assert schema["required"] == ["company"], "the defaults are the function's own"
    assert schema["properties"]["url"]["default"] is None
    assert listed["jobs.board_found"].description.startswith("Save the job board you found")
    assert listed["task.complete"].input_schema["properties"]["next"]["default"] is True
    # Every tool the scout is told about is there, once, under the name its instructions use.
    assert len(listed) == 35 and {"jobs.profile", "jobs.report", "task.next", "doc.read"} <= set(listed)


# --- what an agent is sent of the rows that hold several statements (docs/PRD.md 30.9) -------------------------------


def _statement(ident: str, says: str, points: int = 0, **more) -> dict:
    return {"id": ident, "says": says, "points": points, **more}


#: A first-match row of three statements, best first, in Responsibilities; an "any of" row of two in Company, worth 2.
FIRST = {"id": "row-a", "kind": "best", "section": "responsibilities", "members": [
    _statement("stmt-a", "The work is mostly building", 4), _statement("stmt-b", "The work is partly building", 3),
    _statement("stmt-c", "The work is a little building", 2)]}
ANY = {"id": "box-a", "kind": "any", "section": "company", "points": 2, "members": [
    _statement("stmt-d", "The employer is a public agency"), _statement("stmt-e", "The employer is a nonprofit")]}
#: The app's own tests: an employer fact, and the analyst's classes of the required items.
SMALL = {"fact": "employees", "op": "<", "value": 1000, "checked_by": "app"}
GAPS = {"from_classes": "U", "checked_by": "app"}
LEAVE_OUT = "the app: leave it out"


def _sent(*groups: dict, asks=(), **more) -> dict:
    """The rows an agent is sent, by id, for a scoring with these asks and groups."""
    prof = p.Profile.model_validate({"scoring": {"asks": list(asks), "groups": list(groups), **more}})
    return {row["id"]: row for section in pv.scoring_for_agent(prof.scoring, prof.criteria) for row in section["asks"]}


def _keys(value) -> set[str]:
    """Every key in a value, all the way down."""
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


def test_the_statements_of_a_first_match_row_are_asks_each_with_the_row_they_are_part_of_and_no_points():
    rows = _sent(FIRST)
    assert "row-a" not in rows, "the row is not a question: the app takes the first statement met from the top"
    for ident, says in (("stmt-a", "The work is mostly building"), ("stmt-b", "The work is partly building"),
                        ("stmt-c", "The work is a little building")):
        got = rows[ident]
        assert got["says"] == says and got["part_of"] == "row-a" and got["effect"] == "adds points"
        assert "answer every statement of the row" in got["first_match"]
        assert "statements" not in got and "who_checks" not in got and "only_for_roles" not in got
        assert not {"points", "reach", "weight"} & _keys(got), "an agent is never sent a point value"


def test_the_statements_of_an_any_of_row_are_asks_each_with_the_row_they_are_part_of_and_no_points():
    rows = _sent(ANY)
    assert "box-a" not in rows, "the row is not a question: the app works it out"
    for ident, says in (("stmt-d", "The employer is a public agency"), ("stmt-e", "The employer is a nonprofit")):
        got = rows[ident]
        assert got["says"] == says and got["part_of"] == "box-a" and got["effect"] == "adds points"
        assert got["any_of"] == "the row earns once when any one of its statements is true"
        assert "statements" not in got and "who_checks" not in got
        assert not {"points", "reach", "weight"} & _keys(got)


def test_a_row_says_which_way_it_pulls_without_a_number():
    ladder = {**FIRST, "id": "ladder", "members": [
        _statement("up", "A promotion", 1), _statement("level", "A lateral move", 0),
        _statement("down", "A step down", -1), _statement("lower", "Two steps down", -2)]}
    costly = {**FIRST, "id": "costs",
              "members": [_statement("x", "Heavy travel", -2), _statement("y", "Some travel", -1)]}
    penalty = {**ANY, "id": "avoid", "points": -3, "members": [_statement("p", "Heavy travel"),
                                                                _statement("q", "Long hours")]}
    rows = _sent(ladder, costly, penalty)
    assert [rows[i]["effect"] for i in ("up", "level", "down", "lower")] == [
        "adds points", "noted only", "noted only", "noted only"]  # edited 2026-10-27 (PRD 30.10): none takes off
    assert rows["x"]["effect"] == "noted only" and rows["p"]["effect"] == "noted only"


def test_a_row_on_a_roles_tab_names_the_role_and_one_for_every_role_names_none():
    rows = _sent({**FIRST, "roles": ["builders"]}, {**ANY, "roles": ["builders"]},
                 {**ANY, "id": "box-b", "members": [_statement("stmt-x", "Elsewhere"), _statement("stmt-y", "Or so")]})
    assert rows["stmt-a"]["only_for_roles"] == ["builders"] and rows["stmt-d"]["only_for_roles"] == ["builders"]
    assert "only_for_roles" not in rows["stmt-x"]


def test_a_row_the_app_decides_is_marked_leave_it_out_and_so_is_an_ask_built_from_facts_or_classes():
    by_fact = {**FIRST, "id": "size", "members": [
        _statement("small", "Fewer than 200 people", 2, **{**SMALL, "value": 200}),
        _statement("medium", "Fewer than 1,000 people", 1, **SMALL)]}
    rows = _sent(by_fact, asks=[
        {"id": "gaps", "section": "qualifications", "says": "Each unproven item", "points": -1, "per_item": True,
         "limit": -3, **GAPS},
        {"id": "reader-fact", "section": "company", "says": "Few people", "points": 1, "fact": "employees", "op": "<",
         "value": 100},  # left to the reader by the owner, but built from an employer fact
        {"id": "plain", "section": "posting", "says": "A promotion", "points": 1}])
    assert [rows[i].get("who_checks") for i in ("small", "medium", "gaps", "reader-fact", "plain")] == [
        LEAVE_OUT, LEAVE_OUT, LEAVE_OUT, LEAVE_OUT, None]


def test_a_statement_of_an_any_of_the_app_decides_is_marked_and_the_others_are_not():
    mixed = {**ANY, "id": "mix", "members": [
        _statement("stmt-a", "The employer is a public agency"), _statement("stmt-b", "Few people", **SMALL),
        _statement("stmt-c", "Many gaps", **GAPS)]}
    rows = _sent(mixed)
    assert [rows[i].get("who_checks") for i in ("stmt-a", "stmt-b", "stmt-c")] == [None, LEAVE_OUT, LEAVE_OUT]
    assert {rows[i]["part_of"] for i in rows} == {"mix"}


def test_a_row_in_a_part_that_is_off_is_not_sent_and_the_graded_requirements_row_is_never_sent():
    rows = _sent(FIRST, ANY, sections_off=["company"])
    assert set(rows) == {"stmt-a", "stmt-b", "stmt-c"}
    graded = _sent(FIRST, ANY, grade_requirements=GRADED_ON)
    assert set(graded) == {"stmt-a", "stmt-b", "stmt-c", "stmt-d", "stmt-e"}, (
        "the app's own row over the classes is no agent's question"
    )


async def test_the_scout_is_sent_the_rows_of_the_profile_with_no_point_in_them(js, door):
    scoring_doc = {"asks": [{"id": "promotion", "section": "posting", "says": "A promotion", "points": 1}],
                   "groups": [FIRST, ANY]}
    await _profile_and_search(scoring=scoring_doc)
    reply = await mcp_server.jobs_profile(door)
    sections = reply["profile"]["scoring"]["sections"]
    rows = {row["id"]: row for section in sections for row in section["asks"]}
    assert {"promotion", "stmt-a", "stmt-d", "stmt-e"} <= set(rows)
    assert rows["stmt-a"]["part_of"] == "row-a" and rows["stmt-d"]["part_of"] == "box-a"
    assert "points" not in _keys(reply["profile"]["scoring"])
    async with db.session_factory()() as session:
        prof = await profile_store.current(session, OWNER)
    assert sections == pv.scoring_for_agent(prof.scoring, prof.criteria), "the one function that builds the rows"
