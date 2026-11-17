"""The page program answers the writes that ask the scout for work and answer with a row (second group): the
summary ask, an interview added or prepared again, and the packet ask. Each one saves its row, commits, and
only then tells the engine, once, with the reason and the id it always told. The services are stood in for
(their own tests are in tests/jobsearch). Everything is made up."""

import uuid
from types import SimpleNamespace

from app.jobsearch.interviews import core as interviews
from app.jobsearch.interviews import routes as interview_routes
from app.jobsearch.research import role_summary
from app.jobsearch.work import engine_calls
from app.jobsearch.writing import packets
from tests.split.writes_support import ID, OTHER, THIRD, engine_told, knocked, on_programs, send

#: The rows the page serves in its second group.
ROWS = {
    "POST /api/v1/roles/{role_id}/summary",
    "POST /api/v1/roles/{role_id}/interviews",
    "POST /api/v1/interviews/{interview_id}/prepare",
    "POST /api/v1/roles/{role_id}/packet",
}
INTERVIEW = {"when": "2026-12-01T10:00:00Z", "round_kind": "phone"}


def test_the_page_serves_these_writes_and_the_engine_does_not():
    missing, still_there = on_programs(ROWS)
    assert missing == [], "rows the page does not serve"
    assert still_there == [], "rows the engine still serves"


async def test_a_summary_that_is_asked_knocks_the_scout_once_for_the_role(js, monkeypatch):
    async def ask(session, owner, role_id):
        return {"asked": True}

    monkeypatch.setattr(role_summary, "ask", ask)
    with engine_told() as told:
        reply = await send("POST", f"/api/v1/roles/{ID}/summary")
    assert reply.status_code == 200 and reply.json() == {"asked": True}
    assert knocked(told) == [("knock", ID, engine_calls.ANALYSIS_ASKED)]


async def test_a_summary_that_is_not_asked_knocks_nobody(js, monkeypatch):
    async def ask(session, owner, role_id):
        return {"asked": False}

    monkeypatch.setattr(role_summary, "ask", ask)
    with engine_told() as told:
        reply = await send("POST", f"/api/v1/roles/{ID}/summary")
    assert reply.status_code == 200 and reply.json() == {"asked": False}
    assert knocked(told) == []


def _interviews_stood_in(monkeypatch, waiting: str | None) -> None:
    """Stand in for the interview service and for the route's read of the prep that waits for the Coach (`waiting` is
    that prep's id, or None when none waits)."""

    async def add(session, owner, role_id, **more):
        return SimpleNamespace(id=uuid.UUID(OTHER))

    async def prepare_again(session, owner, interview_id):
        return None

    async def one(session, owner, interview_id, comp=None):
        return {"id": str(interview_id)}

    async def waiting_prep(session, interview_id):
        return None if waiting is None else uuid.UUID(waiting)

    monkeypatch.setattr(interviews, "add", add)
    monkeypatch.setattr(interviews, "prepare_again", prepare_again)
    monkeypatch.setattr(interviews, "one", one)
    monkeypatch.setattr(interview_routes, "_waiting", waiting_prep)


async def test_an_interview_added_with_a_prep_waiting_knocks_the_coach_once_for_the_prep(js, monkeypatch):
    _interviews_stood_in(monkeypatch, THIRD)
    with engine_told() as told:
        reply = await send("POST", f"/api/v1/roles/{ID}/interviews", json=INTERVIEW)
    assert reply.status_code == 200 and reply.json() == {"id": OTHER}
    assert knocked(told) == [("knock", THIRD, engine_calls.COACH_ASKED)]


async def test_an_interview_added_with_no_prep_waiting_knocks_nobody(js, monkeypatch):
    _interviews_stood_in(monkeypatch, None)
    with engine_told() as told:
        reply = await send("POST", f"/api/v1/roles/{ID}/interviews", json=INTERVIEW)
    assert reply.status_code == 200 and reply.json() == {"id": OTHER}
    assert knocked(told) == []


async def test_prepare_again_knocks_the_coach_once_for_the_new_prep(js, monkeypatch):
    _interviews_stood_in(monkeypatch, THIRD)
    with engine_told() as told:
        reply = await send("POST", f"/api/v1/interviews/{OTHER}/prepare")
    assert reply.status_code == 200 and reply.json() == {"id": OTHER}
    assert knocked(told) == [("knock", THIRD, engine_calls.COACH_ASKED)]


async def test_a_packet_that_is_asked_knocks_the_writer_once_for_it(js, monkeypatch):
    async def ask(session, owner, role_id, feedback=""):
        return SimpleNamespace(id=uuid.UUID(OTHER))

    monkeypatch.setattr(packets, "ask", ask)
    monkeypatch.setattr(packets, "view", lambda packet: {"id": str(packet.id), "status": "writing"})
    with engine_told() as told:
        reply = await send("POST", f"/api/v1/roles/{ID}/packet", json={"feedback": "Shorter."})
    assert reply.status_code == 200 and reply.json() == {"id": OTHER, "status": "writing"}
    assert knocked(told) == [("knock", OTHER, engine_calls.PACKET_ASKED)]
