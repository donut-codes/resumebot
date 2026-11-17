"""The page program answers the writes that only ask the scout for work: Check my
roles, the Find Gaps asks, Fill from a posting, Import your resume and the tag job. Each one saves its row, commits, and
only then tells the engine, once, with the reason and the id it always told. The services are stood in for (their own
tests are in tests/jobsearch); what is pinned here is the route: it is the page's, it commits before it knocks and it
asks the engine for nothing else. Everything is made up."""

import uuid
from types import SimpleNamespace

import pytest

from app.jobsearch.gaps import coach as gap_coach
from app.jobsearch.gaps import drafts as gap_drafts
from app.jobsearch.gaps import runs as find_gaps
from app.jobsearch.gaps.runs import GapError
from app.jobsearch.jobs import check as role_check
from app.jobsearch.jobs import parse as role_parse
from app.jobsearch.resume import text as resume_text
from app.jobsearch.work import engine_calls, tagging
from tests.split.writes_support import ID, OTHER, engine_told, knocked, on_programs, send

#: The rows the page serves in its first group.
ASKS = {
    "POST /api/v1/profile/check",
    "POST /api/v1/find-gaps/runs",
    "POST /api/v1/find-gaps/runs/{run_id}/check-again",
    "POST /api/v1/find-gaps/runs/{run_id}/new-sample",
    "POST /api/v1/find-gaps/runs/{run_id}/themes/{theme_id}/draft",
    "POST /api/v1/find-gaps/runs/{run_id}/questions",
    "POST /api/v1/find-gaps/runs/{run_id}/proposals",
    "POST /api/v1/profile/parse",
    "POST /api/v1/profile/parse/resume",
    "POST /api/v1/profile/parse/resume/file",
    "POST /api/v1/profile/roles/tag",
}
POSTING = ("A posting line that is long enough. " * 4).strip()
RESUME = ("A resume line that is long enough. " * 4).strip()
RUNS = "/api/v1/find-gaps/runs"

#: (url, json body, the module and the function the route calls before it commits, the reason it knocks with).
GAPS = [
    pytest.param(RUNS, {"role_id": "r1"}, find_gaps, "start_role", engine_calls.GAPS_ASKED, id="start"),
    pytest.param(f"{RUNS}/{OTHER}/check-again", None, find_gaps, "check_again", engine_calls.GAPS_ASKED,
                 id="check-again"),
    pytest.param(f"{RUNS}/{OTHER}/new-sample", None, find_gaps, "new_sample", engine_calls.GAPS_ASKED,
                 id="new-sample"),
    pytest.param(f"{RUNS}/{OTHER}/themes/t1/draft", {"words": "I led the move."}, gap_drafts, "start",
                 engine_calls.GAPS_ASKED, id="draft"),
    pytest.param(f"{RUNS}/{OTHER}/questions", None, gap_coach, "start_ask", engine_calls.COACH_ASKED, id="questions"),
    pytest.param(f"{RUNS}/{OTHER}/proposals", None, gap_coach, "start_propose", engine_calls.COACH_ASKED,
                 id="proposals"),
]


def test_the_page_serves_these_asks_and_the_engine_does_not():
    missing, still_there = on_programs(ASKS)
    assert missing == [], "rows the page does not serve"
    assert still_there == [], "rows the engine still serves"


async def test_a_check_that_is_made_knocks_the_scout_once_for_its_job(js, monkeypatch):
    async def start(session, owner, scope, text=None, sample=None, now=None):
        return SimpleNamespace(id=uuid.UUID(ID), state=role_check.WAITING), True

    monkeypatch.setattr(role_check, "start", start)
    with engine_told() as told:
        reply = await send("POST", "/api/v1/profile/check", json={"scope": "pursued"})
    assert reply.status_code == 200 and reply.json() == {"id": ID}
    assert knocked(told) == [("knock", ID, engine_calls.CHECK_ASKED)]


async def test_a_check_found_open_and_not_waiting_knocks_nobody(js, monkeypatch):
    async def start(session, owner, scope, text=None, sample=None, now=None):
        return SimpleNamespace(id=uuid.UUID(ID), state=role_check.WORKING), False

    monkeypatch.setattr(role_check, "start", start)
    with engine_told() as told:
        reply = await send("POST", "/api/v1/profile/check", json={"scope": "pursued"})
    assert reply.status_code == 200 and reply.json() == {"id": ID}
    assert knocked(told) == []


@pytest.mark.parametrize(("url", "body", "module", "name", "reason"), GAPS)
async def test_a_find_gaps_ask_commits_then_knocks_once_for_its_row(js, monkeypatch, url, body, module, name, reason):
    async def stand_in(*args, **kwargs):
        return SimpleNamespace(id=uuid.UUID(ID))

    monkeypatch.setattr(module, name, stand_in)
    with engine_told() as told:
        reply = await send("POST", url, **({} if body is None else {"json": body}))
    assert reply.status_code == 200, reply.text
    assert reply.json() == {"id": ID}
    assert knocked(told) == [("knock", ID, reason)]


async def test_a_run_that_cannot_be_made_is_refused_and_nobody_is_knocked(js, monkeypatch):
    async def refuse(*args, **kwargs):
        raise GapError("Fewer than 5 employers have a stored posting for that role.")

    monkeypatch.setattr(find_gaps, "start_role", refuse)
    with engine_told() as told:
        reply = await send("POST", RUNS, json={"role_id": "r1"})
    assert reply.status_code == 422, reply.text  # GapError is a DomainError, status 422
    assert knocked(told) == []


def _jobs_made(monkeypatch) -> list[tuple]:
    """Stand in for the parse job maker and the room check of a resume; the list keeps what the maker was asked for."""
    made: list[tuple] = []

    async def start(session, owner, text, role_id=None, now=None, kind=role_parse.POSTING):
        made.append((text, role_id, kind))
        return SimpleNamespace(id=uuid.UUID(ID))

    async def fits(session, owner, text, *, what, fix):
        return None

    monkeypatch.setattr(role_parse, "start", start)
    monkeypatch.setattr(role_parse, "refuse_resume_over_room", fits)
    return made


async def test_a_pasted_posting_makes_a_parse_job_and_knocks_once_for_it(js, monkeypatch):
    made = _jobs_made(monkeypatch)
    with engine_told() as told:
        reply = await send("POST", "/api/v1/profile/parse", json={"text": POSTING, "role_id": "r1"})
    assert reply.status_code == 200 and reply.json() == {"id": ID}
    assert made == [(POSTING, "r1", role_parse.POSTING)]
    assert knocked(told) == [("knock", ID, engine_calls.PARSE_ASKED)]


async def test_a_pasted_resume_makes_a_resume_job_and_knocks_once_for_it(js, monkeypatch):
    made = _jobs_made(monkeypatch)
    with engine_told() as told:
        reply = await send("POST", "/api/v1/profile/parse/resume", json={"text": RESUME})
    assert reply.status_code == 200 and reply.json() == {"id": ID}
    assert made == [(RESUME, None, role_parse.RESUME)]
    assert knocked(told) == [("knock", ID, engine_calls.PARSE_ASKED)]


async def test_a_resume_file_is_read_in_the_page_and_the_scout_knocked_once(js, monkeypatch):
    made = _jobs_made(monkeypatch)
    monkeypatch.setattr(resume_text, "extract", lambda name, data: RESUME)
    with engine_told() as told:
        reply = await send("POST", "/api/v1/profile/parse/resume/file", params={"name": "cv.txt"},
                           content=b"made up bytes")
    assert reply.status_code == 200 and reply.json() == {"id": ID}
    assert made == [(RESUME, None, role_parse.RESUME)]
    assert knocked(told) == [("knock", ID, engine_calls.PARSE_ASKED)]


async def test_a_resume_file_over_5_mb_is_refused_before_it_is_read(js, monkeypatch):
    made = _jobs_made(monkeypatch)
    read: list[str] = []
    monkeypatch.setattr(resume_text, "extract", lambda name, data: read.append(name) or RESUME)
    with engine_told() as told:
        reply = await send("POST", "/api/v1/profile/parse/resume/file", params={"name": "cv.pdf"},
                           content=b"x" * (resume_text.MAX_BYTES + 1))
    assert reply.status_code == 413, reply.text
    assert read == [] and made == []
    assert knocked(told) == []


async def test_the_tag_job_that_is_open_knocks_the_scout_once(js, monkeypatch):
    async def start(session, owner):
        return SimpleNamespace(id=uuid.UUID(ID)), 3

    monkeypatch.setattr(tagging, "start", start)
    with engine_told() as told:
        reply = await send("POST", "/api/v1/profile/roles/tag")
    assert reply.status_code == 200 and reply.json() == {"open": True, "count": 3}
    assert knocked(told) == [("knock", ID, engine_calls.TAG_ROLES)]


async def test_no_tag_job_when_no_role_needs_a_tag_knocks_nobody(js, monkeypatch):
    async def start(session, owner):
        return None, 0

    monkeypatch.setattr(tagging, "start", start)
    with engine_told() as told:
        reply = await send("POST", "/api/v1/profile/roles/tag")
    assert reply.status_code == 200 and reply.json() == {"open": False, "count": 0}
    assert knocked(told) == []
