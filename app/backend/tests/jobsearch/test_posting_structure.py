"""Haiku structuring of postings in shadow (the structured data plan spec, build step 4): one parser-kind session
reads a posting's text once and fills what code cannot, every item with a quote that must be in the text; item numbers
are made by code and stay the same on a rerun of the same text; code-filled fields stay code; a snippet is partial.
Nothing reads the record. Synthetic data only."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app import db
from app.jobsearch.doors import scout as mcp_server
from app.jobsearch.jobs import parse as role_parse
from app.jobsearch.jobs import posting_structure as ps
from app.jobsearch.models import JobPosting, Opportunity, ParseJob, StructuredRecord
from app.jobsearch.postings import record as sr
from app.settings import settings
from tests.builders import MINUTE, parse_runner
from tests.support import OWNER, TOKEN

TEXT = """Programs Lead
Pay range: $90,000 - $115,000 per year
Requirements
Bachelor's degree in a related field.
At least 7 years of experience leading a programs team.
Active Secret clearance required.
PMP certification is required.
Duties
Run the quarterly review cycle for the whole office.
Write the weekly summary for leadership.
Preferred
Experience with Gantt tools is a plus.
"""

YEARS_LINE = "At least 7 years of experience leading a programs team."
REPORT = {
    "requirements": [
        {"text": "Lead a programs team for 7 years", "quote": YEARS_LINE},
        {"text": "Bachelor's degree", "quote": "Bachelor's degree in a related field."},
    ],
    "duties": [
        {"text": "Weekly summary", "quote": "Write the weekly summary for leadership."},
        {"text": "Quarterly review", "quote": "Run the quarterly review cycle for the whole office."},
    ],
    "preferred": [{"text": "Gantt tools", "quote": "Experience with Gantt tools is a plus."}],
    "keywords": [{"term": "Gantt tools", "importance": "preferred", "quote": "Experience with Gantt tools is a plus."},
                 {"term": "Programs", "importance": "required", "quote": "leading a programs team"}],
    "certifications": [{"name": "PMP", "required": True, "quote": "PMP certification is required."}],
    "clearance": {"value": "Secret", "quote": "Active Secret clearance required."},
    "degree": {"value": "bachelor", "quote": "Bachelor's degree in a related field."},
    "level": {"value": "Lead", "quote": "Programs Lead"},
    "areas": [{"area": "Programs team leadership", "years": 7, "quote": YEARS_LINE}],
}


# --- what the model may fill, each item with a quote that is in the text ----------


def test_the_good_report_is_tidied_and_numbered_by_code_in_the_order_the_lines_stand_in_the_text():
    clean, problems = ps.check(REPORT, TEXT)
    assert problems == []
    assert [(i["n"], i["text"]) for i in clean["requirements"]] == [
        (1, "Bachelor's degree"), (2, "Lead a programs team for 7 years")]
    assert [i["n"] for i in clean["duties"]] == [1, 2] and clean["duties"][0]["text"] == "Quarterly review"


@pytest.mark.parametrize("section, item", [
    ("requirements", {"text": "Own a boat", "quote": "Must own a boat."}),
    ("duties", {"text": "Sail", "quote": "Sail the ship every day."}),
    ("preferred", {"text": "x", "quote": "not in the posting at all"}),
    ("keywords", {"term": "Rust", "importance": "required", "quote": "five years of Rust"}),
    ("certifications", {"name": "CPA", "required": True, "quote": "CPA license"}),
])
def test_an_item_whose_quote_is_not_in_the_text_is_rejected_and_named(section, item):
    clean, problems = ps.check({section: [item]}, TEXT)
    assert problems and "not in the posting" in " ".join(problems)
    assert ps.is_empty(clean)


@pytest.mark.parametrize("raw", [{"clearance": {"value": "Top", "quote": "nope nope"}},
                                 {"level": {"value": "Senior", "quote": "nowhere"}},
                                 {"requirements": [{"text": "no quote"}]},
                                 {"areas": [{"area": "x", "years": 3, "quote": "elsewhere"}]},
                                 {"mystery": []}, "not an object"])
def test_a_single_field_with_a_missing_quote_or_an_unknown_name_is_refused(raw):
    clean, problems = ps.check(raw, TEXT)
    assert problems and ps.is_empty(clean)


def test_numbers_stay_the_same_on_a_rerun_of_the_same_text_whatever_order_the_model_answers():
    first, _ = ps.check(REPORT, TEXT)
    shuffled = {**REPORT, "requirements": REPORT["requirements"][::-1], "duties": REPORT["duties"][::-1]}
    again, _ = ps.check(shuffled, TEXT)
    assert first["requirements"] == again["requirements"] and first["duties"] == again["duties"]


def test_a_rerun_keeps_the_numbers_it_gave_and_numbers_a_new_item_after_them():
    first, _ = ps.check(REPORT, TEXT)
    more = {**REPORT, "requirements": [
        *REPORT["requirements"], {"text": "Clearance", "quote": "Active Secret clearance required."}]}
    again, _ = ps.check(more, TEXT, previous=first)
    by_text = {i["text"]: i["n"] for i in again["requirements"]}
    assert by_text["Bachelor's degree"] == 1 and by_text["Lead a programs team for 7 years"] == 2
    assert by_text["Clearance"] == 3


# --- the record: code stays code, the model fills the rest ----------


def test_the_record_keeps_the_code_fields_and_adds_only_what_code_left_empty():
    wrong = {**REPORT, "degree": {"value": "master", "quote": "Bachelor's degree in a related field."}}
    clean, _ = ps.check(wrong, TEXT)
    f = ps.build(TEXT, clean, listing_title="Programs Lead").fields
    assert f["degree"].source == "code" and f["degree"].value == "bachelor", "code filled it: it stays code"
    assert f["years"].source == "code" and f["pay"].source == "code" and f["title"].source == "listing"
    assert f["clearance"].source == "haiku" and f["clearance"].value == "Secret"
    assert f["areas"].source == "haiku" and f["areas"].value[0]["years"] == 7
    assert [i["n"] for i in f["requirements"].value] == [1, 2] and f["stack"].source == "haiku"


def test_every_field_of_the_record_rests_on_a_quote_in_the_text_and_it_round_trips():
    clean, _ = ps.check(REPORT, TEXT)
    record = ps.build(TEXT, clean)
    assert sr.check_quotes(record, TEXT) == []
    assert sr.Record.from_dict(record.as_dict()) == record


def test_the_models_degree_fills_the_field_only_when_code_found_none():
    plain = "About the job\nYou will do well.\nA four-term programme in leadership is expected.\n"
    clean, problems = ps.check({"degree": {"value": "master", "quote": "A four-term programme in leadership"}}, plain)
    assert problems == []
    assert ps.build(plain, clean).fields["degree"].source == "haiku"


def test_check_quotes_reads_each_items_own_quote_so_a_made_up_item_makes_the_record_fail():
    clean, _ = ps.check(REPORT, TEXT)
    record = ps.build(TEXT, clean)
    record.fields["duties"].value[0]["quote"] = "a line that is not there"
    assert "duties" in sr.check_quotes(record, TEXT)


def test_a_snippet_makes_a_partial_record():
    clean, _ = ps.check({"level": {"value": "Lead", "quote": "Programs Lead"}}, TEXT)
    record = ps.build(TEXT, clean, partial=True)
    assert record.partial is True


# --- the job: through the pool, kept in shadow, read by nothing ----------


@pytest.fixture
async def agent(js, monkeypatch, scout_token):
    monkeypatch.setattr(settings, "owner", OWNER)
    await parse_runner(seen=MINUTE)


@pytest.fixture
def door():
    return SimpleNamespace(headers={"authorization": f"Bearer {TOKEN}"})


async def _role(text: str | None = TEXT, slug="globex") -> Opportunity:
    async with db.session_factory()() as session:
        role = Opportunity(owner=OWNER, slug=slug, company="Globex", title="Programs Lead", fit=5, origin="scout",
                           match_key=f"{slug}|programs lead", status="second_impression", description=text)
        session.add(role)
        await session.commit()
        return role


async def _queue(n=50, dry_run=False) -> int:
    async with db.session_factory()() as session:
        count = await ps.queue(session, OWNER, n, dry_run=dry_run)
        await session.commit()
        return count


async def _jobs() -> list[ParseJob]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(ParseJob).where(ParseJob.kind == "structure"))).all())


async def _records() -> list[StructuredRecord]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(StructuredRecord).where(StructuredRecord.kind == "posting"))).all())


async def test_queueing_makes_one_waiting_job_for_each_role_text_that_has_no_current_record(agent):
    role = await _role()
    await _role(None, slug="no-text")
    assert await _queue(dry_run=True) == 1 and await _jobs() == []
    assert await _queue() == 1
    (job,) = await _jobs()
    assert (job.state, job.role_id, job.text) == ("waiting", str(role.id), TEXT.strip())
    assert await _queue() == 0, "one is waiting for it already"


async def test_the_queue_stops_at_n(agent):
    for i in range(3):
        await _role(slug=f"r{i}")
    assert await _queue(2) == 2 and len(await _jobs()) == 2


async def test_a_job_is_patient_and_the_owners_own_posting_comes_before_it(agent):
    await _role()
    await _queue()
    async with db.session_factory()() as session:
        await role_parse.expire(session, OWNER, datetime.now(UTC) + timedelta(hours=3))
        await session.commit()
    assert [j.state for j in await _jobs()] == ["waiting"], "no 5 minute limit: nobody waits at the screen"
    async with db.session_factory()() as session:
        mine = await role_parse.start(session, OWNER, "x" * 60)
        assert (await role_parse.waiting(session, OWNER)).id == mine.id
        await session.commit()


async def test_the_session_reads_the_posting_and_the_report_keeps_a_shadow_record_with_code_fields(agent, door):
    role = await _role()
    await _queue()
    taken = await mcp_server.jobs_parse_next(door)
    assert taken["kind"] == "structure" and taken["text"] == TEXT.strip() and taken["how"] == ps.HOW
    assert "requirements:" in taken["response_template"]
    reported = await mcp_server.jobs_parse_report(door, taken["id"], REPORT)
    assert reported["ok"] is True and reported["state"] == "done"
    (row,) = await _records()
    assert (row.role_id, row.partial, row.rules_version) == (role.id, False, ps.RULES_VERSION)
    assert row.fields["years"]["source"] == "code" and row.fields["clearance"]["source"] == "haiku"
    assert (await _jobs())[0].text == "", "the text is not kept on the job once it was read"
    assert await _queue() == 0, "the record is current: nothing more is asked for this text"


async def test_a_report_with_a_quote_that_is_not_in_the_text_is_refused_and_keeps_no_record(agent, door):
    await _role()
    await _queue()
    taken = await mcp_server.jobs_parse_next(door)
    bad = {"requirements": [{"text": "Own a boat", "quote": "Must own a boat."}]}
    async with db.session_factory()() as session:
        with pytest.raises(role_parse.ParseError, match="not in the posting"):
            await role_parse.report(session, OWNER, taken["id"], bad)
    assert await _records() == [] and (await _jobs())[0].state == "reading"


async def test_the_same_text_rerun_keeps_its_numbers_and_a_changed_text_is_a_new_version(agent, door):
    role = await _role()
    await _queue()
    first = await mcp_server.jobs_parse_next(door)
    await mcp_server.jobs_parse_report(door, first["id"], REPORT)
    async with db.session_factory()() as session:
        before = (await session.scalars(select(StructuredRecord.fields))).one()
        assert await ps.ask(session, OWNER, role.id) is not None      # the same text, once more
        await session.commit()
    second = await mcp_server.jobs_parse_next(door)
    shuffled = {**REPORT, "requirements": REPORT["requirements"][::-1]}
    await mcp_server.jobs_parse_report(door, second["id"], shuffled)
    (row,) = await _records()
    assert row.fields["requirements"] == before["requirements"], "the same text numbers the same lines the same way"
    async with db.session_factory()() as session:
        live = await session.get(Opportunity, role.id)
        live.description = TEXT + "Travel up to 10% is required.\n"
        await session.commit()
    assert await _queue() == 1, "a changed text is a new version"


async def test_a_role_with_only_a_listing_snippet_is_structured_as_partial(agent, door):
    role = await _role(None, slug="snippet")
    async with db.session_factory()() as session:
        now = datetime.now(UTC)
        session.add(JobPosting(owner=OWNER, opportunity_id=role.id, employer_id=uuid.uuid4(), ats="browser",
                               ext_id="1", company="Globex", title="Lead", first_seen=now, last_seen=now, links=[],
                               text="Programs Lead\nLead the programs team and write the weekly summary."))
        await session.commit()
    assert await _queue() == 1
    taken = await mcp_server.jobs_parse_next(door)
    await mcp_server.jobs_parse_report(door, taken["id"], {"level": {"value": "Lead", "quote": "Programs Lead"}})
    (row,) = await _records()
    assert row.partial is True


async def test_nothing_reads_the_record_and_the_role_is_unchanged(agent, door):
    role = await _role()
    await _queue()
    taken = await mcp_server.jobs_parse_next(door)
    await mcp_server.jobs_parse_report(door, taken["id"], REPORT)
    async with db.session_factory()() as session:
        live = await session.get(Opportunity, role.id)
        assert live.description == TEXT and live.status == "second_impression"
        assert await session.scalar(select(func.count()).select_from(StructuredRecord)) == 1
