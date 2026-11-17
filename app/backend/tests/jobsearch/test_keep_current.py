"""The app keeps the log and the scores current by itself (docs/PRD.md 39.14, 39.21, 39.23): a save of Hard pass or
Prioritization that changes nothing makes no new version and runs nothing, so a release leaves stops judged under an
older Hard pass (or none) and scores worked out under an older Prioritization (or none) until the owner next changes
the setting. The same re-gate and the same re-score run at every start of the app and at the start of the night's read
whenever such stops or scores exist, and do nothing once everything is current. Everything here is made up."""

import asyncio
from datetime import UTC, date, datetime

import httpx
from sqlalchemy import func, select, update

from app import db
from app.jobsearch import actions, process, stages
from app.jobsearch.employers import core as employers
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.fit import freshness, scoring
from app.jobsearch.gating import regate
from app.jobsearch.loops.startup import run_startup_fixups
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.missions import boards_night
from app.jobsearch.models import JobPosting, Opportunity
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import keep_current, prioritize
from app.models.work import TrailEvent
from app.trail import service as trail
from tests.support import EXAMPLE_SYSTEM, FIVE, GRADED_ON, OWNER, PROFILE, answered, load_profile

NOW = datetime(2026, 10, 12, 5, 30, tzinfo=UTC)
TODAY = date(2026, 10, 12)


def gates(floor: int) -> dict:
    """Remote work only, and at least `floor` a year on the posted maximum."""
    return {"location": {"remote": {"on": True}}, "compensation": {"amount": floor, "against": "maximum"}}


def posting(employer, ext: str, top: int | None, **more) -> JobPosting:
    """A posting that is remote and pays up to `top`, as a board listed it; `more` sets the rest."""
    return JobPosting(**{
        "owner": OWNER, "employer_id": employer.id, "ats": EXAMPLE_SYSTEM, "ext_id": ext, "company": employer.name,
        "title": f"Programs Director {ext}", "match_key": match_key(employer.name, f"Programs Director {ext}"),
        "first_seen": NOW, "last_seen": NOW, "links": [], "location": "Remote", "work_mode": "remote",
        "comp_min": top - 10_000 if top else None, "comp_max": top, "filtered_at": NOW,
        "url": f"https://jobs.example.test/{ext}", **more})


def role(slug: str, status: str, top: int | None = None, **more) -> Opportunity:
    return Opportunity(**{
        "owner": OWNER, "slug": slug, "company": "Fabrikam", "title": f"Lead {slug}", "fit": 3, "origin": "scout",
        "status": status, "match_key": f"fabrikam|lead {slug}", "found": TODAY, "location": "Remote",
        "move_type": "remote", "comp": f"${top - 10_000:,}-${top:,}" if top else None, **more})


def stopped_pay(version: int | None) -> dict:
    """What a role the pay gate stopped holds, under this version of Hard pass (none: made before it was kept)."""
    return {"screened_out_reason": "Pay must be at least $95,000 a year.", "screened_out_by": "compensation",
            "screened_out_step": "gate" if version else None,
            "screened_out_gate_id": "compensation" if version else None,
            "screened_out_hard_pass": version, "screened_out_criteria": version}


async def _world(session):
    """Hard pass at version 2 (the lowest pay 50,000) after a version 1 (95,000) that stops are left over from: no
    save of it ran a re-gate. Returns nothing; the rows are read back by name."""
    await load_profile(session, OWNER, gates=gates(95_000), scoring={"asks": []})
    await profile_store.save(session, OWNER, "gates", gates(50_000), by="test")
    fab = await employers.find_or_make(session, OWNER, "Fabrikam")
    session.add_all([
        # A gate's stop under version 1, one under none, and the stops the retired title check made: one that holds
        # nothing yet and one that is whole. A re-gate leaves both as they are: nothing counts or stamps them.
        posting(fab, "low", 80_000, filtered_by="Pay must be at least $95,000 a year.", judged_by="gate",
                judged_by_version=1, stop_gate="compensation", stop_gate_id="compensation", stop_hard_pass=1),
        posting(fab, "none", 80_000, filtered_by="Pay must be at least $95,000 a year.", judged_by="gate"),
        posting(fab, "title-bare", 200_000, filtered_by="not your kind of job", judged_by="titles"),
        posting(fab, "title-whole", 200_000, filtered_by="not your kind of job", judged_by="titles",
                judged_by_version=1, stop_gate="kind", stop_hard_pass=1),
        posting(fab, "passed", 200_000, route="titles"),
    ])
    session.add_all([
        role("back", stages.GATED_OUT, 80_000, **stopped_pay(1)),
        role("still", stages.GATED_OUT, 30_000, **stopped_pay(1)),
        role("by-hand", stages.GATED_OUT, 80_000, **stopped_pay(1)),
        role("tracker", stages.GATED_OUT, 80_000, screened_out_reason="Screened out in your old tracker: made up",
             screened_out_by="other", screened_out_step="tracker"),
        role("found", stages.SECOND_IMPRESSION, 200_000),
    ])
    await session.flush()
    rows = {r.slug: r for r in (await session.scalars(select(Opportunity))).all()}
    await trail.record(session, OWNER, event="role_moved", sentence="You moved it.", actor="person",
                       link=("opportunity", rows["by-hand"].id))
    await session.flush()


async def _by_ext() -> dict[str, JobPosting]:
    async with db.session_factory()() as session:
        rows = (await session.scalars(select(JobPosting).execution_options(populate_existing=True))).all()
        return {p.ext_id: p for p in rows}


async def _by_slug() -> dict[str, Opportunity]:
    async with db.session_factory()() as session:
        rows = (await session.scalars(select(Opportunity).execution_options(populate_existing=True))).all()
        return {r.slug: r for r in rows}


async def _lines() -> int:
    """The trail's lines, counted by a reader: it takes no writer turn, so a test may count while its own session has
    written (it sees what was committed, so a test commits what it set up first)."""
    async with db.read_session_factory()() as session:
        return await session.scalar(select(func.count()).select_from(TrailEvent)) or 0


# --- the stops ------------------------------------------------------------------------------------------------------


async def test_stops_under_an_older_hard_pass_or_none_are_counted_and_no_other(js):
    async with db.session_factory()() as session:
        await _world(session)
        # A gate's stop under version 1, one under none; two roles under 1 (one that stays stopped). Not: the title
        # check's stops (bare or whole), a role the owner moved, a role the old tracker screened.
        assert await regate.stale(session, OWNER) == 4


async def test_the_re_gate_runs_by_itself_and_every_stop_it_keeps_says_it_was_judged_under_the_hard_pass_in_force(js):
    async with db.session_factory()() as session:
        await _world(session)
        await session.commit()
        before = await _lines()
        done = await keep_current.run(session, OWNER)
        await session.commit()
    assert done["scores"] is None
    assert done["gates"] == "Re-checked 5 postings and 3 roles: 3 now pass.", done
    posts, roles = await _by_ext(), await _by_slug()
    for ext in ("low", "none"):  # 80,000 passes the lowest pay of 50,000: on to become a role, with no stop
        assert (posts[ext].filtered_by, posts[ext].route, posts[ext].stop_gate, posts[ext].stop_hard_pass) == (
            None, "roles", None, None), ext
    assert posts["title-bare"].filtered_by == "not your kind of job", "the title check's stop is not a gate's to undo"
    assert (posts["title-bare"].stop_gate, posts["title-bare"].stop_hard_pass) == (None, None), "and not stamped"
    assert (posts["title-whole"].stop_gate, posts["title-whole"].stop_hard_pass) == ("kind", 1), "a whole one is left"
    # The role was stored already Gated out, so no stage it left is known: it goes back to First Impression.
    assert roles["back"].status == stages.FIRST_IMPRESSION and roles["back"].screened_out_hard_pass is None
    assert (roles["still"].status, roles["still"].screened_out_hard_pass, roles["still"].screened_out_step) == (
        stages.GATED_OUT, 2, "gate"), "still stopped, under the version in force now"
    assert (roles["by-hand"].status, roles["by-hand"].screened_out_hard_pass) == (stages.GATED_OUT, 1), (
        "a role the owner moved is never moved by a re-run")
    assert (roles["tracker"].status, roles["tracker"].screened_out_step) == (stages.GATED_OUT, "tracker")
    assert await _lines() > before, "and the role's history says it moved"
    async with db.session_factory()() as session:
        assert await regate.stale(session, OWNER) == 0, "nothing is left that a re-gate would write a version to"
        again = await keep_current.run(session, OWNER)
        await session.commit()
    assert again == {"gates": None, "scores": None}, "once everything is current there is nothing to do"


async def test_a_re_gate_that_is_owed_asks_whether_any_stop_is_stale_once_and_in_a_reader(js, monkeypatch):
    async with db.session_factory()() as session:
        await _world(session)
        await session.commit()
    asked: list[bool] = []
    real = regate.stale

    async def noting(session, owner):
        asked.append(db.is_reader(session))
        return await real(session, owner)

    monkeypatch.setattr(regate, "stale", noting)
    async with db.session_factory()() as session:
        done = await keep_current.run(session, OWNER)
        await session.commit()
    assert done["gates"], "a re-gate was owed and ran"
    assert asked == [True], "the question is asked once, with no writer turn"


async def test_when_every_stop_is_current_it_does_nothing_and_writes_nothing(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(95_000), scoring={"asks": []})
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add(posting(fab, "low", 80_000, filtered_by="Pay must be at least $95,000 a year.", judged_by="gate",
                            judged_by_version=1, stop_gate="compensation", stop_gate_id="compensation",
                            stop_hard_pass=1))
        session.add(role("held", stages.GATED_OUT, 80_000, **stopped_pay(1)))
        await session.commit()
        before = await _lines()
        assert await regate.catch_up(session, OWNER) is None
        assert await keep_current.run(session, OWNER) == {"gates": None, "scores": None}
    assert await _lines() == before


async def test_a_save_of_hard_pass_that_changes_nothing_leaves_the_stops_and_the_next_start_follows_the_release(js):
    async with db.session_factory()() as session:
        await _world(session)
        await session.commit()
        assert await profile_store.save(session, OWNER, "gates", gates(50_000), by="test") is None, "no new version"
        assert await regate.stale(session, OWNER) == 4, "so a save runs nothing, and the stops stay"
        await run_startup_fixups(session, OWNER)
        await session.commit()
    assert (await _by_slug())["back"].status == stages.FIRST_IMPRESSION, "the start-up followed the release"
    async with db.session_factory()() as session:
        assert await regate.stale(session, OWNER) == 0
        await session.commit()
        before = await _lines()
        await run_startup_fixups(session, OWNER)  # a second start: nothing is judged twice
        await session.commit()
        assert await _lines() == before


# --- the night's read -------------------------------------------------------------------------------------------------


def test_the_nights_read_begins_with_the_step_that_keeps_the_log_current():
    library = process.build_library().get(process.READ_BOARDS)
    flows = {(f.source, f.target) for f in library.flows.values()}
    assert ("Start_read", "Keep_current") in flows and ("Keep_current", "Resolve_links") in flows
    assert ("Start_read", "Resolve_links") not in flows
    assert library.elements["Keep_current"].action == "jobs.keep_current"


async def test_the_nights_read_judges_again_what_a_release_left_before_it_filters_anything(js, monkeypatch):
    # No network: every page is a 404.
    monkeypatch.setattr(polite, "make", lambda: Fetcher(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))), per_host_seconds=0))
    async with db.session_factory()() as session:
        await _world(session)
        await session.commit()
    outcome, _summary, _counts = await boards_night.read_boards(OWNER, details_per_night=5)
    assert outcome == "done"
    assert (await _by_slug())["back"].status == stages.FIRST_IMPRESSION
    assert (await _by_ext())["low"].filtered_by is None
    async with db.session_factory()() as session:
        assert await regate.stale(session, OWNER) == 0


async def test_a_pass_that_cannot_finish_is_logged_and_never_stops_the_nights_read(js, monkeypatch, caplog):
    async def fails(*args, **kwargs):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(keep_current, "run", fails)
    async with db.session_factory()() as session:
        assert await actions.keep_current_step(session, None, {"owner": OWNER}) == {"gates": None, "scores": None}
    assert "keeping the log current failed" in caplog.text


async def test_the_re_gate_failing_does_not_stop_the_scores_nor_the_other_way_round(js, monkeypatch, caplog):
    async with db.session_factory()() as session:
        await _world(session)
        await session.commit()

    async def fails(*args, **kwargs):
        raise RuntimeError("the gates broke")

    with monkeypatch.context() as broke:
        broke.setattr(regate, "catch_up", fails)
        async with db.session_factory()() as session:
            done = await keep_current.run(session, OWNER)
            await session.commit()
    assert done == {"gates": None, "scores": None} and "couldn't check the gates again" in caplog.text
    assert (await _by_slug())["back"].status == stages.GATED_OUT, "nothing was changed by the pass that failed"
    with monkeypatch.context() as broke:
        broke.setattr(prioritize, "catch_up", fails)
        async with db.session_factory()() as session:
            done = await keep_current.run(session, OWNER)
            await session.commit()
    assert done["gates"] and done["scores"] is None and "couldn't work the scores out again" in caplog.text
    assert (await _by_slug())["back"].status == stages.FIRST_IMPRESSION, "the gates still ran"


# --- the scores -------------------------------------------------------------------------------------------------------

SETTINGS = {**PROFILE["settings"]}


async def _scored(session, profile, company, *, version, said=("met", "met", "not"), researched=True):
    """A role with its answers, worked out under this version (none: as every score a release finds is)."""
    employer = await employers.find_or_make(session, OWNER, company)
    given = answered(*said)["asks"] if said else []
    row = Opportunity(
        owner=OWNER, slug=company.lower(), company=company, title="Programs Lead", status=stages.SECOND_IMPRESSION,
        origin="scout", match_key=match_key(company, "Programs Lead"), found=TODAY, employer_id=employer.id,
        location="Remote, US", move_type="remote",
        fit_breakdown={"answers": scoring.record([], given, profile, "reader")},
        analysis_state=prioritize.RESEARCHED if researched else None,
        analysis={"required": [{"item": "Five years", "class": "P", "by": "analyst"}]} if researched else {})
    session.add(row)
    await session.flush()
    if researched:
        prioritize.work_out(row, profile, employer=employer, version=version)
    return row


async def test_scores_under_an_older_prioritization_or_none_are_worked_out_again_by_themselves(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, settings=SETTINGS)
        profile = await profile_store.current(session, OWNER)
        await _scored(session, profile, "Unversioned", version=None)
        await _scored(session, profile, "Current", version=1)
        await _scored(session, profile, "Waiting", version=None, researched=False)  # answers, no score: not a score
        await _scored(session, profile, "Answerless", version=None, said=())  # no reader answers: none is waited for
        await session.commit()
        assert await prioritize.stale_scores(session, OWNER, profile, 1) == 2, "only the ones a re-score would write"
        done = await keep_current.run(session, OWNER)
        await session.commit()
        assert done["gates"] is None
        assert done["scores"] == "Worked out 3 scores again: none changed.", done
        assert await prioritize.stale_scores(session, OWNER, profile, 1) == 0
        again = await keep_current.run(session, OWNER)
        assert again == {"gates": None, "scores": None}
    async with db.session_factory()() as session:
        rows = {r.company: r for r in (await session.scalars(select(Opportunity))).all()}
    assert rows["Unversioned"].fit_breakdown["version"] == 1, "it names the version it was worked out under now"
    assert rows["Current"].fit_breakdown["version"] == 1
    assert rows["Waiting"].fit is None, "a role that waits for its research stays without a score"
    assert rows["Answerless"].fit_breakdown["version"] == 1, "nothing waits on a reader, so it is scored too"


async def test_the_checks_take_no_turn_and_the_scores_are_written_a_page_at_a_time_as_the_one_transaction_run_writes(
        js, monkeypatch):
    """On a large database the start's check of the stops and scores held the file's one writer turn for seconds,
    and a re-score of thousands of scores for longer. The checks are made in a reader's session; the
    scores are read and worked out a page at a time with no turn, and only the page's write is a writer's turn: a
    second writer gets through while each page is worked out. The scores written are those of the run inside a
    savepoint (one transaction, as the engine's call runs it)."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, settings=SETTINGS)
        profile = await profile_store.current(session, OWNER)
        for n in range(5):
            await _scored(session, profile, f"Firm{n}", version=None, said=("met", "met", "not") if n % 2 else
                          ("met", "not", "not"))
        await session.commit()
    monkeypatch.setattr(prioritize, "PAGE", 2)
    readers: list[tuple[str, bool]] = []
    free: list[bool] = []
    real = {"stale_scores": prioritize.stale_scores, "stale": regate.stale, "posted": freshness.posted_by_role}

    def noting(name):
        async def wrapped(session, *args, **kwargs):
            readers.append((name, db.is_reader(session)))
            return await real[name](session, *args, **kwargs)
        return wrapped

    async def probing(session, *args, **kwargs):
        async with db.session_factory()() as other:  # a second writer, asking as each page is read
            try:
                await asyncio.wait_for(other.execute(update(Opportunity).where(Opportunity.slug == "none")
                                                     .values(slug="none")), 1)
                free.append(True)
            except TimeoutError:  # queued behind the pass: it still holds the writer's turn
                free.append(False)
            await other.rollback()
        return await real["posted"](session, *args, **kwargs)

    async def snapshot() -> dict:
        async with db.read_session_factory()() as session:
            rows = (await session.execute(select(Opportunity.slug, Opportunity.fit_breakdown, Opportunity.fit))).all()
        return {row.slug: (row.fit_breakdown, row.fit) for row in rows}

    before = await snapshot()
    monkeypatch.setattr(prioritize, "stale_scores", noting("stale_scores"))
    monkeypatch.setattr(regate, "stale", noting("stale"))
    monkeypatch.setattr(freshness, "posted_by_role", probing)
    async with db.session_factory()() as session:
        paged = await keep_current.run(session, OWNER)
        await session.commit()
    after = await snapshot()
    assert paged["scores"] == "Worked out 5 scores again: none changed." and after != before
    assert readers == [("stale", True), ("stale_scores", True)], "the checks read without the writer's turn"
    assert free == [True] * 3, "a page (of 2, 5 roles) was read while the pass held the writer's turn"
    async with db.session_factory()() as session:  # the same roles, stale again, worked out in one savepoint
        for slug, (breakdown, fit) in before.items():
            await session.execute(update(Opportunity).where(Opportunity.slug == slug).values(
                fit_breakdown=breakdown, fit=fit))
        await session.commit()
    monkeypatch.setattr(freshness, "posted_by_role", real["posted"])
    readers.clear()
    async with db.session_factory()() as session:
        async with session.begin_nested():
            whole = await keep_current.run(session, OWNER)
        await session.commit()
    assert readers == [("stale", False), ("stale_scores", False)], "inside a savepoint it reads in its own transaction"
    assert whole == paged and await snapshot() == after


async def test_a_score_worked_out_by_another_arithmetic_or_by_none_is_worked_out_again_once_whatever_its_version(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, settings=SETTINGS)
        profile = await profile_store.current(session, OWNER)
        await _scored(session, profile, "Current", version=1)
        older = await _scored(session, profile, "Older", version=1)
        nameless = await _scored(session, profile, "Nameless", version=1)
        assert older.fit_breakdown["math"] == scoring.MATH, "a score keeps the arithmetic it was worked out by"
        older.fit_breakdown = {**older.fit_breakdown, "math": scoring.MATH - 1}
        nameless.fit_breakdown = {k: v for k, v in nameless.fit_breakdown.items() if k != "math"}
        await session.commit()
        assert await prioritize.stale_scores(session, OWNER, profile, 1) == 2, "the version is the current one"
        done = await keep_current.run(session, OWNER)
        await session.commit()
        assert done["scores"] == "Worked out 3 scores again: none changed.", done
        assert await prioritize.stale_scores(session, OWNER, profile, 1) == 0
        assert (await keep_current.run(session, OWNER)) == {"gates": None, "scores": None}, "and once is enough"
    async with db.session_factory()() as session:
        rows = (await session.scalars(select(Opportunity))).all()
    assert {r.fit_breakdown["math"] for r in rows} == {scoring.MATH}


async def test_scores_kept_before_the_answers_were_kept_apart_are_brought_up_to_date_too(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, settings=SETTINGS)
        profile = await profile_store.current(session, OWNER)
        row = await _scored(session, profile, "Legacy", version=None)
        rows = scoring.compute({"asks": answered("met", "met", "not")["asks"]}, profile)
        row.fit_breakdown, row.fit = rows, rows["total"]  # the rows that score them, no answers apart, no version
        await session.commit()
        assert await prioritize.stale_scores(session, OWNER, profile, 1) == 1
        assert (await keep_current.run(session, OWNER))["scores"] == "Worked out 1 score again: none changed."
        await session.commit()
        await session.refresh(row)
    assert row.fit_breakdown["version"] == 1 and row.fit_breakdown["answers"], "answers apart, and the version"


# --- a run cut short ----------------------------------------------------------------


async def _a_log_the_second_hard_pass_stops_all_of(monkeypatch, *, fails_in: int):
    """Thirty postings paying 60,000 to 118,000: eighteen stopped under a floor of 95,000 (version 1), and a floor of
    300,000 (version 2) now stops all thirty, the other twelve for the first time, in slices of five. The re-gate is
    made to fail in its `fails_in`th slice of writes."""
    monkeypatch.setattr(regate, "CHUNK", 5)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(95_000), scoring={"asks": []})
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add_all([posting(fab, str(n), 60_000 + n * 2_000, route="titles") for n in range(30)])
        await session.flush()
        await regate.run(session, OWNER, cause="under the first Hard pass", today=TODAY)
        await profile_store.save(session, OWNER, "gates", gates(300_000), by="test")
        await session.commit()
    real, calls = regate._bulk, []

    async def fails(session, rows):
        calls.append(len(rows))
        if len(calls) == fails_in:
            raise RuntimeError("the database went away")
        return await real(session, rows)

    monkeypatch.setattr(regate, "_bulk", fails)
    return real


async def _stopped_postings() -> int:
    return sum(1 for p in (await _by_ext()).values() if p.filtered_by is not None)


async def test_a_regate_cut_short_at_the_nights_step_keeps_its_finished_slices_and_the_next_start_finishes_it(
        js, monkeypatch, caplog):
    real = await _a_log_the_second_hard_pass_stops_all_of(monkeypatch, fails_in=2)
    assert await _stopped_postings() == 18
    async with db.session_factory()() as session:
        assert await actions.keep_current_step(session, None, {"owner": OWNER}) == {"gates": None, "scores": None}
        await session.commit()
    assert "couldn't check the gates again" in caplog.text
    assert await _stopped_postings() == 18 + 5, "the first slice of five was committed, the second was not"
    monkeypatch.setattr(regate, "_bulk", real)
    async with db.session_factory()() as session:
        assert await regate.stale(session, OWNER) > 0
        done = await actions.keep_current_step(session, None, {"owner": OWNER})
        await session.commit()
        assert done["gates"] and await regate.stale(session, OWNER) == 0
    assert await _stopped_postings() == 30


async def test_inside_a_savepoint_a_regate_cut_short_leaves_nothing_as_the_starts_steps_do(js, monkeypatch, caplog):
    await _a_log_the_second_hard_pass_stops_all_of(monkeypatch, fails_in=2)
    async with db.session_factory()() as session:
        async with session.begin_nested():  # a step of the start's repairs: nothing in it can be committed
            assert await keep_current.run(session, OWNER) == {"gates": None, "scores": None}
        await session.commit()
    assert "couldn't check the gates again" in caplog.text
    assert await _stopped_postings() == 18, "the whole run was one transaction, and it went with the savepoint"


async def test_a_card_stored_before_the_preferred_tally_is_worked_out_again_from_the_stored_classes(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring={**FIVE, "grade_requirements": GRADED_ON}, settings=SETTINGS)
        profile = await profile_store.current(session, OWNER)
        row = await _scored(session, profile, "Old card", version=1)
        row.analysis = {**row.analysis, "preferred": [{"item": "Nice to have", "class": "U", "by": "analyst"}]}
        row.fit_breakdown = {**row.fit_breakdown, "asks": [
            {k: v for k, v in r.items() if k != "card_preferred"} for r in row.fit_breakdown["asks"]]}
        row.fit_breakdown = {**row.fit_breakdown, "math": scoring.MATH - 1}
        await session.commit()
        assert await prioritize.stale_scores(session, OWNER, profile, 1) == 1
        await keep_current.run(session, OWNER)
        await session.commit()
        assert row.fit_breakdown["math"] == scoring.MATH
        grade = next(r for r in row.fit_breakdown["asks"] if r.get("kind") == "grade")
        assert grade["card_preferred"] == {"proven": 0, "transferable": 0, "unproven": 1}

