"""The master's structured record (the structured data plan spec step 3): built once for each saved master version
by one parser-kind session, years worked out in code from the roles' dates, every named term tied to master line ids,
and the master itself never changed. Synthetic data only."""

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select

from app import db
from app.jobsearch.doors import scout as mcp_server
from app.jobsearch.jobs import parse as role_parse
from app.jobsearch.models import ParseJob, ProfileVersion, StructuredRecord
from app.jobsearch.postings import record as sr
from app.jobsearch.profile import documents
from app.jobsearch.profile import store as profile_store
from app.jobsearch.resume import structured_profile as sp
from app.jobsearch.work import housekeeping
from app.settings import settings
from tests.builders import MINUTE, parse_runner
from tests.support import OWNER, TOKEN

TODAY = date(2026, 10, 20)


def _resume(**more) -> dict:
    base = {
        "roles": [
            {"id": "r1", "employer": "Northwind Labs", "title": "Director of Programs", "start": "Jan 2019",
             "end": "Present", "tools": ["Gantt tools"],
             "bullets": [{"id": "b1", "text": "Led the risk working group across four departments."},
                         {"id": "b2", "text": "Built the knowledge base the whole practice uses."}]},
            {"id": "r2", "employer": "Example Works", "title": "Programs Manager", "start": "2015", "end": "2020",
             "bullets": [{"id": "b3", "text": "Ran the quarterly review cycle for the whole office."}]},
            {"id": "r3", "employer": "Sample Co", "title": "Analyst", "start": "sometime", "end": "",
             "bullets": [{"id": "b4", "text": "Wrote the weekly summary for leadership."}]},
        ],
        "education": [{"id": "e1", "degree": "MS in Leadership", "school": "Example University"}],
        "achievements": [{"id": "a1", "text": "Opened a second office in a year."}],
        "competencies": [{"id": "c1", "area": "Governance", "items": ["Risk frameworks", "Policy design"]}],
    }
    return base | more


def _doc(**more) -> documents.Resume:
    return documents.Resume.model_validate(_resume(**more))


REPORT = {
    "entries": [
        {"category": "areas", "name": "Program management", "lines": ["b1", "b3"]},
        {"category": "methods", "name": "Risk frameworks", "lines": ["b1", "c1"]},
        {"category": "tools", "name": "Gantt tools", "lines": ["r1"]},
        {"category": "skills", "name": "Weekly reporting", "lines": ["b4"]},
        {"category": "methods", "name": "risk frameworks", "lines": ["b2"]},   # a repeat of a name: merged
        {"category": "domains", "name": "Governance", "lines": ["a1"]},
    ],
    "certifications": [{"name": "Example Certified Manager", "lines": ["a1"]}],
    "highlights": [{"line": "b1", "terms": ["Risk frameworks", "risk frameworks", "Governance"]}],
}


# --- years, in code, from the dates ----------


def test_years_are_worked_out_from_the_roles_dates_with_overlapping_months_counted_once():
    resume = _doc()
    first, second = (sp.role_months(r, TODAY) for r in resume.roles[:2])
    assert len(first) == 94                  # Jan 2019 through Oct 2026
    assert len(second) == 72                 # a bare year: its first month for the start, its last for the end
    both = len(first | second)               # Jan 2015 through Oct 2026
    assert both == 142 and sp.years(first | second) == 11.8
    assert sp.years_of_lines(resume, ["b1", "b3"], TODAY) == 11.8
    assert sp.years_of_lines(resume, ["b3"], TODAY) == 6.0


def test_a_date_code_cannot_read_leaves_the_role_with_no_months_never_a_guess():
    resume = _doc()
    assert sp.role_months(resume.roles[2], TODAY) == frozenset()
    assert sp.years_of_lines(resume, ["b4"], TODAY) is None
    assert sp.years_of_lines(resume, ["a1", "e1"], TODAY) is None     # lines outside any role add none


@pytest.mark.parametrize("start, end, months", [
    ("03/2020", "05/2020", 3), ("2020-03", "2020-03", 1), ("March 2020", "April 2020", 2), ("2021", "2021", 12)])
def test_the_ways_a_resume_writes_a_month_are_read(start, end, months):
    role = documents.Role(id="r", employer="E", title="T", start=start, end=end)
    assert len(sp.role_months(role, TODAY)) == months


def test_the_record_carries_years_computed_in_code_and_never_the_models():
    record = sp.build(_doc(), sp.check(REPORT, _doc())[0], TODAY)
    stack = {e["name"]: e for e in record.fields["stack"].value}
    assert stack["Risk frameworks"]["years"] == 7.8 and stack["Risk frameworks"]["lines"] == ["b1", "c1", "b2"]
    assert "years" not in stack["Weekly reporting"] and stack["Weekly reporting"]["category"] == "skills"
    assert record.fields["years"].value["total"] == 11.8 and record.fields["years"].source == "code"
    assert record.fields["areas"].value[0]["years"] == 11.8
    assert record.fields["title"].value == ["Director of Programs", "Programs Manager", "Analyst"]
    assert record.fields["education"].value[0]["line"] == "e1"


def test_every_filled_field_rests_on_a_quote_that_is_in_the_master():
    resume = _doc()
    record = sp.build(resume, sp.check(REPORT, resume)[0], TODAY)
    assert sr.check_quotes(record, "\n".join(sp.line_texts(resume).values())) == []
    assert all(f.quote for f in record.fields.values())
    assert sr.Record.from_dict(record.as_dict()) == record


# --- a name with no line id is refused ----------


@pytest.mark.parametrize("bad, said", [
    ({"entries": [{"category": "skills", "name": "Planning"}]}, "no line id"),
    ({"entries": [{"category": "skills", "name": "Planning", "lines": []}]}, "no line id"),
    ({"entries": [{"category": "skills", "name": "Planning", "lines": ["nope"]}]}, "not a line of the master"),
    ({"certifications": [{"name": "Example Certified Manager"}]}, "no line id"),
    ({"highlights": [{"line": "nope", "terms": ["x"]}]}, "no line id"),
    ({"entries": [{"category": "feelings", "name": "Planning", "lines": ["b1"]}]}, "category must be one of"),
])
def test_a_name_with_no_line_id_or_a_line_that_is_not_the_masters_is_refused_and_left_out(bad, said):
    clean, problems = sp.check(bad, _doc())
    assert problems and said in " ".join(problems)
    assert sp.is_empty(clean)


def test_the_good_names_of_a_report_are_tidied_merged_and_kept():
    clean, problems = sp.check(REPORT, _doc())
    assert problems == []
    names = [(e["category"], e["name"], e["lines"]) for e in clean["entries"]]
    assert ("methods", "Risk frameworks", ["b1", "c1", "b2"]) in names
    assert ("skills", "Weekly reporting", ["b4"]) in names
    assert clean["highlights"] == [{"line": "b1", "terms": ["Risk frameworks", "Governance"]}]


# --- the job: one record for each saved master version, the master untouched ----------


@pytest.fixture
async def agent(js, monkeypatch, scout_token):
    monkeypatch.setattr(settings, "owner", OWNER)
    await parse_runner(seen=MINUTE)


@pytest.fixture
def door():
    return SimpleNamespace(headers={"authorization": f"Bearer {TOKEN}"})


async def _save(data: dict) -> int | None:
    async with db.session_factory()() as session:
        version = await profile_store.save(session, OWNER, "resume", data, by="test")
        await session.commit()
        return version


async def _jobs() -> list[ParseJob]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(ParseJob).where(ParseJob.kind == "profile"))).all())


async def _records() -> list[StructuredRecord]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(StructuredRecord).where(StructuredRecord.kind == "profile"))).all())


async def test_saving_a_master_version_asks_for_one_profile_job_for_that_version(agent):
    assert await _save(_resume()) == 1
    (job,) = await _jobs()
    assert (job.state, job.role_id) == ("waiting", "master:1")
    assert "[b1] Led the risk working group" in job.text and "Jan 2019 - Present" in job.text
    async with db.session_factory()() as session:
        assert await sp.ensure(session, OWNER) is None, "a job for this version is already waiting"
    assert len(await _jobs()) == 1


async def test_a_save_with_no_agent_there_now_still_leaves_a_waiting_job_for_the_pool(js, monkeypatch):
    monkeypatch.setattr(settings, "owner", OWNER)
    await parse_runner(seen=3 * 60 * MINUTE)
    assert await _save(_resume()) == 1
    (job,) = await _jobs()
    assert (job.state, job.role_id) == ("waiting", "master:1")


async def test_the_waiting_job_is_not_failed_for_waiting_and_a_free_session_takes_it(
        js, monkeypatch, scout_token, door):
    monkeypatch.setattr(settings, "owner", OWNER)
    await parse_runner(seen=3 * 60 * MINUTE)
    await _save(_resume())
    async with db.session_factory()() as session:
        await role_parse.expire(session, OWNER, datetime.now(UTC) + timedelta(hours=3))
        await session.commit()
    assert [j.state for j in await _jobs()] == ["waiting"], "no 5 minute limit: nobody waits at the screen"
    await parse_runner(seen=MINUTE)         # an agent is free now
    taken = await mcp_server.jobs_parse_next(door)
    assert taken["kind"] == "profile"


async def test_a_job_that_was_lost_is_made_again_once_when_the_pool_looks_and_not_twice(agent):
    await _save(_resume())
    async with db.session_factory()() as session:
        await session.execute(delete(ParseJob))      # as after a restart that lost it, or a day's clean-up
        await session.commit()
    async with db.session_factory()() as session:
        await housekeeping.settle(session, OWNER)  # the app's own pass makes it again
        assert await role_parse.waiting(session, OWNER) is not None
        await housekeeping.settle(session, OWNER)
        assert await role_parse.waiting(session, OWNER) is not None
        await session.commit()
    assert len(await _jobs()) == 1


async def test_a_job_that_keeps_failing_is_made_again_only_a_few_times(agent):
    await _save(_resume())
    for _ in range(sp.MOST_TRIES + 2):
        async with db.session_factory()() as session:
            await housekeeping.settle(session, OWNER)
            job = await role_parse.waiting(session, OWNER)
            if job is not None:
                job.state = "failed"
            await session.commit()
    assert len(await _jobs()) == sp.MOST_TRIES


async def test_the_owners_own_posting_is_handed_out_before_the_masters_job(agent):
    await _save(_resume())
    async with db.session_factory()() as session:
        mine = await role_parse.start(session, OWNER, "x" * 60)
        await session.commit()
        assert (await role_parse.waiting(session, OWNER)).id == mine.id


async def test_the_session_reads_the_master_once_and_the_master_is_unchanged(agent, door):
    await _save(_resume())
    async with db.session_factory()() as session:
        before = (await session.scalars(select(ProfileVersion.data).where(ProfileVersion.kind == "resume"))).all()
    taken = await mcp_server.jobs_parse_next(door)
    assert taken["kind"] == "profile" and taken["how"] == sp.HOW
    for key in ("entries", "certifications", "highlights"):
        assert f"{key}:" in taken["response_template"]
    reported = await mcp_server.jobs_parse_report(door, taken["id"], REPORT)
    assert reported["ok"] is True and reported["state"] == "done"
    async with db.session_factory()() as session:
        after = (await session.scalars(select(ProfileVersion.data).where(ProfileVersion.kind == "resume"))).all()
        assert after == before, "the master is read, never written"
        assert await session.scalar(select(func.count()).select_from(ProfileVersion).where(
            ProfileVersion.kind == "resume")) == 1
    (row,) = await _records()
    assert (row.fingerprint, row.role_id, row.partial) == ("1", None, False)
    assert {"title", "years", "education", "areas", "stack", "certifications", "duties"} == set(row.fields)
    assert (await _jobs())[0].text == "", "the master's text is not kept once it has been read"


async def test_there_is_one_record_for_each_master_version(agent, door):
    await _save(_resume())
    first = await mcp_server.jobs_parse_next(door)
    await mcp_server.jobs_parse_report(door, first["id"], REPORT)
    async with db.session_factory()() as session:
        assert await sp.ensure(session, OWNER) is None, "version 1 has its record: nothing more is asked"
        record = sp.build(_doc(), sp.check(REPORT, _doc())[0], TODAY)
        await sp.keep(session, OWNER, 1, record)       # keeping it again replaces it, never adds a second
        await session.commit()
    assert [r.fingerprint for r in await _records()] == ["1"] and len(await _jobs()) == 1
    assert await _save(_resume(achievements=[{"id": "a1", "text": "Opened a third office."}])) == 2
    assert len(await _jobs()) == 2
    second = await mcp_server.jobs_parse_next(door)
    if second["kind"] == "claims":      # the claims job of version 1 (cleansed_master.py) is older and goes first
        await mcp_server.jobs_parse_report(door, second["id"], {"claims": []})
        second = await mcp_server.jobs_parse_next(door)
    await mcp_server.jobs_parse_report(door, second["id"], REPORT)
    assert sorted(r.fingerprint for r in await _records()) == ["1", "2"]


async def test_a_report_with_a_name_that_has_no_line_id_is_refused_and_keeps_no_record(agent, door):
    await _save(_resume())
    taken = await mcp_server.jobs_parse_next(door)
    bad = {"entries": [{"category": "skills", "name": "Planning"}]}
    async with db.session_factory()() as session:
        with pytest.raises(role_parse.ParseError, match="no line id"):
            await role_parse.report(session, OWNER, taken["id"], bad)
    assert await _records() == [] and (await _jobs())[0].state == "reading"
