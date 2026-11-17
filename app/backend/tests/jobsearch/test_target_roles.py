"""Target roles (docs/PRD.md 30.5): a role records which of the owner's target roles it matches; a role the scout
reports may name it, and the role made from a posting that has one keeps it; the roles found before target roles are
tagged once by a small title session the owner starts; each role lists its recent finds. The converter and the
criteria's own checks are test_profile.py. Synthetic data only."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch import stages
from app.jobsearch.doors import tools
from app.jobsearch.employers import core as employers
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.missions import core as missions
from app.jobsearch.missions import search_run as run
from app.jobsearch.models import JobPosting, Mission, Opportunity, RoleTagJob, SearchRun
from app.jobsearch.work import hand_outs, housekeeping, session_kinds, tagging, titles
from app.jobsearch.work import hours as night
from app.settings import settings
from tests.support import EXAMPLE_SYSTEM, OWNER, PROFILE, bare, load_profile

NOW = datetime.now(UTC)
BUILDERS = PROFILE["criteria"]["target_roles"][0]
OPS = {"id": "ops", "name": "Operations", "titles": ["Operations Manager"],
       "description": "Keeps the organization running: facilities, vendors and the budget."}
TWO_ROLES = {**PROFILE["criteria"], "target_roles": [BUILDERS, OPS]}


async def _postings(session, n=3, route="roles", start=1, **more):
    employer = await employers.find_or_make(session, OWNER, "Fabrikam")
    rows = []
    for i in range(start, start + n):
        title = f"Title Role {i}"
        rows.append(JobPosting(
            owner=OWNER, employer_id=employer.id, ats=EXAMPLE_SYSTEM, ext_id=f"T-{i}", company=employer.name,
            title=title,
            match_key=match_key(employer.name, title), location="Remote", work_mode="remote",
            url=f"https://jobs.example.test/T-{i}", last_seen=NOW, filtered_at=NOW, links=[], route=route,
            route_why="unsure", fit_prob=0.4 + i / 100, first_seen=NOW + timedelta(seconds=i),
            text=f"The start of the posting for {title}. " + "More words. " * 40, **more))
    session.add_all(rows)
    await session.flush()
    return rows


async def _roles(n=3, status=stages.SECOND_IMPRESSION, start=0, **fields):
    """Roles that were found before target roles (or with the fields given)."""
    async with db.session_factory()() as session:
        rows = [Opportunity(
            owner=OWNER, slug=f"role-{i}", company=f"Company {i}", title=f"Role Title {i}", status=status, fit=3,
            match_key=f"company {i}|role title {i}", origin="scout", created_at=NOW + timedelta(seconds=i), **fields)
            for i in range(start, start + n)]
        session.add_all(rows)
        await session.commit()
        return [r.id for r in rows]


async def _tagged() -> dict[str, str | None]:
    async with db.session_factory()() as session:
        return {r.title: r.target_role for r in (await session.scalars(select(Opportunity))).all()}


# --- a reported role names its target role ------------------------------------------------------------------------


async def test_a_reported_role_may_name_its_target_role_and_a_posting_that_has_one_gives_it_to_the_role(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=TWO_ROLES)
        for posting, role in zip(await _postings(session, 3), ("ops", "builders", "builders"), strict=True):
            posting.target_role = role
        started = await run.start_round(session, OWNER, started_by="manual")
        await session.commit()
        run_id = started.id

    def read(title, **more):
        return {"company": "Fabrikam", "title": title, "url": f"https://jobs.example.test/{title}",
                "fit_evidence": "none",
                "fit_breakdown": {"asks": [{"id": "promotion", "answer": "met", "evidence": "x"}]}, **more}

    async with db.session_factory()() as session:
        row = await session.get(SearchRun, run_id)
        with pytest.raises(run.RunError, match="target_role 'interns' is not one of the owner's target roles"):
            await run.stage_report(session, row, [read("Title Role 1", target_role="interns")], False)
        await run.stage_report(session, row, [
            read("Title Role 1"), read("Title Role 2", target_role="ops"), read("Title Role 3", target_role="none")],
            False)
        await run.stage_report(session, row, [], done=True)
        await session.commit()
    assert await _tagged() == {"Title Role 1": "ops", "Title Role 2": "ops", "Title Role 3": "builders"}, (
        "the report's word is the role's target role, else the posting's")


# --- tagging the roles found before target roles -------------------------------------------------------------------


async def test_the_owner_starts_the_tagging_only_with_a_target_role_and_roles_with_none_and_it_wakes_the_runner(
        client, knocks):
    no_profile = await client.post("/api/v1/profile/roles/tag")
    assert no_profile.status_code == 422 and "add a target role first" in no_profile.json()["detail"]
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=TWO_ROLES)
        await session.commit()
    nothing = await client.post("/api/v1/profile/roles/tag")
    assert nothing.json() == {"open": False, "count": 0} and knocks == []
    await _roles(3)
    await _roles(2, start=10, status=stages.GATED_OUT)  # a gate stopped these: not a find
    await _roles(1, start=20, target_role="builders")  # already has one
    started = await client.post("/api/v1/profile/roles/tag")
    assert started.status_code == 200 and started.json() == {"open": True, "count": 3}
    async with db.session_factory()() as session:
        [job] = (await session.scalars(select(RoleTagJob))).all()
    assert job.status == "open" and job.counts == {"total": 3} and knocks == [(str(job.id), "tag_roles")]
    again = await client.post("/api/v1/profile/roles/tag")
    assert again.json() == {"open": True, "count": 3}
    async with db.session_factory()() as session:
        assert len((await session.scalars(select(RoleTagJob))).all()) == 1, "one job at a time"


async def test_the_runner_gets_the_title_session_for_the_tagging_which_hands_roles_and_closes_when_none_remain(
        client, knocks, monkeypatch):
    monkeypatch.setattr(hand_outs, "ITEM_LEASE_SECONDS", -1)  # this test hands a batch again at once: no lease holds
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=TWO_ROLES)
        await session.commit()
    ids = await _roles(4)
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) is None, "no job yet"
    await client.post("/api/v1/profile/roles/tag")
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.TITLES == "titles"
        batch = await tools.titles_next_reply(session, OWNER)  # with no search open
        await session.commit()
    assert [p["title"] for p in batch["postings"]] == [f"Role Title {i}" for i in (3, 2, 1, 0)], "newest first"
    first = batch["postings"][0]
    assert first == {"id": str(ids[3])[:8], "kind": "role", "title": "Role Title 3", "company": "Company 3"}
    assert "examples" not in batch and batch["more"] == 0
    assert [r["id"] for r in batch["what_to_look_for"]["target_roles"]] == ["builders", "ops"]
    assert "kind `role`" in batch["how"] and "{id, role" in batch["how"]
    by_title = {p["title"]: p["id"] for p in batch["postings"]}
    async with db.session_factory()() as session:
        reply = await tools.titles_report_reply(session, OWNER, [
            {"id": by_title["Role Title 3"], "role": "ops"},
            {"id": by_title["Role Title 2"], "role": "Builders"},
            {"id": by_title["Role Title 1"], "role": "none"},
            {"id": by_title["Role Title 0"], "role": "interns"},
            {"id": "deadbeef", "role": "ops"}])
        await session.commit()
    assert reply["taken"] == 3 and reply["tagged"] == 2 and len(reply["problems"]) == 2
    assert any("'interns' is not one of the owner's target roles" in p for p in reply["problems"])
    assert any("deadbeef" in p and "not a posting or role" in p for p in reply["problems"])
    assert reply["titling_done"] is False and reply["more_to_tag"] == 1 and "send them again" in reply["next"]
    assert await _tagged() == {"Role Title 3": "ops", "Role Title 2": "builders", "Role Title 1": None,
                               "Role Title 0": None}
    async with db.session_factory()() as session:
        again = await tools.titles_next_reply(session, OWNER)
        assert [p["title"] for p in again["postings"]] == ["Role Title 0"], (
            "the role answered none is not handed again; the one with a refused answer is")
        done = await tools.titles_report_reply(session, OWNER, [{"id": again["postings"][0]["id"], "role": "ops"}])
        await session.commit()
        assert done["titling_done"] is True and done["more_to_tag"] == 0 and "finished" in done["next"]
        [job] = (await session.scalars(select(RoleTagJob))).all()
        assert job.status == "done" and job.ended_at is not None and job.counts["tagged"] == 3
        assert await session_kinds.next_kind(session, OWNER) is None, "the job is closed: nothing is left"
        with pytest.raises(run.RunError, match="no search is open"):
            await tools.titles_next_reply(session, OWNER)
    assert (await _tagged())["Role Title 0"] == "ops"
    # A role answered none is Haiku's answer, kept: starting again finds nothing to ask.
    async with db.session_factory()() as session:
        job, count = await tagging.start(session, OWNER)
    assert job is None and count == 0


async def test_a_role_handed_twice_and_never_answered_is_left_and_the_job_closes(client, knocks, monkeypatch):
    monkeypatch.setattr(hand_outs, "ITEM_LEASE_SECONDS", -1)  # this test hands a batch again at once: no lease holds
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=TWO_ROLES)
        await session.commit()
    await _roles(2)
    await client.post("/api/v1/profile/roles/tag")
    async with db.session_factory()() as session:
        for _ in range(tagging.HANDED_AT_MOST):
            assert len((await tools.titles_next_reply(session, OWNER))["postings"]) == 2
            await session.commit()
        await housekeeping.settle(session, OWNER)  # closing a job with nothing left is the app's own pass
        assert await session_kinds.next_kind(session, OWNER) is None, "nothing more to hand: the job closes"
        await session.commit()
        [job] = (await session.scalars(select(RoleTagJob))).all()
        assert job.status == "done"
    assert set((await _tagged()).values()) == {None}


async def test_two_sessions_asking_at_once_are_handed_disjoint_batches_of_roles_and_a_lapsed_lease_hands_one_on(
        client, knocks, monkeypatch):
    """An item handed to a session still working on it was handed to another at once, so titles were judged twice. A
    batch is leased to the session it was handed to."""
    import asyncio

    monkeypatch.setattr(titles, "PER_SESSION", 2)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=TWO_ROLES)
        await roster.ensure_roster(session, OWNER)  # the roster is made once, before sessions race to make it
        await session.commit()
    await _roles(6)
    await client.post("/api/v1/profile/roles/tag")

    async def ask():
        async with db.session_factory()() as session:
            reply = await tools.titles_next_reply(session, OWNER)
            await session.commit()
            return {p["id"] for p in reply["postings"]}

    one, two, three = await asyncio.gather(ask(), ask(), ask())
    assert len(one) == len(two) == len(three) == 2
    assert not (one & two) and not (one & three) and not (two & three), "no role went to two sessions"
    assert not await ask(), "all six are leased, so a fourth session is handed none"
    async with db.session_factory()() as session:  # leases that lapse put their batches back in the line
        [job] = (await session.scalars(select(RoleTagJob))).all()
        lapsed = (NOW - timedelta(minutes=1)).isoformat()
        job.counts = {**job.counts, hand_outs.LEASES: {"handed": {
            k: lapsed for k in job.counts[hand_outs.LEASES]["handed"]}}}
        await session.commit()
    assert len(await ask()) == 2


async def test_a_title_session_is_told_to_stop_when_nothing_waits_or_the_owner_paused_the_scout(client, knocks):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=TWO_ROLES)
        with pytest.raises(run.RunError, match="no search is open"):
            await tools.titles_next_reply(session, OWNER)
        await session.commit()
    await _roles(2)
    await client.post("/api/v1/profile/roles/tag")
    async with db.session_factory()() as session:
        (await roster.ensure_roster(session, OWNER)).paused = True
        await session.flush()
        paused = await tools.titles_next_reply(session, OWNER)
        assert paused["postings"] == [] and "paused" in paused["how"]
        assert await session_kinds.next_kind(session, OWNER) is None


async def test_an_open_search_keeps_the_tagging_waiting_until_it_ends(client, knocks):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=TWO_ROLES)
        started = await run.start_round(session, OWNER, started_by="manual")
        await session.commit()
        run_id = started.id
    await _roles(2)
    await client.post("/api/v1/profile/roles/tag")
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.SEARCH, (
            "the search first: its stuck check is timed")
        await run.stage_report(session, await session.get(SearchRun, run_id), [], done=True)
        await session.commit()
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.TITLES, (
            "the search is over: the tagging's turn")


async def test_the_tagging_holds_in_peak_hours_and_while_the_scout_is_paused_and_touches_no_mission(
        client, knocks, monkeypatch):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=TWO_ROLES, settings={})
        await missions.ensure(session, OWNER)
        rows = (await session.scalars(select(Mission).where(Mission.owner == OWNER))).all()
        await missions.pause(session, rows[0], by="Tester")
        await session.commit()
        before = {m.key: (m.state, m.paused_until) for m in rows}
    assert any(state == "paused" for state, _until in before.values())
    await _roles(2)
    started = await client.post("/api/v1/profile/roles/tag")
    assert started.json() == {"open": True, "count": 2}, "the owner can start it any time"
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.TITLES
        with monkeypatch.context() as m:
            m.setattr(night, "in_peak", lambda s, now=None: bool(s.avoid_peak))
            assert await session_kinds.next_kind(session, OWNER) is None, "Claude's peak hours: nothing new starts"
        conn = await roster.ensure_roster(session, OWNER)
        conn.paused = True
        await session.flush()
        assert await session_kinds.next_kind(session, OWNER) is None, "the owner paused the scout"
        conn.paused = False
        await session.commit()
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.TITLES, "and it is still open afterwards"
        after = {m.key: (m.state, m.paused_until) for m in (await session.scalars(
            select(Mission).where(Mission.owner == OWNER))).all()}
    assert after == before, "no mission was started, paused or resumed"


async def test_the_tagging_closes_when_the_owner_has_no_target_role_left(client, knocks):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=TWO_ROLES)
        await session.commit()
    await _roles(2)
    await client.post("/api/v1/profile/roles/tag")
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria={"company": "A small agency."})
        await housekeeping.settle(session, OWNER)  # closing the job is the app's own pass
        assert await session_kinds.next_kind(session, OWNER) is None
        await session.commit()
        [job] = (await session.scalars(select(RoleTagJob))).all()
    assert job.status == "done"


# --- the trend of a role's finds ----------------------------------------------------------------------------------


async def _found_on(moments, *, status=stages.SECOND_IMPRESSION, target_role="builders", start=0):
    """One role for each moment in `moments`, made at that moment (the day it counts on)."""
    async with db.session_factory()() as session:
        session.add_all([Opportunity(
            owner=OWNER, slug=f"trend-{start + i}", company=f"Company {start + i}", title=f"Trend Title {start + i}",
            status=status, fit=3, match_key=f"company {start + i}|trend title {start + i}", origin="scout",
            created_at=at, target_role=target_role) for i, at in enumerate(moments)])
        await session.commit()


async def test_a_target_roles_trend_has_one_entry_for_each_of_the_last_days_with_none_as_zero(client, monkeypatch):
    monkeypatch.setattr(settings, "timezone", "UTC")
    noon = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    today = datetime.now(UTC).date()
    await _found_on([noon, noon, noon - timedelta(days=1), noon - timedelta(days=9)])
    page = (await client.get("/api/v1/profile/roles/builders/trend")).json()
    assert set(page) == {"days", "total"} and len(page["days"]) == 30, "thirty days unless asked for others"
    assert all(set(d) == {"date", "found"} for d in page["days"])
    assert [d["date"] for d in page["days"]] == [(today - timedelta(days=29 - i)).isoformat() for i in range(30)], (
        "oldest first, today last")
    by_date = {d["date"]: d["found"] for d in page["days"]}
    assert by_date[today.isoformat()] == 2 and by_date[(today - timedelta(days=1)).isoformat()] == 1
    assert by_date[(today - timedelta(days=9)).isoformat()] == 1
    assert sum(by_date.values()) == 4 == page["total"], "every other day is a zero, not missing"
    week = (await client.get("/api/v1/profile/roles/builders/trend", params={"days": 7})).json()
    assert [d["found"] for d in week["days"]] == [0, 0, 0, 0, 0, 1, 2] and week["total"] == 3, (
        "the day nine days back is outside a week")
    one = (await client.get("/api/v1/profile/roles/builders/trend", params={"days": 1})).json()
    assert one["days"] == [{"date": today.isoformat(), "found": 2}] and one["total"] == 2


async def test_a_trend_counts_only_that_roles_roles_that_reached_found(client, monkeypatch):
    monkeypatch.setattr(settings, "timezone", "UTC")
    noon = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    await _found_on([noon, noon], status=stages.SECOND_IMPRESSION, start=0)
    await _found_on([noon], status=stages.APPLIED, start=10)
    await _found_on([noon, noon, noon], status=stages.GATED_OUT, start=20)
    await _found_on([noon, noon], target_role="ops", start=30)
    await _found_on([noon], target_role=None, start=40)
    builders = (await client.get("/api/v1/profile/roles/builders/trend", params={"days": 3})).json()
    assert [d["found"] for d in builders["days"]] == [0, 0, 3] and builders["total"] == 3, (
        "the gated ones, the other role's and the untagged are not counted")
    ops = (await client.get("/api/v1/profile/roles/ops/trend", params={"days": 3})).json()
    assert [d["found"] for d in ops["days"]] == [0, 0, 2] and ops["total"] == 2
    unknown = await client.get("/api/v1/profile/roles/interns/trend", params={"days": 3})
    assert unknown.status_code == 200, "not a 404"
    assert [d["found"] for d in unknown.json()["days"]] == [0, 0, 0] and unknown.json()["total"] == 0


async def _roles_found(rows, *, target_role="builders"):
    """Roles with their own found dates, as the tracker keeps them: `(slug, origin, found, created_at, status)`."""
    async with db.session_factory()() as session:
        session.add_all([Opportunity(
            owner=OWNER, slug=slug, company=f"Company {slug}", title=f"Title {slug}", status=status, fit=3,
            match_key=f"company {slug}|title {slug}", origin=origin, found=found, created_at=made,
            target_role=target_role) for slug, origin, found, made, status in rows])
        await session.commit()


async def test_a_trend_counts_imported_roles_on_the_day_found_not_the_day_imported(client, monkeypatch):
    """docs/PRD.md 31.8: an imported role carries the tracker's found date; every imported row was made on the import
    day, which says nothing about when the role was found."""
    monkeypatch.setattr(settings, "timezone", "UTC")
    today = datetime.now(UTC).date()
    imported_at = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)

    def ago(n):
        return today - timedelta(days=n)

    await _roles_found([
        ("t1", "tracker", ago(3), imported_at, stages.APPLIED),
        ("t2", "tracker", ago(3), imported_at, stages.REJECTED),
        ("t3", "tracker", ago(12), imported_at, stages.SECOND_IMPRESSION),
        ("t4", "tracker", ago(45), imported_at, stages.APPLIED),       # found before the window
        ("t5", "tracker", None, imported_at, stages.SECOND_IMPRESSION),  # no found date: no day to count it on
        ("t6", "tracker", ago(2), imported_at, stages.GATED_OUT),      # gated: never reached Found
        ("s1", "scout", today, imported_at, stages.SECOND_IMPRESSION),             # found by a search, today
        ("s2", "scout", None, imported_at - timedelta(days=1), stages.SECOND_IMPRESSION),  # no date: its row's day
        ("s3", "scout", ago(1), imported_at - timedelta(days=9), stages.SECOND_IMPRESSION),  # found date wins
    ])
    page = (await client.get("/api/v1/profile/roles/builders/trend")).json()
    by_date = {d["date"]: d["found"] for d in page["days"]}
    assert by_date[ago(3).isoformat()] == 2 and by_date[ago(12).isoformat()] == 1
    assert by_date[today.isoformat()] == 1 and by_date[ago(1).isoformat()] == 2
    assert by_date[ago(9).isoformat()] == 0, "the scout role's found date wins over the day its row was made"
    assert sum(by_date.values()) == page["total"] == 6, "t4 is before the window, t5 has no day, t6 is gated"
    assert by_date[imported_at.date().isoformat()] == 1, "the import day holds only what was found that day"


async def test_a_trend_days_are_the_owners_days_not_utc_days(js, monkeypatch):
    monkeypatch.setattr(settings, "timezone", "America/New_York")
    now = datetime(2026, 10, 9, 3, 0, tzinfo=UTC)  # 11 at night on Oct 8 in New York, already Oct 9 in UTC
    await _found_on([
        datetime(2026, 10, 9, 2, 30, tzinfo=UTC),    # 10:30 pm on Oct 8 there: Oct 8
        datetime(2026, 10, 6, 4, 0, tzinfo=UTC),     # midnight starting Oct 6 there: the first day
        datetime(2026, 10, 6, 3, 59, tzinfo=UTC),    # a minute before it: Oct 5, outside three days
        datetime(2026, 10, 9, 4, 0, tzinfo=UTC),     # midnight starting Oct 9 there: after today, not counted
    ])
    async with db.session_factory()() as session:
        got = await tagging.trend(session, OWNER, "builders", 3, now=now)
    assert got == {"days": [{"date": "2026-10-06", "found": 1}, {"date": "2026-10-07", "found": 0},
                            {"date": "2026-10-08", "found": 1}], "total": 2}
    monkeypatch.setattr(settings, "timezone", "UTC")
    async with db.session_factory()() as session:
        got = await tagging.trend(session, OWNER, "builders", 3, now=now)
    assert [d["date"] for d in got["days"]] == ["2026-10-07", "2026-10-08", "2026-10-09"], "today is Oct 9 in UTC"
    assert [d["found"] for d in got["days"]] == [0, 0, 2] and got["total"] == 2


async def test_the_trend_covers_one_to_ninety_days(client):
    for days in (0, 91, -1):
        assert (await client.get("/api/v1/profile/roles/builders/trend", params={"days": days})).status_code == 422
    for days in (1, 90):
        page = await client.get("/api/v1/profile/roles/builders/trend", params={"days": days})
        assert page.status_code == 200 and len(page.json()["days"]) == days


# --- the profile over HTTP -----------------------------------------------------------------------------------------


async def test_the_page_saves_target_roles(client):
    saved = await client.put("/api/v1/profile/criteria", json={"data": TWO_ROLES})
    assert saved.status_code == 200 and bare(saved.json()) == {"saved": True, "version": 1}
    body = (await client.get("/api/v1/profile")).json()
    assert body["is_set"] is True and [r["id"] for r in body["criteria"]["target_roles"]] == ["builders", "ops"]
    assert not {"title_groups", "the_work"} & set(body["criteria"])
    repeated = await client.put("/api/v1/profile/criteria", json={"data": {"target_roles": [{"id": "a"}, {"id": "a"}]}})
    assert repeated.status_code == 422 and "unique" in repeated.json()["detail"]
