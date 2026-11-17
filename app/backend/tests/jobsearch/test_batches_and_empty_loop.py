"""The title session that is left (synthetic data only):

- a title session handed titles batch after batch runs out of turns: a session is
  now handed one batch of `titles.PER_SESSION` and is told to stop when it asks again;
- many title sessions then started in minutes, each handed nothing: a kind whose session was handed nothing is not
  offered again until what waits for it changes, or for `session_kinds.HOLD`.

The batches were once postings for the title pass and the readers' postings; both passes are retired, so the batches
here are the roles the one-off tagging hands out (`tagging.py`).
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select

from app import db
from app.jobsearch.doors import tools
from app.jobsearch.models import Opportunity, RoleTagJob
from app.jobsearch.stages import SECOND_IMPRESSION
from app.jobsearch.work import hand_outs, session_kinds, tagging, titles
from app.planengine.budget import REPLY_CEILING_CHARS, payload_chars
from tests.support import OWNER, load_profile

NOW = datetime.now(UTC)


async def _roles(n):
    """A profile with a target role, `n` roles found before target roles and the tagging job open for them."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        session.add_all([Opportunity(
            owner=OWNER, slug=f"role-{i:04d}", company=f"Company {i}", title=f"Role Title {i:04d}",
            status=SECOND_IMPRESSION, fit=3, match_key=f"company {i}|role title {i:04d}", origin="scout",
            created_at=NOW + timedelta(seconds=i)) for i in range(n)])
        await session.flush()
        assert (await tagging.start(session, OWNER))[1] == n
        await session.commit()


async def _titles(sid):
    async with db.session_factory()() as session:
        reply = await tools.titles_next_reply(session, OWNER, sid)
        await session.commit()
        return reply


async def _next():
    async with db.session_factory()() as session:
        kind = await session_kinds.next_kind(session, OWNER)
        await session.commit()
        return kind


def _none(batch):
    return [{"id": p["id"], "role": "none"} for p in batch["postings"]]


async def _lapse_leases():
    async with db.session_factory()() as session:
        row = await session.scalar(select(RoleTagJob))
        lapsed = (NOW - timedelta(minutes=1)).isoformat()
        leases = row.counts[hand_outs.LEASES]
        row.counts = {**row.counts, hand_outs.LEASES: {**leases, "handed": {k: lapsed for k in leases["handed"]}}}
        await session.commit()


# --- titles: one batch a session ----------


async def test_250_roles_go_out_in_three_batches_of_one_session_each_and_none_fails_for_its_size(js):
    await _roles(250)
    sessions = 0
    while True:
        sid = f"titles-{sessions:03d}"
        batch = await _titles(sid)
        if not batch["postings"]:
            break
        sessions += 1
        assert len(batch["postings"]) <= titles.PER_SESSION == 100
        assert payload_chars(batch) <= titles.REPLY_CHARS < REPLY_CEILING_CHARS
        again = await _titles(sid)
        assert again["postings"] == [] and again["batch_done"] and "Stop" in again["how"], "one batch a session"
        async with db.session_factory()() as session:
            done = await tools.titles_report_reply(session, OWNER, _none(batch))
            await session.commit()
        assert done["taken"] == len(batch["postings"]), "one call answers the whole batch"
        if done["titling_done"]:
            break
        assert "Stop" in done["next"], "the session ends with its batch; the next session takes the next"
    assert sessions == 3, "250 roles at 100 a session"


async def test_a_failed_session_loses_only_its_batch_which_returns_when_its_lease_lapses(js):
    await _roles(250)
    lost = await _titles("titles-failed")  # hits its turn cap and never answers
    assert len(lost["postings"]) == 100
    other = await _titles("titles-next")
    assert len(other["postings"]) == 100 and not {p["id"] for p in lost["postings"]} & {
        p["id"] for p in other["postings"]}
    await _lapse_leases()
    back = await _titles("titles-later")
    assert {p["id"] for p in back["postings"]} == {p["id"] for p in lost["postings"]}, "the lost batch, first in line"


# --- the backstop ----------


def _door(tmp_path, monkeypatch, sid):
    from app.jobsearch.doors import scout as mcp_server

    token = "js_test_token_1234567890"
    path = tmp_path / "agent-token"
    path.write_text(token + "\n", encoding="utf-8")
    monkeypatch.setattr(mcp_server.settings, "agent_token_file", str(path))
    monkeypatch.setattr(mcp_server.settings, "owner", OWNER)
    return SimpleNamespace(headers={"authorization": f"Bearer {token}", "x-session-id": sid})


async def test_a_kind_handed_nothing_is_not_offered_again_until_what_waits_changes_or_the_hold_lapses(
        js, tmp_path, monkeypatch):
    from app.jobsearch.doors import scout as mcp_server

    await _roles(3)
    assert await _next() == session_kinds.TITLES

    async def nothing(session, owner, session_id=None, claim=None):  # a hand-out that gives nothing while roles wait
        return {"ok": True, "postings": [], "more": 0, "how": "Stop."}

    monkeypatch.setattr(tools, "titles_next_reply", nothing)
    empty = await mcp_server.jobs_titles_next(_door(tmp_path, monkeypatch, "titles-empty"))
    assert empty["postings"] == []
    assert await _next() is None, "held: the next session finds nothing too"
    async with db.session_factory()() as session:  # the same hold, made ten minutes ago, has lapsed
        long_ago = datetime.now(UTC) - session_kinds.HOLD
        await session_kinds.saw(session, OWNER, session_kinds.TITLES, empty=True, sid="titles-empty", now=long_ago)
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.TITLES, "about ten minutes at most"
    await mcp_server.jobs_titles_next(_door(tmp_path, monkeypatch, "titles-empty-2"))
    async with db.session_factory()() as session:  # a new role changes what waits: the hold lifts
        session.add(Opportunity(
            owner=OWNER, slug="role-new", company="Company New", title="Role Title New", status=SECOND_IMPRESSION,
            fit=3, match_key="company new|role title new", origin="scout", created_at=NOW))
        await session.commit()
    assert await _next() == session_kinds.TITLES


async def test_a_session_that_worked_and_then_finds_nothing_left_holds_nothing(js):
    await _roles(2)
    async with db.session_factory()() as session:
        await session_kinds.saw(session, OWNER, session_kinds.TITLES, empty=False, sid="titles-worked")
        await session_kinds.saw(session, OWNER, session_kinds.TITLES, empty=True, sid="titles-worked")
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.TITLES, "nothing is held"
        await session_kinds.saw(session, OWNER, session_kinds.TITLES, empty=True, sid="titles-new")
        assert await session_kinds.next_kind(session, OWNER) is None, "a session handed nothing yet holds it"
