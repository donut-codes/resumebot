"""The reply of an interview write is read after the commit on a reader (the rule: the writer's turn is for writing).
The services are stood in for; the stand-in for `interviews.one` asks the session it is given for one row, so a reply
read in the writer's session would take the writer's turn and a reply read on a reader takes none. Everything is
made up."""

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.jobsearch.factory import build_app
from app.jobsearch.interviews import core as interviews
from app.jobsearch.interviews import routes as interview_routes
from tests.split.test_page_reads_take_no_write_lock import turns_recorded
from tests.split.writes_support import ID, OTHER, engine_told, send

#: (method, url, json body, the service function the route calls before it commits).
CASES = [
    pytest.param("POST", f"/api/v1/roles/{ID}/interviews", {"when": "2026-12-01T10:00:00Z", "round_kind": "phone"},
                 "add", id="add"),
    pytest.param("PATCH", f"/api/v1/interviews/{OTHER}", {"notes": "Bring the deck."}, "edit", id="edit"),
    pytest.param("POST", f"/api/v1/interviews/{OTHER}/prepare", None, "prepare_again", id="prepare"),
]


@pytest.mark.parametrize(("method", "url", "body", "service"), CASES)
async def test_an_interview_write_reads_its_reply_on_a_reader(js, monkeypatch, method, url, body, service):
    async def changed(*args, **kwargs):
        return SimpleNamespace(id=uuid.UUID(OTHER))

    async def nothing_waits(session, interview_id):
        return None

    async def one(session, owner, interview_id, comp=None):
        await session.execute(text("SELECT 1"))  # a statement in a writer's session takes the writer's turn
        return {"id": str(interview_id)}

    monkeypatch.setattr(interviews, service, changed)
    monkeypatch.setattr(interview_routes, "_waiting", nothing_waits)
    monkeypatch.setattr(interviews, "one", one)
    with engine_told(), turns_recorded() as turns:
        reply = await send(method, url, app=build_app("all"), **({} if body is None else {"json": body}))
    assert reply.status_code == 200, reply.text
    assert reply.json() == {"id": OTHER}
    assert not turns.waits and not turns.ends, "the reply was read in the writer's session and took its turn"
