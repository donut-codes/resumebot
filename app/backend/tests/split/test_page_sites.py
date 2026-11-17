"""Adding, changing and deleting a site on the page: the page saves the change and asks the
engine to try the site or check the log again, then answers the row as it is when the ask ends. The page never fetches
and never re-gates. Every host is made up."""

import uuid

import pytest

from app import db
from app.jobsearch.factory import build_app
from app.jobsearch.gating import regate
from app.jobsearch.work import engine_calls
from app.sites import read_probe, where
from app.sites.models import Site
from tests.split.test_route_programs import _keys

SITES = "/api/v1/sites"
URL = "https://board.example.test/jobs"
SIGN_IN = "It asks for a sign-in."
ROWS = ("POST /api/v1/sites", "PATCH /api/v1/sites/{site_id}", "DELETE /api/v1/sites/{site_id}")


class Engine:
    """Stands in for the engine behind `engine_calls.ask`. Like the real ask it saves what the caller wrote before it
    answers; it records each ask as (name, args, seconds to wait); what the engine would write, `work(session, args)`,
    is written in a session of its own and committed; it answers `answer`, which a test may change."""

    def __init__(self, monkeypatch, *, work=None, answer=None):
        self.asked: list[tuple[str, dict, float]] = []
        self.work, self.answer = work, answer
        monkeypatch.setattr(engine_calls, "ask", self.ask)

    async def ask(self, session, owner, name, args, wait_s):
        await db.release(session)
        self.asked.append((name, args, wait_s))
        if self.work is not None:
            async with db.session_factory()() as engine_session:
                await self.work(engine_session, args)
                await engine_session.commit()
        return self.answer


async def found_browser(session, args):
    """What the engine writes on a site it tried and could not read."""
    site = await session.get(Site, uuid.UUID(args["site_id"]))
    assert site is not None
    await where.set_found(session, site, "browser", SIGN_IN)


def the_page_never_probes_or_regates(monkeypatch):
    async def never(*args, **kwargs):
        raise AssertionError("the page did the engine's work")

    monkeypatch.setattr(read_probe, "settle", never)
    monkeypatch.setattr(regate, "run", never)


@pytest.mark.parametrize("key", ROWS)
def test_the_page_serves_the_site_write_and_the_engine_does_not(key):
    page, engine = (set(_keys(build_app(role))) for role in ("page", "engine"))
    assert key in page
    assert key not in engine


async def test_adding_a_site_to_search_asks_the_engine_to_try_it_and_answers_the_row_it_settled(client, monkeypatch):
    the_page_never_probes_or_regates(monkeypatch)
    engine = Engine(monkeypatch, work=found_browser, answer={"read": "browser", "note": SIGN_IN})
    made = await client.post(SITES, json={"url": URL})
    assert made.status_code == 200
    row = made.json()
    assert (row["read_by"], row["read_found"], row["read_note"]) == ("auto", "browser", SIGN_IN)
    assert "said" not in row
    assert [(name, wait) for name, _, wait in engine.asked] == [("site.probe", 15.0)]
    assert uuid.UUID(engine.asked[0][1]["site_id"]) == uuid.UUID(row["id"])


async def test_a_notice_that_never_came_costs_nothing_the_row_the_engine_settled_is_answered(client, monkeypatch):
    the_page_never_probes_or_regates(monkeypatch)
    Engine(monkeypatch, work=found_browser, answer=None)  # the engine wrote the row; its answer never reached the page
    row = (await client.post(SITES, json={"url": URL})).json()
    assert (row["read_found"], row["read_note"]) == ("browser", SIGN_IN)


async def test_with_no_engine_the_site_is_saved_and_the_row_is_answered_as_saved(client, monkeypatch):
    the_page_never_probes_or_regates(monkeypatch)
    engine = Engine(monkeypatch)
    made = await client.post(SITES, json={"url": URL})
    assert made.status_code == 200
    row = made.json()
    assert (row["on"], row["read_by"], row["read_found"], row["read_note"]) == (True, "auto", None, None)
    assert [name for name, _, _ in engine.asked] == ["site.probe"]
    listed = (await client.get(SITES)).json()["sites"]
    assert [site["id"] for site in listed] == [row["id"]]


async def test_a_site_the_owner_reads_in_their_way_asks_the_engine_nothing(client, monkeypatch):
    the_page_never_probes_or_regates(monkeypatch)
    engine = Engine(monkeypatch)
    made = await client.post(SITES, json={"url": URL, "read_by": "app"})
    assert made.status_code == 200 and made.json()["read_by"] == "app"
    assert engine.asked == []


async def test_a_refused_change_asks_the_engine_nothing(client, monkeypatch):
    engine = Engine(monkeypatch)
    row = (await client.post(SITES, json={"url": URL, "read_by": "app"})).json()
    nothing = await client.patch(f"{SITES}/{row['id']}", json={})
    bad_address = await client.post(SITES, json={"url": "not an address"})
    assert nothing.status_code == 422 and bad_address.status_code == 422
    assert engine.asked == []


async def test_adding_a_site_to_avoid_asks_the_engine_to_check_the_log_and_says_its_line(client, monkeypatch):
    the_page_never_probes_or_regates(monkeypatch)
    line = "Re-checked 2 postings and 1 role: 1 newly stopped."
    engine = Engine(monkeypatch, answer={"said": line})
    made = await client.post(SITES, json={"url": URL, "list": "avoid"})
    assert made.status_code == 200
    row = made.json()
    assert row["said"] == line and row["list"] == "avoid"
    assert [(name, wait) for name, _, wait in engine.asked] == [("site.regate", 15.0)]
    assert engine.asked[0][1] == {"cause": f"{row['url']} was added to the sites you avoid"}


async def test_adding_a_site_to_avoid_with_no_engine_saves_it_and_says_no_line(client, monkeypatch):
    Engine(monkeypatch)
    made = await client.post(SITES, json={"url": URL, "list": "avoid"})
    assert made.status_code == 200
    assert "said" not in made.json() and made.json()["list"] == "avoid"


async def test_changing_how_a_site_is_read_to_auto_asks_the_engine_and_a_switch_does_not(client, monkeypatch):
    the_page_never_probes_or_regates(monkeypatch)
    engine = Engine(monkeypatch, work=found_browser)
    row = (await client.post(SITES, json={"url": URL, "read_by": "app"})).json()
    assert engine.asked == []
    changed = await client.patch(f"{SITES}/{row['id']}", json={"read_by": "auto"})
    assert changed.status_code == 200
    assert (changed.json()["read_by"], changed.json()["read_found"]) == ("auto", "browser")
    assert [name for name, _, _ in engine.asked] == ["site.probe"]
    off = await client.patch(f"{SITES}/{row['id']}", json={"on": False})
    assert off.status_code == 200 and off.json()["on"] is False
    assert [name for name, _, _ in engine.asked] == ["site.probe"]


async def test_taking_a_site_off_the_avoid_list_asks_the_engine_and_says_its_line_when_it_answers(client, monkeypatch):
    the_page_never_probes_or_regates(monkeypatch)
    line = "Re-checked 1 posting and 0 roles: 1 now pass."
    engine = Engine(monkeypatch)
    first = (await client.post(SITES, json={"url": URL, "list": "avoid"})).json()
    engine.answer = {"said": line}
    removed = await client.delete(f"{SITES}/{first['id']}")
    assert removed.status_code == 200 and removed.json() == {"ok": True, "said": line}
    assert engine.asked[-1][0] == "site.regate"
    assert engine.asked[-1][1] == {"cause": f"{first['url']} was taken off the sites you avoid"}
    second = (await client.post(SITES, json={"url": "https://other.example.test/", "list": "avoid"})).json()
    engine.answer = None
    quiet = await client.delete(f"{SITES}/{second['id']}")
    assert quiet.status_code == 200 and quiet.json() == {"ok": True}
    listed = (await client.get(SITES)).json()["sites"]
    assert first["id"] not in [site["id"] for site in listed] and second["id"] not in [site["id"] for site in listed]
