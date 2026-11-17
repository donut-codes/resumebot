"""What the tracker import makes of a row, and how it keeps the app in step with the file when it runs again: the
tracker's words read into the app's (a way of working, a fit, a screen-out, a key, pay), a role the app has not worked
on brought up to date, one it has left alone. The workbooks are made here, in memory: never the owner's file."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select, update

from app import db
from app.jobsearch import stages
from app.jobsearch.employers import core as employers
from app.jobsearch.fit import classifier
from app.jobsearch.gating import role_gates
from app.jobsearch.imports import tracker
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import Employer, Opportunity, Packet, StageMove
from app.jobsearch.pipeline import deck as pipeline
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis
from app.models.work import TrailEvent
from app.timeutil import owner_today
from app.trail import service as trail
from tests import builders
from tests.builders import analysis_setup, findings, submit_findings
from tests.support import OWNER, POSTING_WORDS, answered, load_profile, tracker_row, tracker_workbook


async def _import(*rows):
    async with db.session_factory()() as session:
        counts = await tracker.import_tracker(session, OWNER, tracker_workbook(list(rows)))
        await session.commit()
    return counts


def _counts(**some) -> dict:
    """The counts of an import that did nothing but what `some` says."""
    return {"rows": 0, "added": 0, "already_there": 0, "updated": 0, "left_as_in_app": 0, "repeated": 0,
            "skipped_blank": 0, **some}


async def _roles() -> dict[str, Opportunity]:
    """The owner's roles, by title."""
    async with db.session_factory()() as session:
        rows = (await session.scalars(select(Opportunity).where(Opportunity.owner == OWNER))).all()
        return {r.title: r for r in rows}


async def _gates(role: Opportunity) -> dict[str, dict]:
    """The role's gate lines under the made-up profile, by gate id."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        profile = await profile_store.current(session, OWNER)
        return {g["id"]: g for g in role_gates.gate_results(role, profile, owner_today(), None)}


# --- the way of working ------------------------------------------------------------------------------------------


async def test_the_trackers_move_type_is_a_way_of_working_only_when_it_is_one(js):
    words = {"Remote One": "remote", "Remote Two": "Remote", "Local": "local", "Unverified": "unverified",
             "Promotion": "promotion", "Lateral": "lateral", "Hybrid": "hybrid", "Odd": "il relocation exception",
             "Blank": None}
    await _import(*[tracker_row("Initrode", title, moveType=word) for title, word in words.items()])
    modes = {title: role.move_type for title, role in (await _roles()).items()}
    assert modes == {"Remote One": "remote", "Remote Two": "remote", "Local": None, "Unverified": None,
                     "Promotion": None, "Lateral": None, "Hybrid": "hybrid", "Odd": None, "Blank": None}


async def test_a_tier_word_does_not_turn_a_remote_place_into_one_the_gate_refuses(js):
    """The gate read the tracker's `promotion` as the way of working and found no town named in "Remote, US"."""
    await _import(tracker_row("Initrode", "Programs Lead", moveType="promotion", location="Remote, US"))
    gates = await _gates((await _roles())["Programs Lead"])
    assert gates["location"]["result"] == "pass", gates["location"]


# --- the fit ----------------------------------------------------------------------------------------------------


async def test_the_plugins_fit_stays_on_the_role_as_its_figure_and_the_apps_own_fit_is_empty(js):
    await _import(
        tracker_row("Initrode", "Scored", fit=7, fitEvidence="base 4; internal +1"),
        tracker_row("Initrode", "Worded", fit="Core"),
        tracker_row("Initrode", "Unscored", fit=None, fitEvidence="carries the board work"),
        tracker_row("Initrode", "Nothing", fit=None))
    roles = await _roles()
    assert [r.fit for r in roles.values()] == [None] * 4, "the plugin's figure is not on the app's scale"
    assert roles["Scored"].fit_evidence == (
        "Your old tracker scored it 7, on a scale of its own that this app does not use. base 4; internal +1")
    assert roles["Worded"].fit_evidence == "Your old tracker rated it Core."
    assert roles["Unscored"].fit_evidence == "carries the board work"
    assert roles["Nothing"].fit_evidence is None


async def test_a_role_with_no_score_yet_is_failed_by_no_gate_on_its_fit(js):
    """No gate reads a role's fit (docs/PRD.md 30.4): a role with no score yet, or a score of nothing, has no fit line
    among its gates and fails none."""
    await _import(tracker_row("Initrode", "Unscored"))
    role = (await _roles())["Unscored"]
    assert "fit" not in await _gates(role)
    role.fit = 0
    gates = await _gates(role)
    assert "fit" not in gates and not [g for g in gates.values() if g["result"] == "fail"]


async def test_an_imported_role_with_no_score_is_researched_after_the_scored_ones_and_research_scores_it(
    js, monkeypatch,
):
    # The three scored roles are found on one day and the night's picks read that same day: pinned, so the order does
    # not depend on the day or the zone the suite runs in. The imported role is dated by its posting, seen just now.
    day = date(2026, 10, 14)
    monkeypatch.setattr(builders, "owner_today", lambda: day)
    monkeypatch.setattr(pipeline, "owner_today", lambda: day)  # the deck order counts a posting's age from today
    await analysis_setup()
    await _import(tracker_row("Initrode", "Imported Lead", location="Remote, US", moveType="remote", fit=2))
    async with db.session_factory()() as session:
        # An import brings no source, and a role with none is never researched (`postings/sourced.py`): this one has
        # been Sourced, as the app's own sourcing or the browser reader would.
        await session.execute(update(Opportunity).where(Opportunity.company == "Initrode").values(
            source_url="https://careers.example.test/jobs/1", source_kind="employer", sourced_at=datetime.now(UTC)))
        picked = await analysis.picks(session, OWNER, 10)
        assert [r.company for r in picked] == ["Fabrikam", "Initech", "Hooli", "Initrode"], (
            "scored roles first, best first; the unscored role last; there is no bar for Hooli's 40% to sit under")
        imported = picked[-1]
        imported.description = POSTING_WORDS  # an import brings no text, and a role with none is never researched
        await analysis.start(session, OWNER, imported)
        await session.commit()
        imported_id = imported.id
    reply = await submit_findings(findings(imported_id, fit_breakdown=answered("met", "met", "not")))
    assert reply["taken"] is True and reply["fit"] == 4 and reply["stage"] == stages.READY_TO_LAUNCH
    assert reply["fit_pct"] == 80


# --- what the plugin screened out -------------------------------------------------------------------------------


SCREEN_OUTS = [
    # (a row's status and fit cell, its notes) -> the reason it is given, and the gate that reason is about
    (("rejected", "REJECTED - trajectory gate", None), "trajectory gate", "other"),
    (("rejected", "REJECTED - comp floor", None), "comp floor", "compensation"),
    (("rejected", "REJECTED - exclusion", None), "exclusion", "employer"),
    (("rejected", "REJECTED - can't-do", None), "can't-do", "other"),
    (("dead", "REJECTED - fit", None), "fit", "other"),
    (("rejected", 4, "[2026-10-10 batch run] REJECTED -- core-requirement gate: 3 of 7 Unproven | more"),
     "core-requirement gate: 3 of 7 Unproven", "other"),
    (("rejected", 4, "[2026-09-24 top-11 batch] rejected: comp gate: $127-165K, top under the floor"),
     "comp gate: $127-165K, top under the floor", "compensation"),
    (("rejected", 4, "Strong fit. GATE-FAIL under 2026-09-24 profile: comp floor (Manager band undisclosed)"),
     "under 2026-09-24 profile: comp floor (Manager band undisclosed)", "compensation"),
    (("rejected", 4, "[2026-10-01] status->rejected: gate-fail: location (gotham, ny on-site/hybrid, unverified)"),
     "location (gotham, ny on-site/hybrid, unverified)", "location"),
    (("rejected", 4, "[2026-10-02] gate-rejected in the re-screen: plain manager title"),
     "in the re-screen: plain manager title", "other"),
    (("rejected", 4, "Rejected on same-company dedupe, not on fit. Loses to the active row"),
     "rejected on same-company dedupe, not on fit. Loses to the active row", "other"),
    (("rejected", 4, "A hard gate (\"disqualifying\"). Moved from new (fit=5) to rejected. Packet already built."),
     "a hard gate", "other"),
]


@pytest.mark.parametrize(("row", "why", "gate"), SCREEN_OUTS)
async def test_a_row_the_old_tracker_screened_out_comes_in_gated_out_with_its_reason(js, row, why, gate):
    status, fit, notes = row
    await _import(tracker_row("Initrode", "Screened", status=status, fit=fit, notes=notes))
    role = (await _roles())["Screened"]
    assert (role.status, role.ended_reason, role.screened_out_by) == (stages.GATED_OUT, None, gate)
    assert role.screened_out_reason is not None
    assert role.screened_out_reason.startswith("Screened out in your old tracker: ")
    assert why.lower() in role.screened_out_reason.lower(), role.screened_out_reason
    assert role.fit is None


async def test_a_rejection_the_employer_sent_and_one_nothing_explains_stay_rejected(js):
    await _import(
        tracker_row("Initrode", "Emailed", status="rejected",
                    notes="Applied. [email-sync 2026-09-30] rejection email 2026-09-29: Initrode: not moving forward"),
        # An email from the employer is the employer's no, whatever else the notes say of the gates.
        tracker_row("Initrode", "Emailed And Gated", status="rejected",
                    notes="GATE-FAIL comp: top under the floor | [email-sync 2026-09-30] rejection email: no"),
        tracker_row("Initrode", "Unexplained", status="rejected", notes="Full JD read; a good role.", fit=5),
        tracker_row("Initrode", "Quiet", status="dead", notes="gate-fail: stale (posted 90 days ago)"),
        tracker_row("Initrode", "Kept", status="new", notes="GATE-FAIL under the old profile, kept anyway"),
        tracker_row("Initrode", "Applied", status="applied", notes="gate-fail comp, applied regardless"))
    roles = await _roles()
    assert {t: (r.status, r.ended_reason) for t, r in roles.items()} == {
        "Emailed": ("rejected", "they_said_no"), "Emailed And Gated": ("rejected", "they_said_no"),
        "Unexplained": ("rejected", "they_said_no"), "Quiet": ("gone_quiet", "unknown"),
        "Kept": ("landing_zone", None), "Applied": ("applied", None)}
    assert all(r.screened_out_reason is None and r.screened_out_by is None for r in roles.values())


async def test_what_the_plugin_screened_out_teaches_the_classifier_nothing_and_an_employers_no_counts_as_pursued(js):
    await _import(
        tracker_row("Initrode", "Gated", status="rejected", fit="REJECTED - trajectory gate"),
        tracker_row("Initrode", "Gated By Notes", status="rejected", notes="REJECTED: core-requirement gate: 3 of 7"),
        tracker_row("Initrode", "Employer No", status="rejected", notes="[email-sync 2026-09-30] rejection email: no"))
    roles = await _roles()
    assert classifier.lesson(roles["Gated"], set()) is None
    assert classifier.lesson(roles["Gated By Notes"], set()) is None
    assert classifier.lesson(roles["Employer No"], set()) == (classifier.PURSUED, True, 2.0)


# --- the key ---------------------------------------------------------------------------------------------------


async def test_a_role_is_stored_under_the_scouts_key_not_the_trackers_own_match_key_column(js):
    await _import(tracker_row("Initrode  Corp", "Programs  Lead", matchKey="initrode|a hand-made key"))
    role = (await _roles())["Programs  Lead"]
    assert role.match_key == match_key("Initrode  Corp", "Programs  Lead") == "initrode corp|programs lead"


async def test_a_role_an_earlier_import_stored_under_another_key_is_found_and_not_stored_twice(js):
    async with db.session_factory()() as session:
        session.add_all([
            # stored under the tracker's own key, which names the company the plugin has since corrected
            Opportunity(owner=OWNER, slug="a", company="Swooped", title="Director", status="second_impression",
                        origin="tracker", match_key="swooped|director"),
            # stored under a key that is not what its company and title make
            Opportunity(owner=OWNER, slug="b", company="Initrode", title="Lead", status="second_impression",
                        origin="tracker", match_key="an old key|lead")])
        await session.commit()
    counts = await _import(
        tracker_row("ButterflyMX", "Director", matchKey="swooped|director", notes="the real employer"),
        tracker_row("Initrode", "Lead", notes="found by its own company and title"))
    assert counts == _counts(rows=2, already_there=2, updated=2)
    assert {r.notes for r in (await _roles()).values()} == {"the real employer", "found by its own company and title"}


async def test_an_imported_role_links_to_the_employer_of_its_whole_name_never_to_one_that_is_part_of_it(js):
    """"Mercury Insurance" is not "Mercury" (a fintech's job board): a new record is better than a wrong link. Only the
    words that tell no employer from another ("Co.", "Inc.", a bracket) are left out of a name."""
    async with db.session_factory()() as session:
        for name in ("Mercury", "Marsh"):
            await employers.find_or_make(session, OWNER, name)
        await session.commit()
    await _import(tracker_row("Mercury Insurance", "Product Manager"), tracker_row("Mercury", "Data Lead"),
                  tracker_row("Marsh Risk", "Analyst"), tracker_row("Marsh & Co.", "Associate"),
                  tracker_row("Mercury (via Jobgether)", "Operations Lead"))
    async with db.session_factory()() as session:
        await employers.settle(session, OWNER)
        await session.commit()
        linked = (await session.execute(
            select(Opportunity.title, Employer.name).join(Employer, Employer.id == Opportunity.employer_id))).all()
        names = (await session.scalars(select(Employer.name))).all()
    assert dict(linked) == {"Product Manager": "Mercury Insurance", "Data Lead": "Mercury", "Analyst": "Marsh Risk",
                            "Associate": "Marsh", "Operations Lead": "Mercury"}
    assert sorted(names) == ["Marsh", "Marsh Risk", "Mercury", "Mercury Insurance"]


# --- the pay ---------------------------------------------------------------------------------------------------


PAY = [
    # (compSource, comp) -> (stored as posted pay, said as the plugin's estimate)
    ("posting", "$150,000 - $180,000", True, None),
    ("posted", "$159K–$210K", True, None),
    ("posting (tenantone)", "$100,100-$185,900 base + bonus", True, None),
    ("posted (ceiling only, floor not disclosed)", "Up to $312,000", True, None),
    ("disclosed in jd", "$100,000-$125,000", True, None),
    (None, "$160,500-$305,000 USD (posted)", True, None),
    ("none", "$144K-$245K posted (offer typically min to mid)", True, None),
    ("estimate", "not disclosed; est. $200K-$240K", False, "not disclosed; est. $200K-$240K"),
    ("researched", "$230K–$395K (est.)", False, "$230K–$395K (est.)"),
    ("tracker estimate", "Not posted; estimate $140,000-$180,000", False, "Not posted; estimate $140,000-$180,000"),
    ("posted (deloitte estimate)", "$144,300-$265,600 (wide band)", False, "$144,300-$265,600 (wide band)"),
    ("comp unposted", "$133,000-$215,000 (NYC-based)", False, "$133,000-$215,000 (NYC-based)"),
    ("recruiter-stated", "$135,000 (1-year contract)", False, "$135,000 (1-year contract)"),
    (None, "$112.7K-$193.2K", False, "$112.7K-$193.2K"),
    ("estimate", "Not disclosed", False, None),
    ("undisclosed", "Not disclosed", False, None),
]


@pytest.mark.parametrize(("source", "comp", "posted", "estimate"), PAY)
async def test_pay_the_plugin_estimated_is_not_stored_as_pay_the_posting_shows(js, source, comp, posted, estimate):
    await _import(tracker_row("Initrode", "Priced", fit=None, comp=comp, compSource=source, fitEvidence="base 4"))
    role = (await _roles())["Priced"]
    assert role.comp == (comp if posted else None)
    assert role.comp_source == (source or ("estimate" if not posted else None))
    if estimate:
        assert role.fit_evidence == f"base 4 Pay, your old tracker's estimate and not posted: {estimate}"
    else:
        assert role.fit_evidence == "base 4"


async def test_the_pay_gate_judges_posted_pay_and_leaves_an_estimate_alone(js):
    await _import(tracker_row("Initrode", "Posted Low", comp="$60,000-$70,000", compSource="posting"),
                  tracker_row("Initrode", "Estimated Low", comp="est. $60,000-$70,000", compSource="estimate"))
    roles = await _roles()
    assert (await _gates(roles["Posted Low"]))["compensation"]["result"] == "fail"
    assert (await _gates(roles["Estimated Low"]))["compensation"]["result"] == "unknown"


# --- coming in again ---------------------------------------------------------------------------------------------


async def test_a_role_the_app_has_not_worked_on_follows_the_file_and_a_second_run_changes_nothing(js):
    await _import(tracker_row("Initrode", "Lead", status="new", notes="found", location="Remote, US", fit=5))
    first = (await _roles())["Lead"]
    again = await _import(tracker_row("Initrode", "Lead", status="applied", notes="applied 2026-10-12",
                                      location="Remote, US", comp="$150,000-$180,000", compSource="posting", fit=6))
    assert again == _counts(rows=1, already_there=1, updated=1)
    role = (await _roles())["Lead"]
    assert (role.id, role.status, role.notes, role.comp, role.fit) == (
        first.id, "applied", "applied 2026-10-12", "$150,000-$180,000", None)
    assert role.fit_evidence.startswith("Your old tracker scored it 6,"), "the figure moves with the file"
    async with db.session_factory()() as session:
        moves = (await session.scalars(select(StageMove).where(StageMove.role_id == first.id))).all()
        lines = (await session.scalars(select(TrailEvent).where(TrailEvent.event == tracker.IMPORTED))).all()
    assert [(m.from_status, m.to_status) for m in moves] == [("landing_zone", "applied")], "a countable move"
    assert len(lines) == 2 and all(line.link_id is None for line in lines), "one line in the Activity for each import"
    assert "1 brought up to date" in max(lines, key=lambda line: line.at).sentence
    same = await _import(tracker_row("Initrode", "Lead", status="applied", notes="applied 2026-10-12",
                                     location="Remote, US", comp="$150,000-$180,000", compSource="posting", fit=6))
    assert same == _counts(rows=1, already_there=1), "nothing differs, so nothing is updated"
    async with db.session_factory()() as session:
        assert len((await session.scalars(select(TrailEvent).where(TrailEvent.event == tracker.IMPORTED))).all()) == 2


async def test_a_role_an_earlier_import_moved_is_not_taken_for_one_the_app_moved(js):
    """The import's own stage moves are in the history too: the next import must still follow the file."""
    await _import(tracker_row("Initrode", "Lead", status="new"))
    assert (await _import(tracker_row("Initrode", "Lead", status="applied")))["updated"] == 1
    assert (await _import(tracker_row("Initrode", "Lead", status="rejected")))["updated"] == 1
    role = (await _roles())["Lead"]
    assert (role.status, role.ended_reason) == ("rejected", "they_said_no")


async def test_every_stage_move_an_import_makes_has_the_time_of_its_line_in_the_activity_and_no_other_does(js):
    """Postgres stamped the moves with the transaction's time, so they shared one; on SQLite each row is stamped by the
    clock, so the import gives the session its one time for them. A move made outside the import is stamped as ever."""
    await _import(tracker_row("Initrode", "Lead", status="new"), tracker_row("Initrode", "Second", status="new"),
                  tracker_row("Initrode", "Third", status="new"))
    await _import(tracker_row("Initrode", "Lead", status="applied"),
                  tracker_row("Initrode", "Second", status="applied"),
                  tracker_row("Initrode", "Third", status="rejected"))
    async with db.session_factory()() as session:
        role = (await session.scalars(select(Opportunity).where(Opportunity.title == "Lead"))).one()
        role.status = stages.READY_TO_LAUNCH  # the app's own move, after the import's
        await session.commit()
        moves = (await session.scalars(select(StageMove).where(StageMove.from_status.is_not(None)))).all()
        lines = (await session.scalars(select(TrailEvent).where(TrailEvent.event == tracker.IMPORTED))).all()
        assert not session.sync_session.info.get("stage_moves_at"), "the session is left as it was found"
    ours = {line.at for line in lines}
    by_import = [m for m in moves if m.to_status != stages.READY_TO_LAUNCH]
    assert len(by_import) == 3 and len({m.at for m in by_import}) == 1 and by_import[0].at in ours
    assert [m.at for m in moves if m.to_status == stages.READY_TO_LAUNCH][0] not in ours


@pytest.mark.parametrize("how", ["researched", "given a packet", "moved by the owner", "moved by an agent",
                                 "gated on its employer's page"])
async def test_a_role_the_app_has_worked_on_stays_as_the_app_has_it(js, how):
    await _import(tracker_row("Initrode", "Lead", status="new", notes="found"))
    async with db.session_factory()() as session:
        role = (await session.scalars(select(Opportunity).where(Opportunity.title == "Lead"))).one()
        if how == "researched":
            role.analysis_state = analysis.DONE
        elif how == "given a packet":
            session.add(Packet(owner=OWNER, role_id=role.id, version=1, status="ready"))
        elif how == "moved by the owner":
            await trail.record(session, OWNER, event="role_moved", sentence="You moved it.",
                               link=("opportunity", role.id))
        elif how == "moved by an agent":
            role.status = stages.READY_TO_LAUNCH  # a stage move, whichever code made it, and no line of its own
        else:
            role.status, role.screened_out_reason, role.screened_out_by = (
                stages.GATED_OUT, "employer gated: never again", "employer")
        await session.commit()
    counts = await _import(tracker_row("Initrode", "Lead", status="rejected", notes="changed in the file"))
    assert counts == _counts(rows=1, already_there=1, left_as_in_app=1)
    role = (await _roles())["Lead"]
    assert role.notes == "found" and role.status != "rejected", "what the app did wins over the file"


async def test_a_link_the_app_followed_is_kept_and_the_status_still_follows_the_file(js):
    await _import(tracker_row("Initrode", "Lead", status="new", url="https://www.boardone.test/jobs/view/1"))
    async with db.session_factory()() as session:
        role = (await session.scalars(select(Opportunity).where(Opportunity.title == "Lead"))).one()
        role.board_url, role.url = role.url, "https://careers.initrode.example.test/jobs/1"
        await trail.record(session, OWNER, event="employer_posting_found", sentence="Found the employer's posting.",
                           link=("opportunity", role.id))
        await session.commit()
    counts = await _import(tracker_row("Initrode", "Lead", status="rejected",
                                       url="https://www.boardone.test/jobs/view/1"))
    assert counts == _counts(rows=1, already_there=1, updated=1)
    role = (await _roles())["Lead"]
    assert role.status == "rejected" and role.url == "https://careers.initrode.example.test/jobs/1"
    assert role.board_url == "https://www.boardone.test/jobs/view/1"


async def test_a_role_the_apps_own_search_found_is_never_changed_by_the_file(js):
    async with db.session_factory()() as session:
        session.add(Opportunity(owner=OWNER, slug="s", company="Initrode", title="Lead", status="second_impression",
                                fit=9, origin="scout", match_key="initrode|lead", notes="the scout's"))
        await session.commit()
    counts = await _import(tracker_row("Initrode", "Lead", status="rejected", fit=2, notes="the tracker's"))
    assert counts == _counts(rows=1, already_there=1, left_as_in_app=1)
    role = (await _roles())["Lead"]
    assert (role.status, role.fit, role.notes, role.origin) == ("second_impression", 9, "the scout's", "scout")


async def test_a_role_that_moves_on_in_the_file_says_why_it_ended_each_time(js):
    async def now(status, **more):
        await _import(tracker_row("Initrode", "Lead", status=status, **more))
        role = (await _roles())["Lead"]
        return role.status, role.ended_reason, role.screened_out_reason

    assert await now("new") == ("landing_zone", None, None)
    assert await now("rejected", notes="[email-sync 2026-09-30] rejection email: no") == (
        "rejected", "they_said_no", None)
    assert await now("dead") == ("gone_quiet", "unknown", None)
    status, ended, why = await now("rejected", fit="REJECTED - trajectory gate")
    assert (status, ended) == ("gated_out", None) and why == "Screened out in your old tracker: trajectory gate."
    assert await now("applied") == ("applied", None, None), "back in play: no end reason, no screen-out reason"


async def test_a_role_from_the_earlier_import_is_brought_into_the_new_words(js):
    """How the first import stored a row: the plugin's fit as the fit, its tier as the way of working, its estimate as
    posted pay, a gate's no as the employer's."""
    async with db.session_factory()() as session:
        session.add_all([
            Opportunity(owner=OWNER, slug="a", company="Initrode", title="Estimated", status="second_impression", fit=7,
                        move_type="promotion", comp="est. $90K-$120K", comp_source="estimate", origin="tracker",
                        fit_evidence="base 4", match_key="initrode|estimated"),
            Opportunity(owner=OWNER, slug="b", company="Initrode", title="Gated", status="rejected", fit=None,
                        ended_reason="they_said_no", origin="tracker", match_key="initrode|gated")])
        await session.commit()
    counts = await _import(
        tracker_row("Initrode", "Estimated", fit=7, moveType="promotion", comp="est. $90K-$120K", compSource="estimate",
                    fitEvidence="base 4"),
        tracker_row("Initrode", "Gated", status="rejected", fit="REJECTED - trajectory gate"))
    assert counts == _counts(rows=2, already_there=2, updated=2)
    roles = await _roles()
    estimated, gated = roles["Estimated"], roles["Gated"]
    assert (estimated.fit, estimated.move_type, estimated.comp) == (None, None, None)
    assert estimated.fit_evidence == (
        "Your old tracker scored it 7, on a scale of its own that this app does not use. base 4 "
        "Pay, your old tracker's estimate and not posted: est. $90K-$120K")
    assert (gated.status, gated.ended_reason, gated.screened_out_by) == ("gated_out", None, "other")
