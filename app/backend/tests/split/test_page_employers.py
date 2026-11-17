"""Changing an employer on the page: the page saves the change; gating or ungating the
employer is checked by the engine (the log, the roles, the trail, the watch), which the page asks and waits for, then
answers the employer as it is. The page never re-gates. Every name here is made up."""

import uuid

import pytest

from app import db
from app.jobsearch.employers import core as employers
from app.jobsearch.factory import build_app
from app.jobsearch.gating import regate
from app.jobsearch.models import Employer
from tests.split.test_page_sites import Engine
from tests.split.test_route_programs import _keys
from tests.support import OWNER

KEY = "PATCH /api/v1/employers/{employer_id}"
LINE = "Re-checked 0 postings and 3 roles: 2 newly stopped."


def the_page_never_regates(monkeypatch):
    async def never(*args, **kwargs):
        raise AssertionError("the page re-gated")

    monkeypatch.setattr(regate, "run", never)


async def _employer(name: str) -> str:
    async with db.session_factory()() as session:
        employer = await employers.find_or_make(session, OWNER, name)
        assert employer is not None  # a plain name always makes or finds one
        await session.flush()
        employer_id = str(employer.id)
        await session.commit()
    return employer_id


async def engine_marks_it(session, args):
    """What the engine writes on the employer while it checks."""
    employer = await session.get(Employer, uuid.UUID(args["employer_id"]))
    assert employer is not None
    employer.notes = "the engine was here"


def test_the_page_serves_the_employer_change_and_the_engine_does_not():
    page, engine = (set(_keys(build_app(role))) for role in ("page", "engine"))
    assert KEY in page
    assert KEY not in engine


async def test_gating_an_employer_saves_the_flag_asks_the_engine_and_says_its_line(client, monkeypatch):
    the_page_never_regates(monkeypatch)
    employer_id = await _employer("Initech")
    engine = Engine(monkeypatch, answer={"said": LINE})
    done = await client.patch(f"/api/v1/employers/{employer_id}", json={"gated": True, "gated_reason": "never again"})
    assert done.status_code == 200
    body = done.json()
    assert body["gated"] is True and body["gated_reason"] == "never again" and body["said"] == LINE
    assert "gate_changed" not in body
    assert engine.asked == [("employer.regate", {"employer_id": employer_id, "by": "owner"}, 15.0)]


async def test_the_employer_is_answered_as_it_is_after_the_engines_check(client, monkeypatch):
    the_page_never_regates(monkeypatch)
    employer_id = await _employer("Initech")
    Engine(monkeypatch, work=engine_marks_it, answer={"said": LINE})
    body = (await client.patch(f"/api/v1/employers/{employer_id}", json={"gated": True})).json()
    assert body["notes"] == "the engine was here" and body["said"] == LINE


async def test_with_no_engine_the_gate_is_saved_and_the_employer_answered_without_a_line(client, monkeypatch):
    the_page_never_regates(monkeypatch)
    employer_id = await _employer("Initech")
    Engine(monkeypatch)
    done = await client.patch(f"/api/v1/employers/{employer_id}", json={"gated": True, "gated_reason": "never again"})
    assert done.status_code == 200
    assert done.json()["gated"] is True and "said" not in done.json()
    assert (await client.get(f"/api/v1/employers/{employer_id}")).json()["gated"] is True


@pytest.mark.parametrize("changes", [{"industry": "Biotech"}, {"gated": False}, {"gated": False, "notes": "n"}])
async def test_a_change_that_does_not_flip_the_gate_asks_the_engine_nothing(client, monkeypatch, changes):
    the_page_never_regates(monkeypatch)
    employer_id = await _employer("Initech")
    engine = Engine(monkeypatch)
    done = await client.patch(f"/api/v1/employers/{employer_id}", json=changes)
    assert done.status_code == 200
    assert "said" not in done.json() and "gate_changed" not in done.json()
    assert engine.asked == []
    for field, value in changes.items():
        assert done.json()[field] == value


async def test_a_refused_change_asks_the_engine_nothing(client, monkeypatch):
    employer_id = await _employer("Initech")
    engine = Engine(monkeypatch)
    refused = await client.patch(f"/api/v1/employers/{employer_id}", json={"gated": "yes please"})
    assert refused.status_code == 422
    assert engine.asked == []
