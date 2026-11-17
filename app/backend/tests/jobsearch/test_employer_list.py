"""The Employers screen's calls (docs/PRD.md 45, the screens rebuild spec screen 2): the list a page at a time, in a
view, narrowed to a name and an industry, with each view's count; merge and split answering the rows they changed; an
employer's history; and the `employer` live message. Everything is made up."""

import uuid

import pytest

from app import db
from app.jobsearch import stages
from app.jobsearch.employers import core as employers
from app.jobsearch.employers import records as employer_records
from app.jobsearch.pipeline import live, live_sink
from tests.employer_builders import make_employer, make_role, make_site
from tests.support import OWNER


async def _four(session) -> dict[str, str]:
    """Four employers: one with a role ahead and a watched site, one local in another industry, one on the hard pass
    list, one with nothing."""
    acme = await make_employer(session, "Acme", industry="Robotics", aliases=["Acme Robotics"])
    globex = await make_employer(session, "Globex", industry="Banking", local=True)
    initech = await make_employer(session, "Initech", industry="Robotics", gated=True, gated_reason="no")
    umbrella = await make_employer(session, "Umbrella")
    session.add_all([make_role(acme, "Director A", status=stages.READY_TO_LAUNCH), make_role(acme, "Director B"),
                     make_role(globex, "Lead C", status=stages.REJECTED),
                     make_site(acme, "https://careers.acme.test", on=True)])
    await session.commit()
    return {e.name: str(e.id) for e in (acme, globex, initech, umbrella)}


async def test_the_list_comes_a_page_at_a_time_with_each_views_count(client):
    async with db.session_factory()() as session:
        await _four(session)
    first = (await client.get("/api/v1/employers", params={"limit": 3})).json()
    assert [e["name"] for e in first["employers"]] == ["Acme", "Globex", "Initech"]
    assert (first["total"], first["next"]) == (4, "3")
    assert first["counts"] == {"all": 4, "open": 1, "watched": 1, "hard_pass": 1, "local": 1}
    assert first["industries"] == ["Banking", "Robotics"]
    rest = (await client.get("/api/v1/employers", params={"limit": 3, "cursor": first["next"]})).json()
    assert ([e["name"] for e in rest["employers"]], rest["next"]) == (["Umbrella"], None)


async def test_a_row_carries_its_open_roles_the_ones_ahead_and_its_site(client):
    async with db.session_factory()() as session:
        await _four(session)
    acme, globex = (await client.get("/api/v1/employers", params={"limit": 2})).json()["employers"]
    assert (acme["roles_total"], acme["roles_open"], acme["roles_ahead"], acme["site"]["on"]) == (2, 2, 1, True)
    assert (globex["roles_total"], globex["roles_open"], globex["roles_ahead"], globex["site"]) == (1, 0, 0, None)


@pytest.mark.parametrize(("params", "names"), [
    ({"view": "open"}, ["Acme"]),
    ({"view": "watched"}, ["Acme"]),
    ({"view": "hard_pass"}, ["Initech"]),
    ({"view": "local"}, ["Globex"]),
    ({"q": "robotics"}, ["Acme"]),  # another name it goes by
    ({"q": "GLO"}, ["Globex"]),
    ({"industry": "Robotics"}, ["Acme", "Initech"]),
    ({"view": "hard_pass", "industry": "Banking"}, []),
])
async def test_a_view_a_name_and_an_industry_narrow_the_list_but_not_the_counts(client, params, names):
    async with db.session_factory()() as session:
        await _four(session)
    page = (await client.get("/api/v1/employers", params=params)).json()
    assert [e["name"] for e in page["employers"]] == names and page["total"] == len(names)
    assert page["counts"]["all"] == 4


async def test_the_list_refuses_a_view_or_a_cursor_it_does_not_know(client):
    assert (await client.get("/api/v1/employers", params={"view": "starred"})).status_code == 422
    assert (await client.get("/api/v1/employers", params={"cursor": "abc"})).status_code == 422
    assert (await client.get("/api/v1/employers", params={"limit": 0})).status_code == 422


async def test_a_merge_answers_the_row_left_the_one_removed_and_the_counts(client):
    async with db.session_factory()() as session:
        ids = await _four(session)
    done = await client.post(f"/api/v1/employers/{ids['Initech']}/merge", json={"into": ids["Acme"]})
    assert done.status_code == 200
    body = done.json()
    assert body["removed"] == [ids["Initech"]]
    (kept,) = body["rows"]
    assert (kept["id"], kept["gated"], kept["roles_total"], kept["site"]["on"]) == (ids["Acme"], True, 2, True)
    # the hard pass holds on the one left, so its roles not yet applied to went to Gated out
    assert body["counts"] == {"all": 3, "open": 0, "watched": 1, "hard_pass": 1, "local": 1}


async def test_a_split_answers_both_rows_and_the_counts(client):
    async with db.session_factory()() as session:
        ids = await _four(session)
    roles = [r["id"] for r in (await client.get(f"/api/v1/employers/{ids['Acme']}")).json()["roles"]]
    done = await client.post(f"/api/v1/employers/{ids['Acme']}/split", json={"roles": roles[:1], "name": "Hooli"})
    assert done.status_code == 200
    rows = {r["name"]: r for r in done.json()["rows"]}
    assert (rows["Acme"]["roles_total"], rows["Hooli"]["roles_total"]) == (1, 1)
    assert done.json()["counts"]["all"] == 5


async def test_an_employers_history_holds_its_hard_pass_newest_first(client):
    async with db.session_factory()() as session:
        ids = await _four(session)
    await client.patch(f"/api/v1/employers/{ids['Umbrella']}", json={"gated": True, "gated_reason": "a test"})
    await client.patch(f"/api/v1/employers/{ids['Umbrella']}", json={"gated": False})
    history = (await client.get(f"/api/v1/employers/{ids['Umbrella']}")).json()["history"]
    assert [h["sentence"] for h in history] == ["Stopped gating roles from Umbrella.", "Gated roles from Umbrella."]
    assert all(h["at"] for h in history)


async def test_an_employer_changed_or_merged_away_is_announced_with_its_rows_fields(js):
    async with db.session_factory()() as session:
        ids = await _four(session)
    sub = live.broker.subscribe(OWNER, frozenset({"employer"}))
    try:
        async with db.session_factory()() as session:
            await employers.edit(session, OWNER, uuid.UUID(ids["Umbrella"]), {"industry": "Biotech"})
            await session.commit()
        await live_sink.settle()
        async with db.session_factory()() as session:
            await employer_records.merge(session, OWNER, uuid.UUID(ids["Acme"]),
                                         uuid.UUID(ids["Globex"]), why="a test")
            await session.commit()
        await live_sink.settle()
        sent = []
        while not sub.queue.empty():
            _, kind, data = sub.queue.get_nowait()
            sent.append((kind, data))
    finally:
        live.broker.unsubscribe(sub)
    assert all(kind == "employer" for kind, _ in sent)
    changed = next(d for _, d in sent if d["id"] == ids["Umbrella"])
    assert (changed["industry"], changed["name"], changed["gated"]) == ("Biotech", "Umbrella", False)
    assert {"id": ids["Globex"], "deleted": True} in [d for _, d in sent]
    assert any(d["id"] == ids["Acme"] and not d.get("deleted") for _, d in sent), "the one left changed too"
