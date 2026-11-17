"""The cleansed master: from each saved master version, concise atomic claims, each citing the master lines it rests on,
with no years, examples in the master's own words, nothing marked never in writing, and the master itself never
changed. The Haiku ranker's short profile and the Second Impression read the claims. Synthetic data only."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.doors import scout as mcp_server
from app.jobsearch.jobs import parse as role_parse
from app.jobsearch.models import ParseJob, ProfileVersion, StructuredRecord
from app.jobsearch.profile import documents, schema
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import rank
from app.jobsearch.resume import cleansed_master as cm
from app.settings import settings
from tests.builders import MINUTE, parse_runner
from tests.support import OWNER, PROFILE, TOKEN, load_profile


def _resume(**more) -> dict:
    base = {
        "roles": [
            {"id": "r1", "employer": "Northwind Labs", "title": "Director of Programs", "start": "Jan 2019",
             "end": "Present",
             "bullets": [{"id": "b1", "text": "Led the risk working group across four departments."},
                         {"id": "b2", "text": "Ran the weekly status review for a team of twelve."}]},
            {"id": "r2", "employer": "Example Works", "title": "Programs Manager", "start": "2015", "end": "2020",
             "bullets": [{"id": "b3", "text": "Ran the quarterly review cycle for the whole office."},
                         {"id": "b4", "text": "Cut the reporting backlog in half.", "note": "never in writing"}]},
            {"id": "r3", "employer": "Sample Co", "title": "Analyst", "start": "sometime", "end": "",
             "bullets": [{"id": "b5", "text": "Wrote the weekly summary for leadership."}]},
        ],
        "education": [{"id": "e1", "degree": "MS in Leadership", "school": "Example University"}],
        "achievements": [{"id": "a1", "text": "Earned the Example Certified Manager credential."}],
    }
    return base | more


def _doc(**more) -> documents.Resume:
    return documents.Resume.model_validate(_resume(**more))


REPORT = {"claims": [
    {"claim": "program management", "examples": ["risk working group", "quarterly review cycle"],
     "lines": ["b1", "b3"]},
    {"claim": "team leadership", "examples": ["team of twelve"], "lines": ["b2"]},
    {"claim": "weekly reporting", "examples": ["weekly summary for leadership"], "lines": ["b5"]},
    {"claim": "Example Certified Manager credential", "examples": [], "lines": ["a1"]},
]}


# --- the check: every cited id exists, no years, the master's own words ----------


def test_a_clean_report_is_kept_as_it_is_with_no_years_anywhere():
    clean, problems = cm.check(REPORT, _doc())
    assert problems == [] and len(clean) == 4
    assert all(set(c) == {"claim", "examples", "lines"} for c in clean), "no kind, no years"


@pytest.mark.parametrize("field, value", [("claim", "program management for 7 years"),
                                           ("examples", ["over 10 years in the field"])])
def test_a_number_of_years_is_refused(field, value):
    item = {"claim": "program management", "examples": [], "lines": ["b1"], field: value}
    _, problems = cm.check({"claims": [item]}, _doc())
    assert any("number of years" in p for p in problems)


def test_a_result_that_carries_a_time_span_is_allowed_as_an_example():
    resume = _doc(achievements=[{"id": "a2", "text": "Cut ramp-up from 6 months to 60 days."}])
    item = {"claim": "onboarding", "examples": ["ramp-up from 6 months to 60 days"], "lines": ["a2"]}
    (claim,), problems = cm.check({"claims": [item]}, resume)
    assert problems == [] and claim["examples"] == ["ramp-up from 6 months to 60 days"]


def test_an_id_on_two_lines_keeps_both_so_an_example_may_quote_either_and_both_are_cited():
    first = {"id": "d1", "text": "Led workshops."}
    second = {"id": "d1", "text": "Designed discovery workshops for state clients."}
    roles = [{"id": "r1", "employer": "E", "title": "T", "bullets": [first]},
             {"id": "r2", "employer": "E", "title": "T", "bullets": [second]}]
    resume = _doc(roles=roles)
    item = {"claim": "workshop facilitation", "examples": ["Led workshops", "discovery workshops for state clients"],
            "lines": ["d1"]}
    (claim,), problems = cm.check({"claims": [item]}, resume)
    assert problems == [] and claim["lines"] == ["d1"] and len(claim["examples"]) == 2
    assert "Led workshops." in cm.visible_texts(resume)["d1"] and "state clients" in cm.visible_texts(resume)["d1"]


def test_a_generic_e_g_list_is_refused():
    item = {"claim": "tools", "examples": ["e.g., React, Vue, or Angular"], "lines": ["b1"]}
    assert any("e.g." in p for p in cm.check({"claims": [item]}, _doc())[1])


def test_an_example_that_is_not_in_the_words_of_the_cited_lines_is_refused_and_left_out():
    item = {"claim": "program management", "examples": ["risk working group", "board presentations"],
            "lines": ["b1"]}
    (claim,), problems = cm.check({"claims": [item]}, _doc())
    assert claim["examples"] == ["risk working group"]
    assert 'the example "board presentations" is not in the words of the lines cited' in " ".join(problems)


def test_a_cited_id_that_is_not_a_line_of_the_master_is_refused_and_every_problem_is_named_at_once():
    report = {"claims": [
        {"claim": "planning", "examples": [], "lines": ["nope"]},
        {"claim": "budgets", "examples": []},
        {"claim": "reviews", "examples": [], "lines": ["b1", "gone"]}]}
    clean, problems = cm.check(report, _doc())
    text = " ".join(problems)
    assert "nope is not a line of the master" in text and "no line id" in text
    assert "gone is not a line of the master" in text
    assert [c["claim"] for c in clean] == ["reviews"] and clean[0]["lines"] == ["b1"]


def test_one_capability_shown_by_several_lines_is_one_claim_with_examples_and_ids_combined():
    twice = {"claims": [
        {"claim": "Experience with program management", "examples": ["risk working group"], "lines": ["b1"]},
        {"claim": "program management", "examples": ["quarterly review cycle"], "lines": ["b3"]}]}
    (merged,), problems = cm.check(twice, _doc())
    assert problems == [] and merged["claim"] == "program management"
    assert merged["lines"] == ["b1", "b3"] and merged["examples"] == ["risk working group", "quarterly review cycle"]


def test_nothing_marked_never_in_writing_is_shown_to_the_session_or_can_be_cited():
    resume = _doc()
    assert "[b4]" not in cm.claims_text(resume) and "[b3]" in cm.claims_text(resume)
    clean, problems = cm.check({"claims": [{"claim": "reporting", "examples": [], "lines": ["b4"]}]}, resume)
    assert clean == [] and "b4 is not a line of the master" in " ".join(problems)


def test_a_role_marked_never_in_writing_takes_its_lines_with_it():
    resume = _doc(roles=[{**_resume()["roles"][1], "note": "Never in writing"}, _resume()["roles"][0]])
    assert cm.hidden_ids(resume) >= {"r2", "b3", "b4"}
    assert "r2" not in cm.claims_text(resume) and "b3" not in cm.claims_text(resume)


def test_a_claim_reads_as_experience_with_the_category_and_its_examples_and_the_ids_only_when_asked():
    claim = {"claim": "program management", "examples": ["a", "b"], "lines": ["b1", "b3"]}
    assert cm.render_claim(claim) == "Experience with program management (example: a, b)"
    assert cm.render_claim(claim, lines=True).endswith("[b1, b3]")
    assert cm.render_claim({"claim": "reporting", "examples": [], "lines": ["b5"]}) == "Experience with reporting"
    assert cm.render([claim]).startswith("- Experience with")


def test_every_claim_of_the_record_rests_on_a_quote_that_is_in_the_master():
    resume = _doc()
    record = cm.build(resume, cm.check(REPORT, resume)[0])
    assert record.fields["claims"].source == "sonnet" and record.fields["claims"].quote in cm.visible_texts(
        resume).values()


# --- the job: one for each saved master version, queued when it is saved --------


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
        found = await session.scalars(select(ParseJob).where(ParseJob.kind == "claims").order_by(ParseJob.role_id))
        return list(found.all())


async def _records() -> list[StructuredRecord]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(StructuredRecord).where(StructuredRecord.kind == "claims"))).all())


async def _take_claims(door) -> dict:
    """The next job a session takes, past the master's structured record job that is queued beside it."""
    for _ in range(4):
        taken = await mcp_server.jobs_parse_next(door)
        if taken.get("kind") == "claims":
            return taken
        await mcp_server.jobs_parse_report(door, taken["id"], {"entries": [
            {"category": "skills", "name": "Reviews", "lines": ["b1"]}]})
    raise AssertionError("no claims job was handed out")


async def test_saving_a_master_version_queues_one_claims_job_and_a_sonnet_session_runs_it(agent):
    assert await _save(_resume()) == 1
    (job,) = await _jobs()
    assert (job.state, job.role_id) == ("waiting", "master:1")
    assert "[b1] Led the risk working group" in job.text and "[b4]" not in job.text, "never in writing stays out"
    async with db.session_factory()() as session:
        assert await cm.ensure(session, OWNER) is None, "a job for this version is already waiting"
        first = await role_parse.waiting(session, OWNER)
        assert first is not None and role_parse.is_sonnet(first) is False, "the record's Haiku job goes first"
        await session.commit()
    assert len(await _jobs()) == 1


async def test_the_session_writes_claims_the_master_is_unchanged_and_the_record_is_versioned(agent, door):
    await _save(_resume())
    async with db.session_factory()() as session:
        before = (await session.scalars(select(ProfileVersion.data).where(ProfileVersion.kind == "resume"))).all()
    taken = await _take_claims(door)
    assert taken["how"] == cm.HOW and "claims:" in taken["response_template"]
    assert (await mcp_server.jobs_parse_report(door, taken["id"], REPORT))["state"] == "done"
    async with db.session_factory()() as session:
        after = (await session.scalars(select(ProfileVersion.data).where(ProfileVersion.kind == "resume"))).all()
        assert after == before, "the master is read, never written"
        version, claims = await cm.newest(session, OWNER)
    assert version == 1 and len(claims) == 4
    (row,) = await _records()
    assert (row.fingerprint, row.role_id) == ("1", None)
    assert await _save(_resume(achievements=[{"id": "a1", "text": "Earned a second credential."}])) == 2
    assert [j.role_id for j in await _jobs()] == ["master:1", "master:2"], "each master version has its own job"


async def test_a_report_citing_a_line_that_is_not_the_masters_is_refused_and_keeps_nothing(agent, door):
    await _save(_resume())
    taken = await _take_claims(door)
    bad = {"claims": [{"claim": "planning", "examples": [], "lines": ["nope"]}]}
    async with db.session_factory()() as session:
        with pytest.raises(role_parse.ParseError, match="nope is not a line of the master"):
            await role_parse.report(session, OWNER, taken["id"], bad)
    assert await _records() == [] and (await _jobs())[0].state == "reading"


async def test_a_report_with_no_claim_fails_the_job(agent, door):
    await _save(_resume())
    taken = await _take_claims(door)
    await mcp_server.jobs_parse_report(door, taken["id"], {"claims": []})
    assert (await _jobs())[0].state == "failed" and await _records() == []


# --- who reads them -------------------------------------------------------------------------------------------


async def _with_claims(session) -> None:
    await session.commit()  # `_save` is a writer of its own: this session lets go of the turn first
    await _save(_resume())
    resume = _doc()
    claims = cm.check(REPORT, resume)[0]
    await cm.keep(session, OWNER, 1, cm.build(resume, claims))
    await session.flush()


async def test_the_rankers_short_profile_carries_the_claims_without_ids(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        plain = (await rank.ensure_profile(session, OWNER)).data["text"]
        await _with_claims(session)
        row = await rank.ensure_profile(session, OWNER)
        text = row.data["text"]
    assert "What the candidate has done" not in plain
    assert "Experience with program management (example: risk working group, quarterly review cycle)" in text
    assert "Example Certified Manager credential" in text and "[b1" not in text, "ids mean nothing to a ranker"
    assert text.index("Target roles") < text.index("What the candidate has done") < text.index("Gates (each")
    assert row.version == 2, "a new profile text is the next version, so roles are ranked against it"
    assert rank.render_profile(schema.Profile.model_validate(PROFILE)) == plain, "no claims, no section"


async def test_the_second_impression_gets_the_claims_with_ids_and_the_full_text_of_the_cited_lines(js):
    async with db.session_factory()() as session:
        assert await cm.second_impression_input(session, OWNER) is None, "no claims yet: it reads the master as before"
        await _with_claims(session)
        given = await cm.second_impression_input(session, OWNER)
    assert given is not None and given.version == 1
    assert "Experience with program management (example: risk working group, quarterly review cycle) [b1, b3]" \
        in given.text
    assert given.cited["b1"] == "Led the risk working group across four departments."
    assert "b4" not in given.cited and given.resume is not None and given.resume.roles[0].id == "r1"
