"""A whole search round: the tracker's roles are known, the scout reports, the process de-duplicates, screens and
stores, and the run is closed with its summary. Also: what refuses a search, what a failure leaves behind. Synthetic
data only."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from process_engine.models import CANCELLED, COMPLETED, ProcHistory, ProcInstance
from sqlalchemy import select

from app import db
from app.jobsearch import actions, process
from app.jobsearch.imports import tracker
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.missions import search_run as run
from app.jobsearch.models import JobPosting, Opportunity, SearchRun, StageMove
from app.jobsearch.stages import GATED_OUT
from app.jobsearch.stages import LANDING_ZONE as LANDED
from tests.builders import run_round, stored_by_title
from tests.support import (
    EXAMPLE_SYSTEM,
    OWNER,
    gate_says,
    load_profile,
    reported,
    tracker_row,
    tracker_workbook,
)


async def _load_profile(session, **parts):
    await load_profile(session, OWNER, **parts)


async def test_a_round_dedupes_against_the_tracker_screens_and_stores_every_new_role(js):
    roles = [
        reported("Northwind Bank", "Director of Programs"),  # in the tracker, written in other case below
        reported("NORTHWIND BANK", "director of programs", fit=9, board="Boardthree"),  # the same role on another board
        reported("Fabrikam", "Head of Delivery", fit=6, location="Oakfield, ST", work_mode="onsite",
                 comp="$112,500 - $130,000", posted="2026-10-01"),
        reported("Fabrikam", "Head of Delivery", fit=8, work_mode="onsite", location="Oakfield, ST"),  # repeated
        reported("Globex", "VP Operations", fit=7),  # excluded employer
        reported("Contoso", "Director", fit=5, location="Denver, CO", work_mode="onsite"),  # place
        reported("Initech", "Program Lead", fit=5, comp_min=50_000, comp_max=70_000),  # pay under the $95,000 floor
        reported("Umbrella", "Manager", fit=0),  # a score of nothing: no gate reads it, so it is kept
        reported("Hooli", "Chief of Staff", fit=4, location="", work_mode="unknown"),  # place not stated: held, kept
    ]
    run_id = await run_round(roles, known=[("Northwind Bank", "Director of Programs")], done_in_batches=3)
    stored = await stored_by_title(run_id)

    assert set(stored) == {"Head of Delivery", "VP Operations", "Director", "Program Lead", "Manager", "Chief of Staff"}
    assert "Director of Programs" not in stored and "director of programs" not in stored, "already in the tracker"
    delivery = stored["Head of Delivery"]
    # A kept role lands in the Landing Zone: the pipeline reads and ranks it from there.
    assert (delivery.status, delivery.fit, delivery.origin) == (LANDED, 8, "scout"), "the best-fit copy is kept"
    assert delivery.match_key == "fabrikam|head of delivery" and delivery.source_run == run_id
    assert stored["Chief of Staff"].status == LANDED and "place not stated" in stored["Chief of Staff"].notes
    manager = stored["Manager"]
    assert (manager.status, manager.fit) == (LANDED, 0), "a low score stops no role: the fit only ranks"
    assert (manager.screened_out_reason, manager.screened_out_by) == (None, None)
    reasons = {t: (r.status, r.screened_out_reason) for t, r in stored.items() if r.status == GATED_OUT}
    assert reasons["VP Operations"][1] == "excluded employer"
    assert reasons["Director"][1].startswith(gate_says()["location"].rstrip(".")), "the gate's own sentence"
    assert reasons["Director"][1].endswith("(posted: Denver, CO).")
    assert reasons["Program Lead"][1] == (
        "Pay must be at least $95,000 a year, on the estimated ask (the estimated ask is $60,000).")
    assert set(reasons) == {"VP Operations", "Director", "Program Lead"}

    async with db.session_factory()() as session:
        # The roles are stored by a Core insert, which the session hook that writes a new role's stage move never
        # sees; the move is written with it, so the Pipeline can say how many came into Found
        moves = (await session.scalars(
            select(StageMove).where(StageMove.role_id.in_([r.id for r in stored.values()])))).all()
        assert sorted((m.from_status or "", m.to_status) for m in moves) == sorted(
            ("", r.status) for r in stored.values())
        row = await session.get(SearchRun, run_id)
        assert row.status == "done" and row.ended_at is not None
        assert row.counts == {"reported": 9, "kept": 3, "screened_out": 3, "duplicates": 3}
        assert row.summary.startswith("Reviewed 9 roles: 3 kept, 3 gated out, 3 already known.")
        assert "Head of Delivery at Fabrikam (fit 100%)" in row.summary
        instance = await session.get(ProcInstance, row.instance_id)
        assert instance.status == COMPLETED and instance.mode == "live", "driven by the engine, no shadow"
        kinds = [h.kind for h in (await session.scalars(select(ProcHistory).where(
            ProcHistory.instance_id == instance.id).order_by(ProcHistory.seq))).all()]
        assert kinds[0] == "start" and kinds[-1] == "end"
        children = (await session.scalars(select(ProcInstance).where(ProcInstance.parent_id == instance.id))).all()
        assert len(children) == 6 and {c.process_key for c in children} == {process.SCREEN}


async def test_roles_a_gate_dropped_are_known_to_the_next_search_and_not_screened_again(js):
    first = await run_round([reported("Globex", "VP Operations")])
    assert (await stored_by_title(first))["VP Operations"].status == GATED_OUT
    second = await run_round([reported("Globex", "VP Operations"), reported("Fabrikam", "Lead")])
    assert set(await stored_by_title(second)) == {"Lead"}
    async with db.session_factory()() as session:
        assert (await session.get(SearchRun, second)).counts["duplicates"] == 1


async def test_a_role_the_owner_has_written_another_way_is_set_aside_and_a_different_job_is_not(js):
    """Found when comparing two nights' searches: a role the owner had applied to came back as Found, the tracker
    having its title with a comma and a job board's link, and the scout's copy having it with a dash and the employer's
    own link. The match key is exact, so they were two roles."""
    async with db.session_factory()() as session:
        await tracker.import_tracker(session, OWNER, tracker_workbook([
            tracker_row("Northwind Bank", "Director, Programs", status="applied",
                        url="https://boards.example.test/jobs/view/3912345678/"),
            tracker_row("Fabrikam", "Analyst II", status="rejected"),
        ]))
        await session.commit()
    run_id = await run_round([
        reported("Northwind Bank", "Director — Programs", url="https://careers.example.test/northwind/director"),
        reported("Northwind Bank", "Senior Director, Programs"),  # another job at the same employer
        reported("Fabrikam", "Analyst 2"),  # the same title, with a numeral
        reported("Contoso", "Head of Delivery", fit=6),
        reported("Contoso", "Head, Delivery", fit=8),  # the same role as the one before: the better one is kept
    ])
    async with db.session_factory()() as session:
        roles = {r.title: r for r in (await session.scalars(select(Opportunity))).all()}
        assert set(roles) == {"Director, Programs", "Analyst II", "Senior Director, Programs", "Head, Delivery"}
        applied = roles["Director, Programs"]
        assert (applied.status, applied.origin, applied.source_run) == ("applied", "tracker", None), "left as it was"
        assert (roles["Head, Delivery"].fit, roles["Head, Delivery"].origin) == (8, "scout")
        assert roles["Senior Director, Programs"].source_run == run_id
        row = await session.get(SearchRun, run_id)
        assert row.counts == {"reported": 5, "kept": 2, "screened_out": 0, "duplicates": 3}
        assert row.summary.startswith("Reviewed 5 roles: 2 kept, 0 gated out, 3 already known.")


async def test_a_role_reported_with_a_known_roles_link_is_set_aside_only_for_the_same_employer(js):
    """8 live tracker rows carry another employer's posting link, so a new posting of that link at the other employer
    was taken for the older row's job and never read. The link is the same posting only when the employer folds the
    same."""
    link = "https://boards.example.test/jobs/view/3912345678/"
    async with db.session_factory()() as session:
        await tracker.import_tracker(session, OWNER, tracker_workbook([
            tracker_row("Northwind Bank", "Director, Programs", status="applied", url=link)]))
        await session.commit()
    run_id = await run_round([
        reported("Northwind Bank, Inc.", "Head of Program Delivery", url=f"{link}?utm_source=x"),  # the same posting
        reported("Contoso", "Head of Program Delivery", url=link.replace("view/", "view/director-")),
    ])
    stored = await stored_by_title(run_id)
    assert {(r.company, r.title) for r in stored.values()} == {("Contoso", "Head of Program Delivery")}
    async with db.session_factory()() as session:
        counts = (await session.get(SearchRun, run_id)).counts
    assert counts == {"reported": 2, "kept": 1, "screened_out": 0, "duplicates": 1}


async def test_a_role_set_aside_as_known_makes_the_posting_it_was_read_from_the_roles_own(js):
    """A reader reports a posting under its own company and title. When that is a role the owner has in other words,
    the role is set aside and the posting becomes the role's, as the nightly filter does for a posting it meets after
    the role: left alone it would be handed to a reader again every night."""
    seen = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)
    title = "Director — Programs"
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await tracker.import_tracker(session, OWNER, tracker_workbook([
            tracker_row("Northwind Bank", "Director, Programs", status="applied")]))
        session.add_all([JobPosting(
            owner=OWNER, employer_id=uuid.uuid4(), ats=EXAMPLE_SYSTEM, ext_id=ext_id, company="Northwind Bank",
            title=name, match_key=match_key("Northwind Bank", name), first_seen=seen, last_seen=seen, links=[],
            filtered_at=seen) for ext_id, name in (("gh-1", title), ("gh-2", "Senior Director, Programs"))])
        started = await run.start_round(session, OWNER, started_by="manual")
        await session.commit()
    async with db.session_factory()() as session:
        staged = await session.get(SearchRun, started.id)
        await run.stage_report(session, staged, [reported("Northwind Bank", title)], done=False)
        data = {"owner": OWNER, "run": str(staged.id)}
        taken = await actions.take_reported(session, None, data)
        assert await actions.dedupe(session, None, {**data, "taken": taken}) == {"roles": [], "duplicates": 1}
        role = await session.scalar(select(Opportunity))
        postings = {p.title: p for p in (await session.scalars(select(JobPosting))).all()}
        assert postings[title].opportunity_id == role.id
        assert postings["Senior Director, Programs"].opportunity_id is None, "a posting of another job is not touched"


async def test_a_posting_whose_stored_key_no_longer_matches_its_company_still_becomes_the_known_roles_own(js):
    """A posting kept the key of the employer's old name ("Northwind Bank, Inc."), so the role its
    reader reported under the present name was set aside as known but the posting was never linked, stayed a reader's
    candidate, and was handed again by every search of the night."""
    seen = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await tracker.import_tracker(session, OWNER, tracker_workbook([
            tracker_row("Northwind Bank", "Director, Programs", status="applied")]))
        session.add(JobPosting(
            owner=OWNER, employer_id=uuid.uuid4(), ats=EXAMPLE_SYSTEM, ext_id="gh-1", company="Northwind Bank",
            title="Director, Programs", match_key=match_key("Northwind Bank, Inc.", "Director, Programs"),
            first_seen=seen, last_seen=seen, links=[], filtered_at=seen))
        started = await run.start_round(session, OWNER, started_by="manual")
        await session.commit()
    async with db.session_factory()() as session:
        staged = await session.get(SearchRun, started.id)
        await run.stage_report(session, staged, [reported("Northwind Bank", "Director, Programs")], done=False)
        data = {"owner": OWNER, "run": str(staged.id)}
        taken = await actions.take_reported(session, None, data)
        assert await actions.dedupe(session, None, {**data, "taken": taken}) == {"roles": [], "duplicates": 1}
        role = await session.scalar(select(Opportunity))
        posting = await session.scalar(select(JobPosting).where(JobPosting.ext_id == "gh-1"))
        assert posting.opportunity_id == role.id


async def test_a_search_that_reports_nothing_still_closes_with_a_plain_summary(js):
    run_id = await run_round([])
    async with db.session_factory()() as session:
        row = await session.get(SearchRun, run_id)
        assert (row.status, row.summary) == ("done", "The scout reported no roles.")
        history = (await session.scalars(select(ProcHistory).where(
            ProcHistory.instance_id == row.instance_id, ProcHistory.kind == "decision")
            .order_by(ProcHistory.seq))).all()
        assert [h.flow_id for h in history] == ["F_none", "F_no_research"]


async def test_one_search_at_a_time_and_none_before_the_profile_is_loaded(js):
    async with db.session_factory()() as session:
        with pytest.raises(run.RunError, match="what to look for first"):
            await run.start_round(session, OWNER)
        await _load_profile(session)
        await run.start_round(session, OWNER)
        await session.commit()
    async with db.session_factory()() as session:
        with pytest.raises(run.RunError, match="already running"):
            await run.start_round(session, OWNER)


async def test_a_batch_with_a_bad_role_is_refused_whole_and_every_problem_is_named(js):
    async with db.session_factory()() as session:
        await _load_profile(session)
        started = await run.start_round(session, OWNER)
        await session.commit()
        with pytest.raises(run.RunError) as caught:
            await run.stage_report(session, started, [
                reported("Fine Co", "Fine Role"),
                {"company": "No Link", "title": "T", "fit": 3, "fit_evidence": "x"},
                {"company": "", "title": "No company", "url": "https://x.test", "fit": 3, "fit_evidence": "x"},
                {**reported("Bad Fit", "T"), "fit": "high"},
                {**reported("No Evidence", "T"), "fit_evidence": ""},
                {**reported("Bad Mode", "T"), "work_mode": "sideways"},
                {**reported("Bad Date", "T"), "posted": "last week"},
            ], done=False)
        message = str(caught.value)
        assert message.startswith("nothing was saved")
        for needle in ("role 2", "needs the link", "role 3", "company and a title", "role 4", "fit score",
                       "role 5", "fit evidence", "role 6", "work_mode", "role 7", "posted"):
            assert needle in message
        assert started.reported == [], "not even the good role was staged"


async def test_a_round_that_fails_is_closed_whole_with_nothing_stored_and_the_next_search_can_start(js, monkeypatch):
    async def boom(session, instance, data):
        raise RuntimeError("the database hiccuped")

    original = actions.registry.get("jobs.summarize")
    monkeypatch.setitem(actions.registry._actions, "jobs.summarize", type(original)(
        original.name, boom, original.inputs, original.outputs, original.description, original.source))
    async with db.session_factory()() as session:
        await _load_profile(session)
        started = await run.start_round(session, OWNER)
        await session.commit()
        run_id = started.id
        with pytest.raises(run.RoundFailed, match="couldn't be finished"):
            await run.stage_report(session, started, [reported("Fabrikam", "Lead")], done=True)
        await session.commit()
    async with db.session_factory()() as session:
        row = await session.get(SearchRun, run_id)
        assert row.status == "failed" and "database hiccuped" in row.summary
        assert (await session.get(ProcInstance, row.instance_id)).status == CANCELLED
        assert list((await session.scalars(select(Opportunity))).all()) == [], "a failed round keeps no half-done work"
        monkeypatch.undo()
        again = await run.start_round(session, OWNER)
        assert again.status == "open"


async def test_a_search_the_scout_never_finished_is_closed_after_the_stuck_time(js):
    async with db.session_factory()() as session:
        await _load_profile(session)
        started = await run.start_round(session, OWNER)
        await session.commit()
        assert await run.settle_stuck(session, OWNER) == 0
        later = datetime.now(UTC) + timedelta(minutes=500)
        assert await run.settle_stuck(session, OWNER, now=later) == 1
        assert started.status == "failed" and "didn't finish" in started.summary
        assert await run.open_run(session, OWNER) is None


async def test_a_stuck_search_keeps_what_the_scout_reported(js):
    async with db.session_factory()() as session:
        await _load_profile(session)
        started = await run.start_round(session, OWNER)
        await run.stage_report(session, started, [reported("Fabrikam", "Head of Delivery", fit=4)], done=False)
        await session.commit()
        later = datetime.now(UTC) + timedelta(minutes=500)
        assert await run.settle_stuck(session, OWNER, now=later) == 1
        await session.commit()
        assert started.status == "done" and "what it reported was kept" in started.summary
    assert "Head of Delivery" in await stored_by_title(started.id)


async def test_a_scout_session_that_ends_unfinished_keeps_what_it_reported(js, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from app.jobsearch.main import app
    from app.settings import settings

    monkeypatch.setattr("app.jobsearch.routes.runner.token_ok", lambda header: True)
    async with db.session_factory()() as session:
        await load_profile(session, settings.owner)
        started = await run.start_round(session, settings.owner)
        await session.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/v1/agent/heartbeat", json={"session_id": "scout-abc", "state": "started"})
        async with db.session_factory()() as session:
            row = await session.get(SearchRun, started.id)
            await run.stage_report(session, row, [reported("Fabrikam", "Head of Delivery", fit=4)], done=False)
            await session.commit()
        await client.post("/api/v1/agent/heartbeat", json={"session_id": "scout-abc", "state": "ended"})
    async with db.session_factory()() as session:
        row = await session.get(SearchRun, started.id)
        assert row.status == "done" and "session ended before it said it was done" in row.summary
