"""No character limit on the text the owner brings to Import your resume (the owner, 2026-10-14: a length limit on what
is
typed is not wanted). A pasted resume and the text of a resume file are
kept whole, and so are the resume's own words in the agent's report: a long line, a long summary, a long bullet arrives
as the resume has it, never split or shortened. What can refuse a resume is one thing: an agent cannot be sent more
than `ROOM_CHARS` of the resume and the answer shape together, which is said in words, with the sizes, before a job is
made. The counts stay (40 roles, 40 bullets). A pasted posting is Search Settings text and gets no room check.
Synthetic text only."""

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app import db
from app.harness import instructions as harness_instructions
from app.jobsearch import owner_text
from app.jobsearch.doors import scout as mcp_server
from app.jobsearch.jobs import parse as role_parse
from app.jobsearch.main import app
from app.jobsearch.models import ParseJob
from app.jobsearch.resume import parse as resume_parse
from app.jobsearch.resume import text as resume_text
from app.jobsearch.work import instruction_files
from app.planengine.budget import REPLY_CEILING_CHARS, payload_chars
from app.settings import settings
from tests.builders import MINUTE, parse_row, parse_runner
from tests.support import OWNER, TOKEN, load_profile

LONG = 20_000
BIG = 120_000
#: A posting past the room a resume has, and still under what a reply to an agent can hold (the door, not the paste,
#: refuses a reply past `REPLY_CEILING_CHARS`).
PAST_THE_ROOM = owner_text.ROOM_CHARS + 20_000


@pytest.fixture
async def client(js, monkeypatch):
    """The app as the owner with the made-up profile saved (its settings do not avoid Claude's peak hours, so no test
    depends on the hour it runs)."""
    monkeypatch.setattr(settings, "owner", OWNER)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _words(tag: str, n: int = LONG) -> str:
    """About `n` characters of made-up plain sentences, each different, so a cut anywhere shows."""
    out, i = [], 0
    while sum(len(s) + 1 for s in out) < n:
        i += 1
        out.append(f"{tag} sentence {i} says one more thing the owner wants.")
    return " ".join(out)[: n - 1] + "!"  # exactly n characters, and no space at either end for a save to trim


@pytest.fixture
def door(scout_token):
    return SimpleNamespace(headers={"authorization": f"Bearer {TOKEN}"})


async def _count() -> int:
    async with db.session_factory()() as session:
        return await session.scalar(select(func.count()).select_from(ParseJob))


async def _shape_size() -> int:
    """The size of the resume's answer shape as saved now: what an agent is sent beside the resume."""
    async with db.session_factory()() as session:
        return len(await instruction_files.read(session, OWNER, "templates", role_parse.RESUME_TEMPLATE))


def _paste(client, text: str):
    return client.post("/api/v1/profile/parse/resume", json={"text": text})


def _file(client, name: str, data: bytes):
    return client.post("/api/v1/profile/parse/resume/file", params={"name": name}, content=data,
                       headers={"Content-Type": "application/octet-stream"})


def _sentence(what: str, size: int, shape: int, fix: str) -> str:
    """Sentences 1 and 2 of the spec's "Screens and words", built from the real sizes."""
    return (f"{what} is {size:,} characters, and the answer shape sent with it is {shape:,}. An agent can be sent at "
            f"most {owner_text.ROOM_CHARS:,} of the two at once, so it was not sent. Nothing was cut and nothing was "
            f"saved. {fix}")


# --- 23: the pasted resume ----------


async def test_a_pasted_resume_of_120000_characters_makes_a_job_and_the_agent_is_handed_all_of_it(client, door, knocks):
    await parse_runner(seen=MINUTE)
    resume = _words("resume", BIG)
    res = await _paste(client, resume)
    assert res.status_code == 200, res.text
    assert (await parse_row(res.json()["id"])).text == resume, "kept whole"
    taken = await mcp_server.jobs_parse_next(door)
    assert taken["kind"] == "resume" and taken["text"] == resume and len(taken["text"]) == BIG
    assert [reason for _id, reason in knocks] == ["parse_asked"]


async def test_a_pasted_resume_fits_up_to_the_room_less_the_answer_shape_and_one_over_is_refused_in_words(
        client, door, knocks):
    await parse_runner(seen=MINUTE)
    shape = await _shape_size()
    assert 0 < shape < 20_000, "the shipped answer shape is small"
    room = owner_text.ROOM_CHARS - shape
    resume = _words("resume", room)
    assert (await _paste(client, resume)).status_code == 200
    taken = await mcp_server.jobs_parse_next(door)
    assert taken["ok"] is True and taken["text"] == resume
    assert payload_chars({"ok": True, **taken}) < REPLY_CEILING_CHARS, "what the room allows, the door can send"
    made, woken = await _count(), len(knocks)
    over = await _paste(client, _words("resume", room + 1))
    assert over.status_code == 413
    assert over.json() == {"detail": _sentence("Your resume text", room + 1, shape, "Check that it is your resume.")}
    assert await _count() == made, "a refusal makes no job"
    assert len(knocks) == woken, "and wakes no agent"


async def test_the_room_is_less_the_answer_shape_at_the_size_now_saved(client):
    await parse_runner(seen=MINUTE)
    async with db.session_factory()() as session:
        text = await instruction_files.read(session, OWNER, "templates", role_parse.RESUME_TEMPLATE)
        await instruction_files.save(session, OWNER, "templates", role_parse.RESUME_TEMPLATE,
                                     f"{text}\n# {_words('note', 5_000)}", "a longer shape, for the room")
        await session.commit()
    shape = await _shape_size()
    assert shape > 5_000
    room = owner_text.ROOM_CHARS - shape
    assert (await _paste(client, _words("resume", room))).status_code == 200
    over = await _paste(client, _words("resume", room + 1))
    assert over.status_code == 413
    assert over.json()["detail"] == _sentence("Your resume text", room + 1, shape, "Check that it is your resume.")


async def test_the_room_is_measured_on_the_trimmed_text_the_agent_would_be_sent(client):
    await parse_runner(seen=MINUTE)
    room = owner_text.ROOM_CHARS - await _shape_size()
    res = await _paste(client, "   " + _words("resume", room) + "\n\n   ")
    assert res.status_code == 200, "the spaces around it are trimmed, not counted"


# --- 23: the text of a file ----------


async def test_a_file_whose_text_is_120000_characters_reads_and_is_handed_whole(client, door):
    await parse_runner(seen=MINUTE)
    resume = _words("file", BIG)
    res = await _file(client, "resume.txt", resume.encode())
    assert res.status_code == 200, res.text
    assert (await parse_row(res.json()["id"])).text == resume
    taken = await mcp_server.jobs_parse_next(door)
    assert taken["kind"] == "resume" and taken["text"] == resume


async def test_a_file_whose_text_passes_the_room_is_refused_in_words_and_makes_no_job(client, knocks):
    await parse_runner(seen=MINUTE)
    shape = await _shape_size()
    room = owner_text.ROOM_CHARS - shape
    assert (await _file(client, "resume.txt", _words("file", room).encode())).status_code == 200
    made, woken = await _count(), len(knocks)
    over = await _file(client, "resume.txt", _words("file", room + 1).encode())
    assert over.status_code == 413
    assert over.json() == {"detail": _sentence("This file's text", room + 1, shape, "Check that it is the right file.")}
    assert await _count() == made and len(knocks) == woken


def test_reading_a_file_has_no_most_of_its_own_text_only_the_5_mb_file_stays():
    text = _words("file", 150_000)
    assert resume_text.extract("resume.txt", text.encode()) == text
    assert resume_text.MAX_BYTES == 5 * 1024 * 1024
    assert not hasattr(resume_parse, "MAX_CHARS") and not hasattr(resume_text, "TOO_LONG")


# --- 25: the resume's own words in the agent's report ----------


def _long_report() -> dict:
    return {
        "summary": _words("summary", 5_000),
        "achievements": [_words("achievement", 3_000)],
        "competencies": [{"area": "A" * 400, "items": ["t" * 120, "short"]}],
        "roles": [{
            "employer": "E" * 400, "title": "T" * 400, "location": "L" * 400, "start": "S" * 400, "end": "N" * 400,
            "descriptor": "D" * 400, "intro": _words("intro", 3_000),
            "bullets": [_words("bullet", 3_000), "A short one."],
        }],
        "education": [{"degree": "G" * 400, "school": "H" * 400}],
        "philosophy": _words("philosophy", 5_000),
        "awards": [_words("award", 2_000)],
        "projects": [_words("project", 2_000)],
        "contact": {"name": "P" * 400, "location": "Q" * 400, "address": "R" * 400, "phone": "5" * 100,
                    "email": "e" * 400, "links": ["l" * 400]},
    }


def test_a_resume_report_with_a_long_summary_bullet_area_and_term_is_accepted_whole():
    report = _long_report()
    clean, problems = resume_parse.check(report)
    assert problems == []
    assert clean == report, "every word as the resume has it, none split, shortened or dropped"
    assert len(clean["summary"]) == 5_000 and len(clean["roles"][0]["bullets"][0]) == 3_000
    assert len(clean["competencies"][0]["area"]) == 400 and len(clean["competencies"][0]["items"][0]) == 120


def test_the_counts_of_a_resume_report_stay():
    for fields, problem in [
        ({"roles": [{"employer": "x"}] * 41}, "roles has 41; at most 40"),
        ({"achievements": ["x"] * 41}, "achievements has 41; at most 40"),
        ({"roles": [{"employer": "x", "bullets": ["b"] * 41}]}, "roles[0].bullets has 41; at most 40"),
        ({"competencies": [{"area": "x", "items": ["t"] * 61}]}, "competencies[0].items has 61; at most 60"),
    ]:
        _, problems = resume_parse.check(fields)
        assert problem in problems, problem


async def test_a_long_report_reaches_the_page_whole_through_the_two_tools(client, door):
    await parse_runner(seen=MINUTE)
    job_id = (await _paste(client, _words("resume", 10_000))).json()["id"]
    await mcp_server.jobs_parse_next(door)
    report = _long_report()
    reported = await mcp_server.jobs_parse_report(door, job_id, report)
    assert reported["ok"] is True, reported
    done = (await client.get(f"/api/v1/profile/parse/{job_id}")).json()
    assert done["state"] == "done" and done["fields"] == report


# --- 26: a posting has no room check ----------


async def test_a_posting_pasted_to_fill_from_has_no_room_check_and_is_handed_whole(client, door, knocks):
    await parse_runner(seen=MINUTE)
    posting = _words("posting", PAST_THE_ROOM)
    res = await client.post("/api/v1/profile/parse", json={"text": posting, "role_id": "builders"})
    assert res.status_code == 200, res.text
    assert (await mcp_server.jobs_parse_next(door))["text"] == posting
    assert [reason for _id, reason in knocks] == ["parse_asked"]


async def test_a_posting_pasted_to_check_my_roles_has_no_room_check_and_is_handed_whole(client, door, knocks):
    await parse_runner(seen=MINUTE)
    posting = _words("posting", PAST_THE_ROOM)
    res = await client.post("/api/v1/profile/check", json={"scope": "posting", "text": posting})
    assert res.status_code == 200, res.text
    assert (await mcp_server.jobs_match_next(door))["items"] == [{"id": "1", "text": posting}]


# --- 27: the instruction file ----------


def test_the_resume_instructions_no_longer_tell_the_agent_to_split_a_long_bullet():
    text = harness_instructions.read("instructions", "jobsearch.parser.resume")
    assert "1500" not in text and "4000" not in text and "split at a sentence" not in text
    assert "a line too long" not in text
    # The one split allowed (476f5633, feat/plain-master): a line of several distinct facts, one line per fact.
    assert ("A line is as long as the resume has it. Never split, shorten or join lines beyond what 'Copying' "
            "allows: the only split is one line of several distinct facts made into one line per fact.") in text
    assert "(a field of the wrong kind, a list that is not a list)" in text
    assert "Never rewrite, shorten, summarise" in text, "the rule about copying the person's words stays"
