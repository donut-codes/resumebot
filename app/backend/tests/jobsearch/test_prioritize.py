"""Prioritization, the fifth stage (docs/PRD.md 39.20 to 39.22): a role's score is worked out last, by code, from the
answers kept on it and what research found, and a role has none until it is researched; saving Prioritization works
every score out again, with no Claude, and says in one line what it did; an answer is kept under the ask's id and the
words it was given for, so a reworded or new ask is asked again only where it is needed; and a percent stays the one
the score was worked out at. Every name, number and sentence is made up."""

import time
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import event, select

from app import db
from app.jobsearch import judged, stages
from app.jobsearch.employers import core as employers
from app.jobsearch.fit import freshness, scoring
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import JobPosting, Opportunity
from app.jobsearch.pipeline import row as role_row
from app.jobsearch.profile import schema as p
from app.jobsearch.profile import scoring as ps
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis, prioritize
from app.models.work import TrailEvent
from tests.builders import findings, save_documents
from tests.support import EXAMPLE_SYSTEM, FIVE, GRADED_ON, OWNER, PROFILE, answered, bare, load_profile

NOW = datetime.now(UTC)
#: The made-up profile's settings.
SETTINGS = {**PROFILE["settings"]}
SAYS = {a["id"]: a["says"] for a in FIVE["asks"]}
#: Two target roles with no Responsibilities written, so the ask that follows them (docs/PRD.md 31.6) is for neither.
TABS = {"company": "A public agency.", "target_roles": [
    {"id": "builders", "name": "Builders", "titles": ["Director"], "description": "Builds programs."},
    {"id": "analysts", "name": "Analysts", "titles": ["Analyst"], "description": "Looks at programs."}]}
#: A fourth ask, for the role tab `builders` only.
GRANTS = {"id": "ask-d", "section": "preferred", "says": "The work includes writing grants", "points": 1,
          "roles": ["builders"]}


FILLER = " The role leads the work, sets priorities, manages the team and reports to the executive team." * 4

def five(**more) -> p.Profile:
    return p.Profile.model_validate({"scoring": {**FIVE, **more}})


def reworded(ask_id: str, says: str, *extra: dict) -> dict:
    """FIVE with one ask in other words (and any asks added)."""
    return {"asks": [{**a, "says": says} if a["id"] == ask_id else a for a in FIVE["asks"]] + list(extra)}


async def _profile(session, scoring_doc=FIVE, settings=None, **parts) -> p.Profile:
    await load_profile(session, OWNER, scoring=scoring_doc, settings=settings or SETTINGS, **parts)
    return await profile_store.current(session, OWNER)


async def _role(session, profile, company, said=("met", "met", "not"), *, researched=False, legacy=False, prob=None,
                status=stages.SECOND_IMPRESSION, target=None, route=judged.READER, posting=True, title="Programs Lead",
                answers=None, version=1) -> Opportunity:
    """A role a reader has read, with its posting. Its answers are the made-up ones (`said`, or `answers` as given);
    `researched` scores it, as research does; `legacy` stores its score the way it was kept before the answers were kept
    apart (rows that score them, no words); `route` is where its posting went (a reader's, or the title check's)."""
    employer = await employers.find_or_make(session, OWNER, company)
    given = answers if answers is not None else answered(*said)["asks"]
    role = Opportunity(
        owner=OWNER, slug=company.lower(), company=company, title=title, status=status, origin="scout",
        match_key=match_key(company, title), found=date.today(), employer_id=employer.id, target_role=target,
        location="Remote, US", move_type="remote", url=f"https://jobs.example.test/{company.lower()}",
        fit_breakdown={"answers": scoring.record([], given, profile, "reader")},
        analysis_state=prioritize.RESEARCHED if researched else None,
        analysis={"required": [{"item": "Five years", "class": "P", "by": "analyst"}]} if researched else {})
    session.add(role)
    await session.flush()
    if legacy:
        rows = scoring.compute({"asks": given}, profile, target_role=target)
        role.fit_breakdown, role.fit = rows, rows["total"]
    elif researched:
        prioritize.work_out(role, profile, employer=employer, version=version)
    if posting:
        session.add(JobPosting(
            owner=OWNER, employer_id=employer.id, ats=EXAMPLE_SYSTEM, ext_id=f"R-{company}", company=company,
            title=title, match_key=match_key(company, title), url=role.url, location="Remote", work_mode="remote",
            text=f"The whole posting for {company}." + FILLER,
            route=route, judged_by=judged.TITLE_CHECK, first_seen=NOW,
            last_seen=NOW, filtered_at=NOW, links=[], fit_prob=prob, opportunity_id=role.id, target_role=target))
    return role


async def _scores() -> dict[str, tuple[int | None, int | None]]:
    """Each role's fit and the percent it shows, by company."""
    async with db.session_factory()() as session:
        span = scoring.bounds(await profile_store.current(session, OWNER))
        return {r.company: (r.fit, scoring.percent_of(r, span))
                for r in (await session.scalars(select(Opportunity))).all()}


# --- the words an answer was given for ------------------------------------------------------------------------------


def test_an_answer_is_kept_under_the_asks_id_and_words_and_one_to_other_words_stays_on_record_unused():
    prof = five()
    record = scoring.record([], answered("met", "met", "not")["asks"], prof, "reader")
    assert [(a["id"], a["says"], a["answer"], a["by"]) for a in record] == [
        ("ask-a", SAYS["ask-a"], "met", "reader"), ("ask-b", SAYS["ask-b"], "met", "reader"),
        ("ask-c", SAYS["ask-c"], "not", "reader")]
    assert scoring.compute({"answers": record}, prof)["total"] == 4

    other = p.Profile.model_validate({"scoring": reworded("ask-a", "The work is running programs")})
    breakdown = scoring.compute({"answers": record}, other)
    rows = {r["id"]: r for r in breakdown["asks"]}
    assert (rows["ask-a"]["answer"], rows["ask-a"]["evidence"], rows["ask-a"]["earned"]) == (
        "unknown", "not answered in its present words", 0.0), "the old answer is not used: the ask counts as can't tell"
    assert rows["ask-b"]["answer"] == "met" and rows["ask-c"]["answer"] == "not"
    assert breakdown["total"] == 2, "and can't tell earns nothing"
    assert (record[0]["answer"], record[0]["says"]) == ("met", SAYS["ask-a"]), "and it stays on record"

    both = scoring.record(record, [{"id": "ask-a", "answer": "partly", "evidence": "now"}], other, "analyst")
    assert [(a["says"], a["answer"], a["by"]) for a in both if a["id"] == "ask-a"] == [
        (SAYS["ask-a"], "met", "reader"), ("The work is running programs", "partly", "analyst")]
    assert {r["id"]: r["answer"] for r in scoring.compute({"answers": both}, other)["asks"]}["ask-a"] == "partly"
    again = scoring.record(both, [{"id": "ask-a", "answer": "not", "evidence": "later"}], other, "analyst")
    assert [a["answer"] for a in again if a["id"] == "ask-a"] == ["met", "not"], "the same words answered again"
    assert scoring.wording("  The work   is  running programs ") == "The work is running programs", "no change"
    # An ask that is unchanged is asked of nobody again, and an answer to an ask the owner no longer has is kept.
    gone = p.Profile.model_validate({"scoring": {"asks": FIVE["asks"][:2]}})
    assert len(scoring.record(record, [], gone, "reader")) == 3


def test_an_answer_kept_before_the_words_were_counts_for_the_words_the_ask_has_now():
    prof = five()
    report = answered("met", "partly", "not")
    old = scoring.compute(report, prof)  # a score as it was kept: rows, and no answers of their own
    assert "answers" not in old
    answers = scoring.answers_in(old)
    assert [(a["id"], a["answer"], "says" in a) for a in answers] == [
        ("ask-a", "met", False), ("ask-b", "partly", False), ("ask-c", "not", False)]
    assert scoring.compute({"answers": answers}, prof)["total"] == old["total"], "they are in use as they were"
    # What the app decided, and what nobody answered, is no answer.
    with_age = p.Profile.model_validate({"scoring": {"asks": [*FIVE["asks"], {
        "id": "aging", "section": "posting", "says": "Posted long ago", "points": -1, "days": 30,
        "checked_by": "app"}]}})
    rows = scoring.compute({"asks": answered("met")["asks"][:1]}, with_age, facts={"age_days": 40})
    assert {r["id"]: r["by"] for r in rows["asks"]}["aging"] == "app"
    assert [a["id"] for a in scoring.answers_in(rows)] == ["ask-a"]
    # Taken to be for the words the ask had when they were stamped, they are not used for other words.
    stamped = scoring.stamp(answers, prof)
    assert [a["says"] for a in stamped] == [SAYS["ask-a"], SAYS["ask-b"], SAYS["ask-c"]]
    other = p.Profile.model_validate({"scoring": reworded("ask-b", "The role manages a team of five")})
    rows = {r["id"]: r for r in scoring.compute({"answers": stamped}, other)["asks"]}
    assert (rows["ask-a"]["answer"], rows["ask-b"]["answer"]) == ("met", "unknown")


def test_the_percent_is_of_the_points_the_role_was_scored_against_so_editing_the_asks_cannot_move_it():
    prof = five()
    breakdown = scoring.compute(answered("met", "met", "not"), prof, version=3)
    role = SimpleNamespace(fit=breakdown["total"], fit_breakdown={"answers": [], **breakdown}, target_role=None)
    assert (breakdown["possible"], breakdown["version"]) == (5, 3)
    bigger = p.Profile.model_validate({"scoring": {"asks": [*FIVE["asks"], {
        "id": "ask-d", "section": "preferred", "says": "Writes grants", "points": 4}]}})
    assert scoring.bounds(bigger)[1] == 9
    assert scoring.percent_of(role, scoring.bounds(prof)) == scoring.percent_of(role, scoring.bounds(bigger)) == 80
    waiting = SimpleNamespace(fit=None, fit_breakdown={"answers": []}, target_role=None)
    assert scoring.percent_of(waiting, scoring.bounds(prof)) is None
    # A score kept before it named its points is judged against the asks as they are.
    plain = SimpleNamespace(fit=4, fit_breakdown={"asks": [], "total": 4}, target_role=None)
    assert scoring.percent_of(plain, scoring.bounds(prof)) == 80
    assert scoring.percent_of(plain, scoring.bounds(bigger)) == 44


# --- the score comes last ---------------------------------------------------------------------------------------------


async def test_a_role_not_yet_researched_has_no_score_on_its_card_and_a_researched_one_shows_its_score(js):
    async with db.session_factory()() as session:
        profile = await _profile(session)
        await _role(session, profile, "Unlikely", prob=0.2)
        await _role(session, profile, "Likeliest", prob=0.9)
        await _role(session, profile, "Unrated", prob=None)
        await _role(session, profile, "Middling", prob=0.5)
        await _role(session, profile, "Researched", researched=True, prob=0.1)
        await session.commit()
    async with db.session_factory()() as session:
        columns = {c["key"]: c["cards"] for c in await role_row.columns(session, OWNER)}
    by = {c["company"]: c for column in columns.values() for c in column}
    assert set(by) == {"Likeliest", "Middling", "Unlikely", "Unrated", "Researched"}, "every role is on the board"
    assert by["Researched"]["fit_pct"] == 80
    for company in ("Likeliest", "Middling", "Unlikely", "Unrated"):
        assert by[company]["fit_pct"] is None, company


ASKS_WITH_APP = {"asks": [*FIVE["asks"],
                          {"id": "small", "section": "company", "says": "Under 1,000 people", "points": 1,
                           "fact": "employees", "op": "<", "value": 1000},
                          {"id": "tier", "section": "compensation", "says": "The ask is over 100,000", "points": 1,
                           "amount": 100000, "checked_by": "app"},
                          {"id": "gaps", "section": "qualifications", "says": "Each unproven required item",
                           "points": -1, "per_item": True, "limit": -3, "from_classes": "U"}]}


async def test_research_works_the_score_out_from_the_stored_answers_and_what_it_found_and_the_same_inputs_agree(js):
    async with db.session_factory()() as session:
        profile = await _profile(session, ASKS_WITH_APP)
        await save_documents(session)
        role = await _role(session, profile, "Fabrikam", prob=0.5)
        await analysis.start(session, OWNER, role)
        await session.commit()
        role_id = role.id
        assert role.fit is None and set(role.fit_breakdown) == {"answers"}
    async with db.session_factory()() as session:
        # The analyst re-judges one ask with the whole posting: its answer joins the reader's, each as it was given.
        reply = await analysis.submit(session, OWNER, findings(role_id, fit_breakdown={
            "asks": [{"id": "ask-c", "answer": "met", "evidence": "the posting asks for it"}]}))
        await session.commit()
        role = await session.get(Opportunity, role_id)
    assert reply["taken"] is True and role.fit == reply["fit"] == 7 and reply["fit_pct"] == 100
    assert {a["id"]: (a["answer"], a["by"]) for a in role.fit_breakdown["answers"]} == {
        "ask-a": ("met", "reader"), "ask-b": ("met", "reader"), "ask-c": ("met", "analyst")}
    rows = {r["id"]: r for r in role.fit_breakdown["asks"]}
    assert (rows["small"]["answer"], rows["small"]["by"]) == ("met", "app"), "300 people, from the research"
    # Edited 2026-10-27 (PRD 30.10): the Unproven item earned -1.
    assert rows["gaps"]["count"] == 1 and rows["gaps"]["earned"] == 0, "one Unproven item"
    assert (role.fit_breakdown["version"], role.fit_breakdown["possible"]) == (1, 7)
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        profile = await profile_store.current(session, OWNER)
        employer = await session.get(employers.Employer, role.employer_id)
        first = dict(role.fit_breakdown)
        prioritize.work_out(role, profile, employer=employer, version=1)
        assert role.fit_breakdown == first, "the same inputs always give the same score"


# --- saving Prioritization works every score out again --------------------------------------------------------------


async def test_saving_prioritization_works_every_score_out_again_in_one_save_and_says_what_it_changed(client):
    async with db.session_factory()() as session:
        profile = await _profile(session)
        for company, said in (("Fabrikam", ("met", "met", "not")), ("Initech", ("met", "partly", "not")),
                              ("Hooli", ("met", "not", "not"))):
            await _role(session, profile, company, said, researched=True)
        await _role(session, profile, "Wingtip", ("met", "met", "met"))  # not researched: answers, no score
        session.add(Opportunity(  # scored before asks: left as it is
            owner=OWNER, slug="contoso", company="Contoso", title="Programs Lead", status=stages.SECOND_IMPRESSION,
            fit=3, origin="scout", match_key="contoso|programs lead", found=date.today(),
            fit_breakdown={"base": 3, "base_why": "x", "applied": [], "not_applied": [], "total": 3}))
        await session.commit()
    assert await _scores() == {"Fabrikam": (4, 80), "Initech": (2, 40), "Hooli": (2, 40), "Wingtip": (None, None),
                               "Contoso": (3, None)}
    bigger = {"asks": [{**FIVE["asks"][0], "points": 4}, *FIVE["asks"][1:]]}
    saved = await client.put("/api/v1/profile/scoring", json={"data": bigger})
    assert saved.status_code == 200
    assert bare(saved.json()) == {"saved": True, "version": 2, "said": "Worked out 4 scores again: 4 changed."}
    assert await _scores() == {"Fabrikam": (6, 86), "Initech": (4, 57), "Hooli": (4, 57), "Wingtip": (None, None),
                               "Contoso": (0, 0)}, "every affected role's percent moved, with no Claude"
    async with db.session_factory()() as session:
        roles = {r.company: r for r in (await session.scalars(select(Opportunity))).all()}
    for company in ("Fabrikam", "Initech", "Hooli"):
        assert (roles[company].fit_breakdown["version"], roles[company].fit_breakdown["possible"]) == (2, 7), company
    assert roles["Wingtip"].fit is None and set(roles["Wingtip"].fit_breakdown) == {"answers"}
    # a role scored before asks waited for a reader's answers to be scored on them; no reader is asked a line now, so it
    # is scored on the asks the app checks (none met for it yet: 0)
    assert roles["Contoso"].fit == 0 and roles["Contoso"].fit_breakdown["version"] == 2
    assert all(r.status == stages.SECOND_IMPRESSION for r in roles.values()), "the drafting bar is out of reach"
    nothing = await client.put("/api/v1/profile/scoring", json={"data": bigger})
    assert bare(nothing.json()) == {"saved": False, "version": None}, "a save that changes nothing says nothing"


async def test_a_posting_age_ask_is_decided_from_when_the_app_first_saw_the_posting_each_time_scores_are_worked_out(
    js, monkeypatch,
):
    # The clock is pinned to an evening in the Americas, when the owner's day (UTC here) is already the next one and the
    # machine's own date may not be: the roles' ages are worked from this instant and day, never from the real ones.
    now = datetime(2026, 10, 15, 3, 0, tzinfo=UTC)
    monkeypatch.setattr(prioritize, "owner_today", lambda: now.date())
    monkeypatch.setattr(freshness, "utc_now", lambda: now)
    within = {"id": "fresh", "section": "posting", "says": "Posted within the last day", "points": 3,
              "checked_by": "app", "days": 1, "above": False}
    async with db.session_factory()() as session:
        profile = await _profile(session, {"asks": [*FIVE["asks"], within]})
        for company, hours in (("Seen-30-hours-ago", 30), ("Seen-5-hours-ago", 5)):
            role = await _role(session, profile, company, researched=True)
            role.found = now.date() - timedelta(days=1)  # a date alone gives yesterday the benefit
            posting = await session.scalar(select(JobPosting).where(JobPosting.opportunity_id == role.id))
            posting.first_seen = now - timedelta(hours=hours)
        await session.commit()
        done = await prioritize.rescore(session, OWNER, profile, version=2)
        await session.commit()
        roles = {r.company: r for r in (await session.scalars(select(Opportunity))).all()}
    assert done.scored == 2
    rows = {c: {r["id"]: r for r in role.fit_breakdown["asks"]}["fresh"] for c, role in roles.items()}
    assert (rows["Seen-30-hours-ago"]["answer"], rows["Seen-5-hours-ago"]["answer"]) == ("not", "met")
    assert rows["Seen-5-hours-ago"]["evidence"] == "first seen 5 hours ago"
    assert (roles["Seen-30-hours-ago"].fit, roles["Seen-5-hours-ago"].fit) == (4, 7)


def test_the_line_a_save_says_is_one_plain_sentence_for_each_thing_it_did():
    done = prioritize.Rescored
    assert prioritize.say(done()) == ""
    assert prioritize.say(done(scored=1)) == "Worked out 1 score again: none changed."
    assert prioritize.say(done(scored=14, changed=6)) == "Worked out 14 scores again: 6 changed."
    assert prioritize.say(done(scored=2, changed=1, failed=1)) == (
        "Worked out 2 scores again: 1 changed. 1 score could not be worked out again and stays as it was.")


async def test_a_save_that_moves_scores_moves_no_role_and_a_save_that_changes_nothing_says_nothing(client):
    async with db.session_factory()() as session:
        profile = await _profile(session)
        await _role(session, profile, "Low", ("met", "not", "not"), researched=True, status=stages.READY_TO_LAUNCH)
        await _role(session, profile, "High", ("met", "met", "met"), researched=True, status=stages.READY_TO_LAUNCH)
        await session.commit()
    saved = await client.put("/api/v1/profile/scoring", json={"data": {
        "asks": [{**FIVE["asks"][0], "points": 4}, *FIVE["asks"][1:]]}})  # Low: 4 of 7 is 57%; High: 100%
    assert saved.json()["said"] == "Worked out 2 scores again: 1 changed."
    async with db.session_factory()() as session:
        roles = {r.company: r.status for r in (await session.scalars(select(Opportunity))).all()}
    assert roles == {"Low": stages.READY_TO_LAUNCH, "High": stages.READY_TO_LAUNCH}, "a score moves no role"
    again = await client.put("/api/v1/profile/scoring", json={"data": {
        "asks": [{**FIVE["asks"][0], "points": 4}, *FIVE["asks"][1:]]}})
    assert bare(again.json()) == {"saved": False, "version": None}, "a save that changes nothing says nothing"


async def test_working_every_score_out_again_reads_the_database_a_few_times_and_not_once_for_each_role(js):
    async with db.session_factory()() as session:
        profile = await _profile(session)
        for number in range(120):
            await _role(session, profile, f"Company {number}", researched=True, prob=0.5)
        await session.commit()
    selects: list[str] = []

    def watch(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    sync = db.engine().sync_engine
    event.listen(sync, "before_cursor_execute", watch)
    try:
        async with db.session_factory()() as session:
            started = time.monotonic()
            done = await prioritize.rescore(session, OWNER, profile, version=2)
            took = time.monotonic() - started
            await session.commit()
    finally:
        event.remove(sync, "before_cursor_execute", watch)
    assert done.scored == 120 and done.changed == 0 and done.failed == 0
    # A page (`PAGE`, 250 roles: this is one) is the owner's scores listed once, then the page's roles, their employers
    # and their postings together, and the lock's read of the rows to write: a few reads, whatever the number of roles.
    assert len(selects) <= 7, f"the roles, their employers and their postings are read together: {len(selects)} reads"
    assert took < 10, "seconds, not minutes"


async def test_a_role_that_changed_while_its_score_was_worked_out_is_left_alone_and_not_counted(js, monkeypatch):
    async with db.session_factory()() as session:
        profile = await _profile(session)
        await _role(session, profile, "Low", ("met", "not", "not"), researched=True)
        await _role(session, profile, "High", ("met", "met", "met"), researched=True)
        await session.commit()
    real_lock = prioritize.as_read.lock

    async def lock_after_a_change(session, model, ids, **kwargs):
        async with db.session_factory()() as other:  # the owner's own change, between the read and the write
            low = await other.scalar(select(Opportunity).where(Opportunity.company == "Low"))
            low.fit = 1
            await other.commit()
        return await real_lock(session, model, ids, **kwargs)

    monkeypatch.setattr(prioritize.as_read, "lock", lock_after_a_change)
    async with db.session_factory()() as session:
        bigger = five(asks=[{**FIVE["asks"][0], "points": 4}, *FIVE["asks"][1:]])
        done = await prioritize.rescore(session, OWNER, bigger, version=2)
        await session.commit()
    assert (done.scored, done.changed) == (1, 0), "Low was not written, so it is neither scored nor changed"
    assert (await _scores())["Low"][0] == 1, "the change made meanwhile stands"


async def test_a_save_that_moves_no_percent_says_none_changed_and_a_restored_version_works_the_scores_out_too(client):
    async with db.session_factory()() as session:
        profile = await _profile(session)
        await _role(session, profile, "Fabrikam", researched=True)
        await session.commit()
    notes = {"notes": [{"id": "n", "text": "A note.", "source": "somewhere"}]}
    saved = await client.put("/api/v1/profile/scoring", json={"data": {**FIVE, **notes}})
    assert bare(saved.json()) == {"saved": True, "version": 2, "said": "Worked out 1 score again: none changed."}
    bigger = {"asks": [{**FIVE["asks"][0], "points": 4}, *FIVE["asks"][1:]]}
    await client.put("/api/v1/profile/scoring", json={"data": bigger})
    assert (await _scores())["Fabrikam"] == (6, 86)
    back = await client.post("/api/v1/profile/scoring/versions/1/restore")
    assert bare(back.json()) == {"restored": 1, "version": 4, "said": "Worked out 1 score again: 1 changed."}
    assert (await _scores())["Fabrikam"] == (4, 80)


# --- an ask that is new or in new words goes back to a reader only where it is needed --------------------------------


async def _tabbed(session) -> p.Profile:
    """Roles on two tabs and none, each a reader has read and answered the three asks for; one a title check only judged
    (no reader was routed to it, but it is in play with its text kept, so it is asked like the rest); and three that
    are not asked: one gated out, one ended, one with no posting at all."""
    profile = await _profile(session, criteria=TABS)
    await _role(session, profile, "B-Unlikely", target="builders", prob=0.2)
    await _role(session, profile, "B-Likeliest", target="builders", prob=0.8)
    await _role(session, profile, "A-One", target="analysts", prob=0.6)
    await _role(session, profile, "None-One", prob=0.4)
    await _role(session, profile, "Gated", target="builders", status=stages.GATED_OUT, prob=0.9)
    await _role(session, profile, "Ended", target="builders", status=stages.WITHDREW, prob=0.9)
    await _role(session, profile, "TitlesOnly", target="builders", route=judged.TITLES, prob=0.9)
    await _role(session, profile, "NoPosting", target="builders", posting=False)
    return profile


# --- the night's readers take them, and the answers join the role ---------------------------------------------------


def _report(company: str, *ids: str, **answer_for) -> dict:
    """A reader's report of a posting it was handed: an answer to each ask named."""
    return {"company": company, "title": "Programs Lead", "fit_evidence": "None.",
            "fit_breakdown": {"asks": [{"id": i, "answer": answer_for.get(i, "met"), "evidence": f"for {i}"}
                                       for i in ids]}}


# --- a score moves no role --------------------------------------------------------------------------------------------


async def test_a_save_of_prioritization_works_the_scores_out_and_moves_no_role_to_another_stage(client):
    async with db.session_factory()() as session:
        profile = await _profile(session)
        await _role(session, profile, "Waiting", ("met", "not", "met"), researched=True)  # 60%
        await _role(session, profile, "Unresearched", ("met", "met", "met"))
        await _role(session, profile, "Later", ("met", "not", "met"), researched=True, status=stages.READY_TO_LAUNCH)
        await session.commit()
    saved = await client.put("/api/v1/profile/scoring", json={"data": {
        "asks": [{**FIVE["asks"][0], "points": 4}, *FIVE["asks"][1:]]}})  # 5 of 7: 71%, over the bar
    assert saved.json()["said"] == "Worked out 2 scores again: 2 changed."
    assert (await _scores())["Waiting"] == (5, 71)
    async with db.session_factory()() as session:
        roles = {r.company: r for r in (await session.scalars(select(Opportunity))).all()}
        moved = list(await session.scalars(select(TrailEvent).where(TrailEvent.event.like("%bar%"))))
    assert {c: r.status for c, r in roles.items()} == {
        "Waiting": stages.SECOND_IMPRESSION, "Unresearched": stages.SECOND_IMPRESSION,
        "Later": stages.READY_TO_LAUNCH}, "the new numbers move no role"
    assert moved == [], "and nothing in the history says one reached a bar"


# --- the role page ----------------------------------------------------------------------------------------------------


async def test_the_role_page_shows_the_answers_of_a_role_with_no_score_and_a_score_with_its_version(client):
    async with db.session_factory()() as session:
        profile = await _profile(session)
        waiting = await _role(session, profile, "Wingtip", ("met", "unknown", "not"))
        scored = await _role(session, profile, "Fabrikam", ("met", "met", "not"), researched=True, version=1)
        await session.commit()
        ids = (waiting.id, scored.id)
    page = (await client.get(f"/api/v1/roles/{ids[0]}")).json()["scoring"]
    assert (page["kind"], page["total"], page["percent"], page["version"]) == ("asks", None, None, None)
    assert {a["id"]: (a["state"], a["answer"], a["earned"]) for a in page["asks"]} == {
        "ask-a": ("checked", "met", None), "ask-b": ("unknown", "unknown", None), "ask-c": ("checked", "not", None)}
    page = (await client.get(f"/api/v1/roles/{ids[1]}")).json()["scoring"]
    assert (page["total"], page["percent"], page["possible"], page["version"]) == (4, 80, 5, 1)
    assert {a["id"]: a["earned"] for a in page["asks"]} == {"ask-a": 2, "ask-b": 2, "ask-c": 0}


async def test_the_role_page_says_how_much_is_decided_and_each_requirement_the_app_graded(client):
    async with db.session_factory()() as session:
        profile = await _profile(session, GRADED)
        role = await _role(session, profile, "Fabrikam", ("met", "met", "not"), researched=True, version=1)
        role.analysis = {"required": [{"item": "Five years", "class": "P", "by": "analyst"},
                                      {"item": "A licence", "class": "U", "by": "analyst"}],
                         "preferred": [{"item": "A second language", "class": "T", "by": "analyst"}]}
        prioritize.work_out(role, profile, employer=None, version=1)
        await session.commit()
        role_id = role.id
    body = (await client.get(f"/api/v1/roles/{role_id}")).json()
    page = body["scoring"]
    everything = page["possible"]
    assert (page["decided"], page["possible"], page["coverage"]) == (everything, everything, 100)
    assert "floor" not in page and "prob" not in page, "no floor to a fit and no probability in it"
    assert page["math"] == scoring.MATH and page["percent"] is not None
    graded = next(a for a in page["asks"] if a.get("kind") == "grade")
    assert graded["required"] == {"proven": 1, "transferable": 0, "unproven": 1}
    assert graded["preferred"] == {"proven": 0, "transferable": 1, "unproven": 0}
    assert graded["items"] == [
        {"side": "required", "item": "Five years", "class": "P", "points": 2},
        {"side": "required", "item": "A licence", "class": "U", "points": -2},
        {"side": "preferred", "item": "A second language", "class": "T", "points": 1}]
    assert [r["item"] for r in body["preferred"]] == ["A second language"], "the Requirements view lists them apart"
    assert [r["item"] for r in body["requirements"]] == ["Five years", "A licence"]


async def test_the_best_each_requirement_can_add_is_the_best_positive_number_or_zero_and_none_when_not_graded(client):
    async def best(doc: dict) -> dict:
        assert (await client.put("/api/v1/profile/scoring", json={"data": doc})).status_code == 200
        return (await client.get("/api/v1/profile")).json()["grading_best"]
    assert await best(FIVE) == {"required": 0, "preferred": 0}, "not graded: nothing to add"
    assert await best(GRADED) == {"required": 2, "preferred": 1}
    low = {"on": True, "required_proven": -1, "required_transferable": 0, "required_unproven": -3,
           "preferred_proven_or_transferable": 4, "preferred_unproven": 7}
    assert await best({**FIVE, "grade_requirements": low}) == {"required": 0, "preferred": 7}
    assert await best({**FIVE, "grade_requirements": {**low, "on": False}}) == {"required": 0, "preferred": 0}


def test_the_analysis_state_the_scores_wait_for_is_the_analysts():
    assert prioritize.RESEARCHED == analysis.DONE


# --- the requirements the analyst classed, graded by the app when the owner asks (docs/PRD.md 30.9, rule 13) ---------

GRADED = {**FIVE, "grade_requirements": GRADED_ON}


def _graded_row(role: Opportunity) -> dict:
    return next(r for r in role.fit_breakdown["asks"] if r["id"] == ps.GRADE_ID)


async def test_work_out_hands_both_lists_of_the_analysts_classes_to_the_graded_row(js):
    async with db.session_factory()() as session:
        profile = await _profile(session, GRADED)
        role = await _role(session, profile, "Fabrikam", researched=True)
        role.analysis = {"required": [{"item": "a", "class": "P"}, {"item": "b", "class": "U"}],
                         "preferred": [{"item": "c", "class": "T"}, {"item": "d", "class": "U"}]}
        assert prioritize.work_out(role, profile, employer=None, version=1) is True
        row = _graded_row(role)
    assert row["required"] == {"proven": 1, "transferable": 0, "unproven": 1}
    assert row["preferred"] == {"proven": 0, "transferable": 1, "unproven": 1}
    assert row["earned"] == 1.0, "2 for the Proven, 2 off for the Unproven, 1 for the Transferable preferred"


async def test_a_role_with_no_preferred_classes_is_graded_on_its_required_items_alone(js):
    async with db.session_factory()() as session:
        profile = await _profile(session, GRADED)
        role = await _role(session, profile, "Fabrikam", researched=True)  # one Proven required item, none preferred
        row = _graded_row(role)
    assert row["required"] == {"proven": 1, "transferable": 0, "unproven": 0}
    assert row["preferred"] == {"proven": 0, "transferable": 0, "unproven": 0} and row["earned"] == 2.0


async def test_a_role_researched_with_its_posting_not_read_earns_nothing_from_the_graded_row_and_says_so(js):
    async with db.session_factory()() as session:
        profile = await _profile(session, GRADED)
        role = await _role(session, profile, "Fabrikam", researched=True)
        role.analysis = {"required": [], "posting_not_read": True}
        assert prioritize.work_out(role, profile, employer=None, version=1) is True
        row = _graded_row(role)
    assert (row["points"], row["earned"]) == (0, 0.0)
    assert row["evidence"] == ("from the research: required 0 Proven, 0 Transferable, 0 Unproven; "
                               "preferred 0 Proven, 0 Transferable, 0 Unproven")


async def test_the_owner_correcting_a_required_items_class_works_the_graded_row_out_again(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=GRADED)
        profile = await profile_store.current(session, OWNER)
        before = scoring.compute(answered(), profile, classes=["U"])
        role = Opportunity(owner=OWNER, slug="fab", company="Fabrikam", title="Programs Lead", fit=before["total"],
                           origin="scout", match_key="fabrikam|programs lead", status=stages.SECOND_IMPRESSION,
                           found=date.today(), fit_breakdown=before,
                           analysis={"required": [{"item": "A", "class": "U", "by": "analyst"}]})
        session.add(role)
        await session.flush()
        assert _graded_row(role)["earned"] == -2.0
        await analysis.correct_class(session, OWNER, role.id, 0, "P", "I have done this")
        assert (_graded_row(role)["earned"], _graded_row(role)["required"]["proven"]) == (2.0, 1)


async def test_a_save_that_ticks_the_box_works_every_score_out_again_and_unticking_it_returns_every_score(client):
    async with db.session_factory()() as session:
        profile = await _profile(session)
        await _role(session, profile, "Fabrikam", ("met", "met", "not"), researched=True)
        await _role(session, profile, "Initech", ("met", "partly", "not"), researched=True)
        await session.commit()
    plain = await _scores()
    ticked = await client.put("/api/v1/profile/scoring", json={"data": GRADED})
    assert ticked.json()["said"] == "Worked out 2 scores again: 2 changed.", "in the one request, with no Claude"
    assert (await _scores())["Fabrikam"] == (6, 86), "4 of 5 became 6 of 7: one Proven required item earns 2"
    unticked = await client.put("/api/v1/profile/scoring", json={"data": FIVE})
    assert unticked.json()["said"] == "Worked out 2 scores again: 2 changed."
    assert await _scores() == plain, "unticking returns every score to what it was"
