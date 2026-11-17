"""A posting within the last so many days (docs/PRD.md path 40, 30.6): the one rule for how fresh a posting is (under
N x 24 hours old, told from the time the app first saw it when it has that, else from a date), the ask on a posting's
age that goes either way, its validation and its page words, and where the app gets the time from when a role is first
scored and when the analyst scores it again. Every name, number and sentence is made up."""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch import stages
from app.jobsearch.fit import freshness, scoring
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.missions import search_run as run
from app.jobsearch.models import JobPosting, Opportunity, SearchRun
from app.jobsearch.profile import schema as p
from app.jobsearch.profile import scoring as ps
from app.jobsearch.profile import views as pv
from app.jobsearch.research import analysis
from app.timeutil import owner_today
from tests.builders import findings, save_documents, score_like_research, submit_findings
from tests.support import EXAMPLE_SYSTEM, FIVE, OWNER, POSTING_WORDS, answered, bare, load_profile, pin_clock, reported

#: "Posted within the last day", for every role, in Job posting: the shape the owner's ask is saved in.
WITHIN_A_DAY = {"id": "posted-within-1-day", "section": "posting", "says": "Posted within the last day", "points": 3,
                "checked_by": "app", "days": 1, "above": False}
#: Five asks worth 5 and the one above worth 3 more.
SCORING = {"asks": [*FIVE["asks"], WITHIN_A_DAY]}
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def _ago(**delta) -> datetime:
    return NOW - timedelta(**delta)


# --- the rule ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("days", "hours", "within"), [
    (1, 0.0, True), (1, 5.0, True), (1, 23.0, True), (1, 23.99, True),
    (1, 24.0, False),  # 24 hours old is not under 24 hours
    (1, 24.01, False), (1, 30.0, False), (1, 400.0, False),
    (2, 47.99, True), (2, 48.0, False), (3, 71.99, True), (3, 72.0, False),
])
def test_with_the_time_the_app_first_saw_a_posting_within_n_days_is_under_n_times_24_hours(days, hours, within):
    assert freshness.within(days, None, hours) is within


@pytest.mark.parametrize(("days", "age", "within"), [
    (1, 0, True), (1, 1, True),  # dated today or yesterday
    (1, 2, False), (1, 9, False),
    (3, 0, True), (3, 3, True), (3, 4, False),
])
def test_with_only_a_date_a_posting_is_within_n_days_when_dated_today_or_up_to_n_days_back(days, age, within):
    assert freshness.within(days, age, None) is within


def test_the_employers_date_can_say_no_where_the_time_says_yes_and_the_time_where_the_date_says_yes():
    # Seen an hour ago, but the employer dated it last week: a board read for the first time shows every posting as new.
    assert freshness.within(1, 7, 1.0) is False
    # Dated today, but the app has had it for two days: the time is the surer word.
    assert freshness.within(1, 0, 48.0) is False
    assert freshness.within(1, 1, 30.0) is False, "dated yesterday is not enough when the hours say 30"
    # Both agree.
    assert freshness.within(1, 0, 2.0) is True and freshness.within(1, 1, 23.5) is True


def test_with_neither_a_time_nor_a_date_the_app_cannot_tell():
    assert freshness.within(1, None, None) is None
    assert freshness.age_hours(None, None) is None


@pytest.mark.parametrize("days", [1, 2, 3, 5, 30])
def test_on_a_date_alone_over_and_within_are_never_both_true_and_never_both_false(days):
    for age in range(0, 40):
        assert (age > days) is not freshness.within(days, age, None), (days, age)


def test_the_hours_since_the_app_first_saw_a_posting():
    assert freshness.hours_since(None, NOW) is None
    assert freshness.hours_since(_ago(hours=5), NOW) == 5.0
    assert freshness.hours_since(_ago(hours=24, minutes=1), NOW) == pytest.approx(24 + 1 / 60)
    assert freshness.hours_since(NOW + timedelta(minutes=3), NOW) == 0.0, "a clock a moment ahead is 0, never negative"
    naive = _ago(hours=2).replace(tzinfo=None)  # a test database hands times back without a zone: taken as UTC
    assert freshness.hours_since(naive, NOW) == 2.0


def test_a_postings_age_in_hours_is_the_later_of_the_time_the_app_first_saw_it_and_its_date():
    age = freshness.age_hours
    assert age(None, 5.0) == 5, "the time alone"
    assert age(None, 5.99) == 5 and age(None, 0.4) == 0, "in whole hours: the finds of one search are minutes apart"
    assert age(0, None) == 0 and age(1, None) == 24 and age(9, None) == 216, "a date alone, in whole days"
    assert age(0, 5.0) == 5 and age(1, 5.0) == 24, "the date says older where the time says new"
    assert age(0, 30.0) == 30 and age(1, 48.0) == 48, "and the time says older where the date does"
    # A board read for the first time shows a posting the employer dated last week as first seen now.
    assert age(7, 0.0) == 168


def test_postings_go_youngest_first_by_the_time_else_the_employers_date_else_the_day_found_and_undated_last():
    today = date(2026, 10, 13)
    yesterday, last_week = today - timedelta(days=1), today - timedelta(days=7)
    postings = {
        "seen 5h": (None, today, _ago(hours=5)),
        "posted today": (today, today, None),
        "found today": (None, today, None),
        "seen 23h59": (None, today, _ago(hours=23, minutes=59)),
        "posted yesterday": (yesterday, today, None),
        "found yesterday": (None, yesterday, None),
        "seen 25h": (None, today, _ago(hours=25)),
        "seen 1h, posted last week": (last_week, today, _ago(hours=1)),
        "seen 1h, found last week": (None, last_week, _ago(hours=1)),
        "undated": (None, None, None),
    }
    ordered = sorted(postings, key=lambda name: (*freshness.youngest_first(*postings[name], today, NOW), name))
    assert ordered == ["found today", "posted today", "seen 5h", "seen 23h59", "found yesterday", "posted yesterday",
                       "seen 25h", "seen 1h, found last week", "seen 1h, posted last week", "undated"]
    assert freshness.youngest_first(None, None, None, today, NOW) == (True, 0)
    assert freshness.youngest_first(today, None, None, today, NOW) == (False, 0), "dated today is not undated"
    assert freshness.youngest_first(None, today, _ago(hours=5), today, NOW) == (False, 5)
    # Two finds of one search, seconds apart, are the same age: the queue rule decides between them, not the seconds.
    assert (freshness.youngest_first(None, today, _ago(minutes=62, seconds=3), today, NOW)
            == freshness.youngest_first(None, today, _ago(minutes=61, seconds=40), today, NOW))


def test_the_words_say_which_fact_decided_the_answer():
    said = freshness.evidence
    assert said(1, None, 5.2) == "first seen 5 hours ago"
    assert said(1, 0, 0.4) == "first seen under an hour ago" and said(1, 0, 1.5) == "first seen 1 hour ago"
    assert said(1, 0, 30.0) == "first seen 30 hours ago" and said(1, None, 75.0) == "first seen 3 days ago"
    assert said(1, 0, None) == "posted today" and said(1, 1, None) == "posted yesterday"
    assert said(3, 2, None) == "posted 2 days ago"
    assert said(1, 8, 1.0) == "posted 8 days ago", "the employer's date said no, so it is the date that is quoted"
    assert said(1, 1, None, "found") == "found yesterday" and said(1, 8, 1.0, "found") == "found 8 days ago"
    assert said(1, None, None) == ""
    assert freshness.facts(date(2026, 10, 1), _ago(hours=3), NOW) == {"seen_hours": 3.0, "age_from": "posted"}
    assert freshness.facts(None, None, NOW) == {"seen_hours": None, "age_from": "found"}


# --- where the time comes from ----------------------------------------------------------------------------------------


def _posting(company: str, first_seen: datetime, *, owner: str = OWNER, role: uuid.UUID | None = None,
             title: str = "Programs Lead") -> JobPosting:
    return JobPosting(owner=owner, employer_id=uuid.uuid4(), ats=EXAMPLE_SYSTEM, ext_id=str(uuid.uuid4()),
                      company=company, title=title, match_key=match_key(company, title), first_seen=first_seen,
                      last_seen=first_seen, links=[], opportunity_id=role)


async def test_the_time_is_when_the_app_first_saw_the_posting_a_role_was_made_from_the_earliest_of_several(js):
    async with db.session_factory()() as session:
        linked = Opportunity(
            description=POSTING_WORDS,
            owner=OWNER, slug="a", company="Fabrikam", title="Programs Lead", origin="scout",
                             match_key=match_key("Fabrikam", "Programs Lead"), status=stages.SECOND_IMPRESSION)
        alone = Opportunity(
            description=POSTING_WORDS,
            owner=OWNER, slug="b", company="Initech", title="Programs Lead", origin="scout",
                            match_key=match_key("Initech", "Programs Lead"), status=stages.SECOND_IMPRESSION)
        session.add_all([linked, alone])
        await session.flush()
        session.add_all([
            _posting("Fabrikam", _ago(hours=10), role=linked.id), _posting("Fabrikam", _ago(hours=40), role=linked.id),
            # Waiting to become a role: counted by its key, the earliest of two.
            _posting("Hooli", _ago(hours=7)), _posting("Hooli", _ago(hours=20)),
            # Another owner's, and one already a role: not in the answer for a role about to be made.
            _posting("Hooli", _ago(hours=90), owner="someone-else"),
            _posting("Fabrikam", _ago(hours=3), role=linked.id),
        ])
        await session.flush()
        hooli, fabrikam = match_key("Hooli", "Programs Lead"), match_key("Fabrikam", "Programs Lead")
        assert await freshness.first_seen_by_key(session, OWNER, []) == {}
        assert await freshness.first_seen_by_key(session, OWNER, [hooli, fabrikam, "nobody|nothing"]) == {
            hooli: _ago(hours=20)}, "a posting already made a role has no place here"
        assert await freshness.first_seen_by_role(session, OWNER) == {linked.id: _ago(hours=40)}
        assert await freshness.first_seen_of(session, linked.id) == _ago(hours=40)
        assert await freshness.first_seen_of(session, alone.id) is None, "a role with no posting has no time"


# --- the ask ----------------------------------------------------------------------------------------------------------


def _ask(**more) -> ps.Ask:
    return ps.Ask(id="x", section="posting", says="Posted within the last day", points=3, checked_by="app", **more)


def test_an_ask_on_a_postings_age_goes_over_by_default_and_within_with_above_false():
    over, within = _ask(days=30), _ask(days=1, above=False)
    assert over.above is True and within.above is False
    assert over.mechanism() == within.mechanism() == "age"
    assert ps.Ask.model_validate({"id": "old", "says": "Old", "days": 30, "checked_by": "app"}).above is True, (
        "an ask stored before there was a direction is over, as it always was")


def test_a_within_ask_needs_one_day_or_more_and_an_over_ask_keeps_any_number():
    for days in (0, -1):
        with pytest.raises(ValueError, match="needs 1 day or more"):
            _ask(days=days, above=False)
    assert _ask(days=1, above=False).days == 1
    assert _ask(days=0).days == 0 and _ask(days=-1).days == -1, "the other direction is not tightened"
    pay_under = ps.Ask(id="u", says="Ask under 90,000", points=-2, checked_by="app", amount=90_000, above=False)
    assert pay_under.days is None, "a pay tier under an amount has no days, so nothing is asked of them"


def test_the_ask_is_decided_by_the_app_and_a_pay_tier_does_not_count_it():
    prof = p.Profile.model_validate({"scoring": {"asks": [
        WITHIN_A_DAY, {"id": "pay", "section": "compensation", "says": "Ask over 100,000", "points": 1,
                       "checked_by": "app", "amount": 100_000}]}})
    assert [a.id for a in scoring.pay_tiers_met(150_000, prof)] == ["pay"], "an age ask is no pay tier"
    sections = pv.scoring_for_agent(prof.scoring)
    posting = next(s for s in sections if s["id"] == "posting")
    assert posting["asks"] == [{"id": "posted-within-1-day", "says": "Posted within the last day",
                                "effect": "adds points", "who_checks": "the app: leave it out"}]


def _row(facts: dict | None, ask: dict = WITHIN_A_DAY) -> dict:
    prof = p.Profile.model_validate({"scoring": {"asks": [ask]}})
    return scoring.compute({"asks": []}, prof, facts=facts)["asks"][0]


@pytest.mark.parametrize(("facts", "answer", "earned", "evidence"), [
    ({"seen_hours": 5.0}, "met", 3, "first seen 5 hours ago"),
    ({"seen_hours": 23.99}, "met", 3, "first seen 23 hours ago"),
    ({"seen_hours": 24.0}, "not", 0, "first seen 24 hours ago"),
    ({"seen_hours": 30.0}, "not", 0, "first seen 30 hours ago"),
    ({"age_days": 0}, "met", 3, "posted today"),
    ({"age_days": 1}, "met", 3, "posted yesterday"),
    ({"age_days": 2}, "not", 0, "posted 2 days ago"),
    ({"age_days": 1, "age_from": "found"}, "met", 3, "found yesterday"),
    ({"age_days": 12, "seen_hours": 1.0}, "not", 0, "posted 12 days ago"),
    ({"age_days": 0, "seen_hours": 26.0}, "not", 0, "first seen 26 hours ago"),
    ({"age_days": 0, "seen_hours": 2.0}, "met", 3, "first seen 2 hours ago"),
])
def test_the_app_answers_a_within_ask_from_the_hours_or_the_date(facts, answer, earned, evidence):
    row = _row(facts)
    assert (row["answer"], row["earned"], row["by"], row["evidence"]) == (answer, earned, "app", evidence)


def test_an_age_the_app_cannot_tell_leaves_the_ask_unknown_and_it_earns_nothing_like_a_penalty():
    for facts in (None, {}, {"ask": 120_000}):
        row = _row(facts)
        assert (row["answer"], row["by"], row["earned"]) == ("unknown", "app", 0), "can't tell: no partial credit"
    penalty = {**WITHIN_A_DAY, "points": -2}
    # Edited 2026-10-27 (PRD 30.10): a met -2 age ask earned -2; it earns 0 now, as an unknown one does.
    assert _row({"seen_hours": 4.0}, ask=penalty)["earned"] == 0 and _row(None, ask=penalty)["earned"] == 0


def test_an_over_ask_reads_the_days_alone_and_is_not_moved_by_the_hours():
    over = {"id": "old", "section": "posting", "says": "Posted over 3 days ago", "points": -1, "checked_by": "app",
            "days": 3}
    row = _row({"age_days": 5, "seen_hours": 1.0}, ask=over)
    assert (row["answer"], row["evidence"]) == ("met", "posted 5 days ago"), "the same words and answer as before"
    assert _row({"age_days": 3, "seen_hours": 400.0}, ask=over)["answer"] == "not"
    assert _row({"seen_hours": 400.0}, ask=over)["answer"] == "unknown", "hours alone say nothing to an over ask"
    assert _row({"age_days": 1}, ask=over)["evidence"] == "posted 1 day ago"


def test_each_age_is_over_or_within_never_both_and_never_neither_on_a_date():
    pair = p.Profile.model_validate({"scoring": {"asks": [
        {"id": "over", "section": "posting", "says": "Over 4", "points": -1, "checked_by": "app", "days": 4},
        {"id": "within", "section": "posting", "says": "Within 4", "points": 1, "checked_by": "app", "days": 4,
         "above": False}]}})
    for age in range(0, 12):
        rows = {r["id"]: r["answer"] for r in scoring.compute({"asks": []}, pair, facts={"age_days": age})["asks"]}
        assert sorted(rows.values()) == ["met", "not"], (age, rows)


# --- over HTTP -----------------------------------------------------------------------------------------------------


async def test_the_page_saves_a_within_ask_as_it_is_and_a_save_below_one_day_is_refused(client):
    saved = await client.put("/api/v1/profile/scoring", json={"data": SCORING, "why": "fresh first"})
    assert saved.status_code == 200 and saved.json()["saved"] is True
    body = (await client.get("/api/v1/profile")).json()
    stored = next(a for a in body["scoring"]["asks"] if a["id"] == "posted-within-1-day")
    assert (stored["days"], stored["above"], stored["checked_by"], stored["points"], stored["roles"]) == (
        1, False, "app", 3, [])
    assert body["fit_possible"] == 8, "the asks worth 5, and this one worth 3: it adds to what every role can earn"
    assert body["says"]["asks"]["posted-within-1-day"] == "Posted within the last day: +3"
    again = await client.put("/api/v1/profile/scoring", json={"data": body["scoring"], "why": "no change"})
    assert bare(again.json()) == {"saved": False, "version": None}, "read back, it is the same document"
    refused = await client.put("/api/v1/profile/scoring", json={"data": {"asks": [{**WITHIN_A_DAY, "days": 0}]}})
    assert refused.status_code == 422 and "needs 1 day or more" in refused.json()["detail"]
    old = await client.put("/api/v1/profile/scoring", json={"data": {"asks": [
        {**WITHIN_A_DAY, "id": "old", "days": 0, "above": True}]}})
    assert old.status_code == 200, "over 0 days is still an ask"


# --- a role about to be made, and a role the analyst scores -----------------------------------------------------------


def _answers(title: str, **said: str) -> dict:
    role = reported("Fabrikam", title)
    role.pop("fit"), role.pop("fit_evidence")
    role["fit_breakdown"] = {"asks": [{"id": f"ask-{k}", "answer": v, "evidence": f"for {k}"} for k, v in said.items()]}
    return role


def _within_row(role: Opportunity) -> dict:
    return next(a for a in role.fit_breakdown["asks"] if a["id"] == WITHIN_A_DAY["id"])


async def test_a_round_counts_a_posting_from_the_hour_the_app_first_saw_it_unless_the_employer_dated_it_earlier(
        js, monkeypatch):
    now = NOW
    pin_clock(monkeypatch, now)  # the rows and the round are made by one clock, whatever UTC says when this runs
    last_week = (owner_today() - timedelta(days=8)).isoformat()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=SCORING)
        for title, seen in (("Recent", now - timedelta(hours=5)), ("Days", now - timedelta(hours=30)),
                            ("Dated", now - timedelta(hours=1))):
            session.add(_posting("Fabrikam", seen, title=title))
        started = await run.start_round(session, OWNER, started_by="manual")
        await session.commit()
        run_id = started.id
    said = {"a": "met", "b": "met", "c": "not"}
    async with db.session_factory()() as session:
        await run.stage_report(session, await session.get(SearchRun, run_id), [
            _answers("Recent", **said), _answers("Days", **said), {**_answers("Dated", **said), "posted": last_week},
            _answers("Unseen", **said)], done=True)
        await session.commit()
        stored = {o.title: o for o in (await session.scalars(select(Opportunity))).all()}
    assert [o.fit for o in stored.values()] == [None] * 4, "a find has its answers and no score before its research"
    stored = await score_like_research()  # the score is worked out last, from the posting each role was made from
    rows = {title: _within_row(role) for title, role in stored.items()}
    assert {t: (r["answer"], r["evidence"]) for t, r in rows.items() if t != "Unseen"} == {
        "Recent": ("met", "first seen 5 hours ago"), "Days": ("not", "first seen 30 hours ago"),
        "Dated": ("not", "posted 8 days ago")}, "the employer's week-old date beats an hour on the app's board"
    unseen = rows["Unseen"]
    assert (unseen["answer"], unseen["by"], unseen["evidence"]) == ("met", "app", "first seen under an hour ago"), (
        "no date from the employer, but every role has the posting the app made it from, first seen just now")
    assert stored["Recent"].fit == 7 and stored["Days"].fit == 4, "4 from the reader's answers, and 3 for being fresh"


@pytest.mark.parametrize(("zone", "now", "evidence"), [
    ("UTC", datetime(2026, 11, 4, 12, 0, tzinfo=UTC), "first seen 30 hours ago"),
    ("UTC", datetime(2026, 11, 4, 3, 0, tzinfo=UTC), "first seen 2 days ago"),  # 30 hours back is two days back
    ("America/New_York", datetime(2026, 11, 5, 0, 30, tzinfo=UTC), "first seen 30 hours ago"),  # 7:30 pm there
], ids=["noon", "small-hours", "evening-in-new-york"])
async def test_the_analyst_scores_a_role_again_from_the_hour_its_posting_was_first_seen(
        js, monkeypatch, zone, now, evidence):
    """The days are counted from the owner's local day the app first saw the posting (`freshness.posted_basis`), and a
    date that says no is the evidence over the hours: in the small hours, when 30 hours ago was two days back, it says
    so. At 7:30 pm in New York, already the next day in UTC, 30 hours ago is one day back there and two in UTC: the
    evidence is the owner's. The clock is pinned: the evening in New York is already the next day in UTC, and this test,
    which took the time for its rows and the day for its answer at two real moments, saw UTC midnight pass between
    them."""
    pin_clock(monkeypatch, now, zone)
    today = owner_today()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=SCORING)
        await save_documents(session)
        await roster.ensure_roster(session, OWNER)
        made = {}
        for company, seen in (("Fabrikam", now - timedelta(hours=3)), ("Initech", now - timedelta(hours=30))):
            role = Opportunity(
                description=POSTING_WORDS,
                owner=OWNER, slug=company.lower(), company=company, title="Programs Lead", fit=2,
                               origin="scout", match_key=match_key(company, "Programs Lead"),
                                   status=stages.SECOND_IMPRESSION,
                               found=today, url=f"https://jobs.example.test/{company.lower()}")
            session.add(role)
            await session.flush()
            session.add(_posting(company, seen, role=role.id))
            await analysis.start(session, OWNER, role)
            made[company] = role.id
        await session.commit()
    for role_id in made.values():
        assert (await submit_findings(findings(role_id, fit_breakdown=answered("met", "met", "not"))))["taken"] is True
    async with db.session_factory()() as session:
        rows = {r.company: _within_row(r) for r in (await session.scalars(select(Opportunity))).all()}
    assert (rows["Fabrikam"]["answer"], rows["Fabrikam"]["evidence"], rows["Fabrikam"]["by"]) == (
        "met", "first seen 3 hours ago", "app")
    assert rows["Initech"]["answer"] == "not", "found today, but the app has had the posting for more than a day"
    assert rows["Initech"]["evidence"] == evidence


async def test_a_role_with_no_posting_in_the_index_is_told_by_its_date_alone(js, monkeypatch):
    pin_clock(monkeypatch, NOW)  # the rows and the answer are of one day, whatever UTC says when this runs
    today = owner_today()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=SCORING)
        await save_documents(session)
        await roster.ensure_roster(session, OWNER)
        made = {}
        for company, found, posted in (("Fabrikam", today, None), ("Initech", today - timedelta(days=4), None),
                                       ("Hooli", today, today - timedelta(days=6))):
            role = Opportunity(
                description=POSTING_WORDS,
                owner=OWNER, slug=company.lower(), company=company, title="Programs Lead", fit=2,
                               origin="scout", match_key=match_key(company, "Programs Lead"),
                                   status=stages.SECOND_IMPRESSION,
                               found=found, posted=posted, url=f"https://jobs.example.test/{company.lower()}")
            session.add(role)
            await session.flush()
            await analysis.start(session, OWNER, role)
            made[company] = role.id
        await session.commit()
    for role_id in made.values():
        await submit_findings(findings(role_id, fit_breakdown=answered("met", "met", "not")))
    async with db.session_factory()() as session:
        rows = {r.company: _within_row(r) for r in (await session.scalars(select(Opportunity))).all()}
    assert {c: (r["answer"], r["evidence"]) for c, r in rows.items()} == {
        "Fabrikam": ("met", "found today"), "Initech": ("not", "found 4 days ago"),
        "Hooli": ("not", "posted 6 days ago")}
