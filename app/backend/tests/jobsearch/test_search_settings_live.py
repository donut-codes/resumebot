"""What Search Settings needs from the API to save a field at a time (the screens rebuild spec, step 3): a save of a
profile part answers the whole profile, the reason for a save can be said after it, each line of Hard pass says how
many it stopped, and the live stream announces a saved profile version and a changed site. Everything is made up."""

import uuid
from datetime import UTC, date, datetime

import pytest

from app import db
from app.jobsearch import stages
from app.jobsearch.models import JobPosting, Opportunity
from app.jobsearch.pipeline import live, live_sink
from app.sites.models import Site
from tests.support import EXAMPLE_SYSTEM, OWNER

CRITERIA = {"company": "A made-up kind of employer.", "target_roles": []}


async def _drain(sub: live.Subscriber) -> list[tuple[str, dict]]:
    await live_sink.settle()
    out = []
    while not sub.queue.empty():
        _, kind, data = sub.queue.get_nowait()
        out.append((kind, data))
    return out


@pytest.fixture
def listening(js):
    sub = live.broker.subscribe(OWNER, frozenset({"profile_version", "site"}))
    yield sub
    live.broker.unsubscribe(sub)


async def test_a_save_of_a_profile_part_answers_the_whole_profile(client):
    saved = (await client.put("/api/v1/profile/criteria", json={"data": CRITERIA, "base_version": 0})).json()
    assert saved["saved"] is True and saved["version"] == 1
    assert saved["profile"]["criteria"]["company"] == CRITERIA["company"]
    assert saved["profile"]["versions"]["criteria"] == 1, "the versions come with it, so the page need not ask again"
    assert "says" in saved["profile"]


async def test_the_reason_for_the_newest_version_can_be_said_after_it_was_saved(client):
    await client.put("/api/v1/profile/criteria", json={"data": CRITERIA})
    answer = await client.put("/api/v1/profile/criteria/versions/1/why", json={"why": "  a made-up reason  "})
    assert answer.status_code == 200 and answer.json()["why"] == "a made-up reason"
    history = (await client.get("/api/v1/profile/criteria/history")).json()["versions"]
    assert history[0]["why"] == "a made-up reason"


async def test_only_the_newest_version_takes_a_reason_after_it_was_saved(client):
    await client.put("/api/v1/profile/criteria", json={"data": CRITERIA})
    await client.put("/api/v1/profile/criteria", json={"data": {**CRITERIA, "company": "Another made-up kind."}})
    answer = await client.put("/api/v1/profile/criteria/versions/1/why", json={"why": "too late"})
    assert answer.status_code == 422 and "not the newest" in answer.json()["detail"]


async def test_each_line_of_hard_pass_says_how_many_roles_and_postings_it_stopped(client):
    async with db.session_factory()() as session:
        for slug, line in (("a", "pay-rule"), ("b", "pay-rule"), ("c", "location")):
            session.add(Opportunity(owner=OWNER, slug=slug, company=f"Co {slug}", title=f"Role {slug}",
                                    status=stages.GATED_OUT, origin="scout", match_key=f"co {slug}|role {slug}",
                                    found=date.today(), screened_out_gate_id=line))
        session.add(Opportunity(owner=OWNER, slug="d", company="Co d", title="Role d", status=stages.FIRST_IMPRESSION,
                                origin="scout", match_key="co d|role d", found=date.today(),
                                screened_out_gate_id="pay-rule"))
        now = datetime.now(UTC)
        session.add(JobPosting(owner=OWNER, employer_id=uuid.uuid4(), ats=EXAMPLE_SYSTEM, ext_id="p1",
                               title="A posting", company="Co e", first_seen=now, last_seen=now,
                               stop_gate_id="location"))
        await session.commit()
    stopped = (await client.get("/api/v1/profile/gates/stopped")).json()["stopped"]
    assert stopped == {"pay-rule": 2, "location": 2}, "a role no longer Gated out counts for nothing"


async def test_a_saved_profile_version_is_announced(client, listening):
    await client.put("/api/v1/profile/scoring", json={"data": {"asks": [], "match_weight": 0.7}})
    messages = [(k, d) for k, d in await _drain(listening) if k == "profile_version"]
    assert [(d["kind"], d["version"], d["saved_by"]) for _, d in messages] == [("scoring", 1, "owner")]
    await client.put("/api/v1/profile/scoring/versions/1/why", json={"why": "made up"})
    (kind, data), = [(k, d) for k, d in await _drain(listening) if k == "profile_version"]
    assert data["why"] == "made up", "a reason said afterwards is announced too"


async def test_a_site_added_changed_and_removed_is_announced(js, listening):
    async with db.session_factory()() as session:
        site = Site(owner=OWNER, url="jobs.example.org", kind="job_board", on=True, created_at=datetime.now(UTC),
                    list_name="avoid")
        session.add(site)
        await session.commit()
        site_id = site.id
    (kind, data), = await _drain(listening)
    assert kind == "site" and data["id"] == str(site_id) and data["list"] == "avoid" and data["on"] is True
    async with db.session_factory()() as session:
        row = await session.get(Site, site_id)
        row.label = "A made-up board"
        await session.commit()
    (kind, data), = await _drain(listening)
    assert kind == "site" and data["label"] == "A made-up board"
    async with db.session_factory()() as session:
        await session.delete(await session.get(Site, site_id))
        await session.commit()
    assert await _drain(listening) == [("site", {"id": str(site_id), "deleted": True})]


def test_the_new_topics_can_be_asked_for_by_name():
    assert live.parse_topics("profile_version,site") == frozenset({"profile_version", "site"})
