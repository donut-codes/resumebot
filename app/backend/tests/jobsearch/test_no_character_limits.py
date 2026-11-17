"""No character limit on what the owner writes in Search Settings (the owner, 2026-10-14). A value of 20,000 characters
saves and reads back whole on each tab's API: What to look for
(the company and every text of a target role, and its name, and a posting pasted into "Paste a job posting" or "Check my
roles"), Hard pass (a sentence rule, for every role and for one), Prioritization (an ask), the compensation method's
words on Hard pass, Where to look (a site's address, which the database keeps once whatever its length), and the note
that goes with a save. What stays is a count (how many roles, how many example titles), a fewest on a pasted posting
and a request's whole size. Synthetic data only."""

import hashlib
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from app import db
from app.jobsearch.doors import scout as mcp_server
from app.jobsearch.profile import schema as p
from app.sites.models import Site
from app.timeutil import utc_now
from tests.builders import MINUTE, parse_runner
from tests.support import FIVE, OWNER, PROFILE, TOKEN, bare

LONG = 20_000


def _words(tag: str, n: int = LONG) -> str:
    """About `n` characters of made-up plain sentences, each different, so a cut anywhere shows."""
    out, i = [], 0
    while sum(len(s) + 1 for s in out) < n:
        i += 1
        out.append(f"{tag} sentence {i} says one more thing the owner wants.")
    return " ".join(out)[: n - 1] + "!"  # exactly n characters, and no space at either end for a save to trim


async def test_what_to_look_for_keeps_a_long_company_role_name_and_every_text_of_a_role_whole(client):
    texts = {part: _words(part) for part in p.ROLE_TEXT}
    role = {"id": "builders", "name": "Director of " + "Programs and Operations " * 40, "titles": ["Director"], **texts}
    company = _words("company")
    why = _words("why", 5_000)
    saved = await client.put("/api/v1/profile/criteria", json={"data": {"company": company, "target_roles": [role]},
                                                               "why": why})
    assert saved.status_code == 200 and saved.json()["saved"] is True, saved.text
    got = (await client.get("/api/v1/profile")).json()["criteria"]
    assert got["company"] == company and len(got["company"]) == LONG
    [back] = got["target_roles"]
    assert back["name"] == " ".join(role["name"].split()) and len(back["name"]) > 120
    for part, text in texts.items():
        assert back[part] == text and len(back[part]) == LONG, part
    [version] = (await client.get("/api/v1/profile/criteria/history")).json()["versions"]
    assert version["why"] == why, "the note that goes with a save is kept whole too"
    again = await client.put("/api/v1/profile/criteria", json={"data": got, "why": "no change"})
    assert bare(again.json()) == {"saved": False, "version": None}, "read back, it is the same document"


async def test_hard_pass_keeps_a_long_sentence_rule_for_every_role_and_for_one_role(client):
    await client.put("/api/v1/profile/criteria", json={"data": PROFILE["criteria"]})
    every, one = _words("every role"), _words("one role")
    rules = [
        {"id": "long-all", "kind": "rule", "section": "description", "text": every},
        {"id": "long-one", "kind": "rule", "section": "qualifications", "text": one, "roles": ["builders"]},
    ]
    saved = await client.put("/api/v1/profile/gates", json={"data": {**PROFILE["gates"], "other": rules}})
    assert saved.status_code == 200 and saved.json()["saved"] is True, saved.text
    body = (await client.get("/api/v1/profile")).json()
    mine = {g["id"]: g for g in body["gates"]["other"]}
    assert mine["long-all"]["text"] == every and len(mine["long-all"]["text"]) == LONG
    assert mine["long-one"]["text"] == one and mine["long-one"]["roles"] == ["builders"]
    assert body["says"]["gates"]["other"]["long-all"] == every, "the page shows the sentence as it was written"


async def test_hard_pass_keeps_long_words_about_the_pay_it_assumes(client):
    assumed = _words("assumes")
    saved = await client.put("/api/v1/profile/settings", json={"data": {"comp_assumptions": assumed}})
    assert saved.status_code == 200 and saved.json()["saved"] is True, saved.text
    assert (await client.get("/api/v1/profile")).json()["settings"]["comp_assumptions"] == assumed


async def test_prioritization_keeps_a_long_ask(client):
    ask = {"id": "long-ask", "section": "description", "says": _words("ask"), "points": 2}
    saved = await client.put("/api/v1/profile/scoring", json={"data": {"asks": [*FIVE["asks"], ask]}})
    assert saved.status_code == 200 and saved.json()["saved"] is True, saved.text
    body = (await client.get("/api/v1/profile")).json()
    mine = {a["id"]: a for a in body["scoring"]["asks"]}
    assert mine["long-ask"]["says"] == ask["says"] and len(mine["long-ask"]["says"]) == LONG


def _address(end: str, n: int = LONG) -> str:
    """An address of exactly `n` characters: a path of made-up lowercase words that do not repeat (so Postgres cannot
    squeeze it small), the same for every call but for its last character."""
    out, i = "https://board.example.test/careers", 0
    while len(out) < n:
        i += 1
        letters = "".join(chr(97 + b % 26) for b in hashlib.sha256(str(i).encode()).digest())
        out += "/" + letters[: 4 + i % 9] + "/" + letters[16 : 20 + i % 9]
    return out[: n - 1] + end


async def test_where_to_look_takes_a_site_address_of_20000_characters_and_keeps_one_row_for_each(client):
    one, other = _address("a"), _address("b")
    assert len(one) == len(other) == LONG and one[:-1] == other[:-1]
    made = await client.post("/api/v1/sites", json={"url": one, "kind": "job_board", "read_by": "app"})
    assert made.status_code == 200, made.text
    assert made.json()["url"] == one, "it comes back whole"
    again = await client.post("/api/v1/sites", json={"url": one, "kind": "job_board", "read_by": "app"})
    assert again.status_code == 200 and again.json()["id"] == made.json()["id"], "the same address is the same site"
    second = await client.post("/api/v1/sites", json={"url": other, "kind": "job_board", "read_by": "app"})
    assert second.status_code == 200 and second.json()["id"] != made.json()["id"], "another ending is another site"
    rows = (await client.get("/api/v1/sites")).json()["sites"]
    assert sorted(r["url"] for r in rows if r["url"] in (one, other)) == sorted([one, other])


async def test_the_database_keeps_a_20000_character_site_address_once_for_an_owner(js):
    """The unique index is over a hash of the address (migration 0072): Postgres refuses a btree entry of the address
    itself past about 2.7 KB, which would have been a 500 for an address that long."""
    address = _address("a")
    async with db.session_factory()() as session:
        session.add(Site(owner=OWNER, url=address, kind="job_board", created_at=utc_now()))
        session.add(Site(owner="someone-else", url=address, kind="job_board", created_at=utc_now()))
        await session.flush()  # the same address for another owner is another row
        session.add(Site(owner=OWNER, url=_address("b"), kind="job_board", created_at=utc_now()))
        await session.commit()
    async with db.session_factory()() as session:
        session.add(Site(owner=OWNER, url=address, kind="job_board", created_at=utc_now()))
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.fixture
def door(scout_token):
    return SimpleNamespace(headers={"authorization": f"Bearer {TOKEN}"})


async def test_a_posting_pasted_into_paste_a_job_posting_has_no_most_and_the_agent_is_handed_all_of_it(
        client, door, knocks):
    await parse_runner(seen=MINUTE)
    posting = _words("posting", 120_000)
    res = await client.post("/api/v1/profile/parse", json={"text": posting, "role_id": "builders"})
    assert res.status_code == 200, res.text
    handed = await mcp_server.jobs_parse_next(door)
    assert handed["text"] == posting and len(handed["text"]) == 120_000
    assert [reason for _id, reason in knocks] == ["parse_asked"]


async def test_a_posting_pasted_into_check_my_roles_has_no_most_and_the_agent_is_handed_all_of_it(
        client, door, knocks):
    await client.put("/api/v1/profile/criteria", json={"data": PROFILE["criteria"]})
    # Check my roles reads the wall clock for Claude's peak hours (`role_check.start` passes its own `now`, which the
    # suite's off-peak clock does not replace), so the box is unticked here and the test holds at any hour of the day.
    unticked = await client.patch("/api/v1/missions/numbers", json={"values": {"avoid_peak": False}})
    assert unticked.status_code == 200, unticked.text
    await parse_runner(seen=MINUTE)
    posting = _words("posting", 120_000)
    res = await client.post("/api/v1/profile/check", json={"scope": "posting", "text": posting})
    assert res.status_code == 200, res.text
    batch = await mcp_server.jobs_match_next(door)
    assert batch["items"] == [{"id": "1", "text": posting}], "the whole posting, not the start of it"
    assert [reason for _id, reason in knocks] == ["check_asked"]
