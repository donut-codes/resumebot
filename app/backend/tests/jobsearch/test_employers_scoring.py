"""Employers as records (path 21) and fit part by part (path 20.3): matching names, linking roles, the watchlist folded
in, local employers, facts that decide modifiers, the owner's edits, and a round that stores the breakdown, the posting
text and the employer. Everything is made up."""

import asyncio
import copy
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import event, select, update

from app import db
from app.jobsearch import stages
from app.jobsearch.employers import core as employers
from app.jobsearch.fit import scoring
from app.jobsearch.missions import search_run as run
from app.jobsearch.models import Employer, Opportunity, SearchRun
from app.jobsearch.profile import schema as p
from tests.builders import score_like_research
from tests.support import OWNER, PROFILE, load_profile, reported

SCORING = {
    "asks": [
        {"id": "promotion", "section": "posting", "says": "A promotion", "points": 2},
        {"id": "lateral", "section": "posting", "says": "Lateral", "points": 0},
        {"id": "established", "section": "company", "says": "Established 20+ years", "points": 1,
         "fact": "years_since_founded", "op": ">=", "value": 20},
        {"id": "small", "section": "company", "says": "Under 1,000 employees", "points": 1,
         "fact": "employees", "op": "<", "value": 1000},
        {"id": "gaps", "section": "qualifications", "says": "Each unproven required item", "points": -1,
         "per_item": True, "limit": -3},
        {"id": "aging", "section": "posting", "says": "Posted over 30 days ago", "points": -1, "days": 30,
         "checked_by": "app"},
    ],
}


def said(**answers) -> dict:
    """A report's answers by ask id: `said(promotion="met", gaps="not")`."""
    return {"asks": [{"id": k, "answer": v, "evidence": f"evidence for {k}"} for k, v in answers.items()]}


def profile() -> p.Profile:
    return p.Profile.model_validate({**copy.deepcopy(PROFILE), "scoring": SCORING})


# --- names ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("a", "b"), [
    ("Globex Inc.", "globex"), ("The Globex Corporation", "GLOBEX"), ("Globex, LLC", "Globex Holdings"),
    ("Northwind Labs (fictional)", "northwind labs"),
    # the tail of a legal name leaves nothing behind
    ("Globex and Company", "Globex"), ("Globex & Co.", "Globex"), ("Globex and Co", "Globex & Company"),
    # who a posting is posted by, or which part of the employer it is, is not part of the name
    ("Initech (via Fabrikam)", "Initech and Company"), ("Initech and Company (via Fabrikam)", "INITECH, INC."),
    ("Fabrikam, Inc.", "Fabrikam"), ("Contoso Solutions LLC", "Contoso Solutions"), ("Contoso (Retail)", "Contoso"),
    # "&" and "and" are one word
    ("Northwind & Fabrikam", "Northwind and Fabrikam"), ("Northwind and Fabrikam Co.", "Northwind & Fabrikam"),
    # the plural of "Company" is one more ending: a company and "the Companies" of its group are one employer
    ("Northwind Insurance Group, Inc.", "Northwind Insurance Companies"), ("Globex Companies", "Globex Co."),
    ("The Globex Companies, Inc.", "Globex"), ("Globex and Companies", "GLOBEX"),
])
def test_names_that_mean_the_same_employer_share_a_key(a, b):
    assert employers.key_of(a) == employers.key_of(b) != ""


@pytest.mark.parametrize(("a", "b"), [
    ("Globex and Sons", "Globex"), ("Globex Health", "Globex"), ("Northwind and Fabrikam", "Northwind"),
    ("Initech (via Fabrikam)", "Fabrikam"), ("Globex Companies Health", "Globex"),
])
def test_names_that_are_not_the_same_employer_keep_apart(a, b):
    assert employers.key_of(a) != employers.key_of(b)


def test_and_is_left_out_only_as_a_word():
    assert employers.key_of("Anderson Brand and Sandstone") == "anderson brand sandstone"


def test_a_key_is_the_same_when_it_is_made_again():
    for name in ("Globex and Company (via Fabrikam)", "Northwind & Fabrikam Co.", "Globex USA"):
        assert employers.key_of(employers.key_of(name)) == employers.key_of(name)


# --- scoring --------------------------------------------------------------------------------------------------------


def test_the_app_adds_the_points_of_each_ask_on_its_own_and_counts_per_item_to_the_limit():
    raw = {"asks": [
        {"id": "promotion", "answer": "met", "evidence": "a Director title"},
        {"id": "lateral", "answer": "met", "evidence": "also lateral?"},
        {"id": "gaps", "answer": "met", "evidence": "five gaps", "count": 5}]}
    b = scoring.compute(raw, profile())
    rows = {a["id"]: a for a in b["asks"]}
    assert [(a, rows[a]["earned"], rows[a]["by"]) for a in ("promotion", "gaps")] == [
        ("promotion", 2, "reader"), ("gaps", 0, "reader")]  # edited 2026-10-27 (PRD 30.10): gaps took 3 off
    assert rows["lateral"]["earned"] == 0 and "skipped" not in rows["lateral"], "no ask is set aside for another"
    assert b["total"] == 2 and b["possible"] == 4


def test_an_employer_fact_decides_its_ask_over_the_readers_word():
    raw = said(established="met", small="not")
    known = {"years_since_founded": 5, "employees": 400}
    b = scoring.compute(raw, profile(), known)
    rows = {a["id"]: a for a in b["asks"]}
    assert rows["established"]["answer"] == "not", "the record says five years, so it isn't so"
    assert rows["small"]["answer"] == "met" and rows["small"]["by"] == "app"
    assert "employees 400" in rows["small"]["evidence"]
    assert b["total"] == 1


def test_an_ask_the_reader_cant_tell_or_only_partly_meets_earns_nothing_and_a_penalty_applies_only_when_shown():
    raw = said(promotion="unknown", established="partly", gaps="unknown")
    # promotion (2 points) and small (1, not answered) are unknown, established is partly met: no partial credit.
    b = scoring.compute(raw, profile())
    rows = {a["id"]: a for a in b["asks"]}
    assert rows["promotion"]["earned"] == 0, "can't tell: nothing"
    assert rows["established"]["earned"] == 0, "partly met: nothing, not half the points"
    assert rows["small"]["earned"] == 0, "not answered: nothing"
    assert rows["gaps"]["earned"] == 0, "a penalty applies only when shown"
    assert (b["earned"], b["total"]) == (0, 0)


def test_a_breakdown_naming_an_ask_the_owner_lacks_is_a_problem():
    assert scoring.problems(said(invented="met"), profile()) == [
        "fit_breakdown names an ask 'invented' the owner doesn't have"]
    assert "one of met, not, unknown" in scoring.problems(said(promotion="yes"), profile())[0]
    assert scoring.problems(said(promotion="partly"), profile()) == [], "a stored partly is still accepted"
    twice = {"asks": [{"id": "promotion", "answer": "met"}, {"id": "promotion", "answer": "not"}]}
    assert "twice" in scoring.problems(twice, profile())[0]
    assert "answer each of the owner's asks" in scoring.problems({"base": 2, "applied": []}, profile())[0]
    assert scoring.problems(None, profile()) == []


def test_the_postings_age_decides_an_age_ask_and_an_unknown_age_takes_nothing_off():
    prof = profile()
    old = scoring.compute({"asks": []}, prof, facts={"age_days": 45})
    row = next(a for a in old["asks"] if a["id"] == "aging")
    assert (row["answer"], row["earned"], row["by"], row["evidence"]) == ("met", 0, "app", "posted 45 days ago")
    assert old["total"] == 0  # edited 2026-10-27 (PRD 30.10): the old-posting ask took 1 off
    fresh = next(a for a in scoring.compute({"asks": []}, prof, facts={"age_days": 5})["asks"] if a["id"] == "aging")
    assert (fresh["answer"], fresh["earned"]) == ("not", 0)
    unknown = next(a for a in scoring.compute({"asks": []}, prof)["asks"] if a["id"] == "aging")
    assert (unknown["answer"], unknown["earned"], unknown["by"]) == ("unknown", 0, "app")


def test_a_pay_tier_is_decided_by_the_app_on_the_likely_ask():
    tiers = {"asks": [
        {"id": "pay-low", "section": "compensation", "says": "Ask over 100,000", "points": 1,
         "amount": 100000, "checked_by": "app"},
        {"id": "pay-high", "section": "compensation", "says": "Ask over 150,000", "points": 2,
         "amount": 150000, "checked_by": "app"},
        {"id": "pay-under", "section": "compensation", "says": "Ask under 90,000", "points": -2, "above": False,
         "amount": 90000, "checked_by": "app"}]}
    prof = p.Profile.model_validate({"scoring": tiers})
    best = scoring.compute({"asks": []}, prof, facts={"ask": 160000})
    assert {a["id"]: (a["answer"], a["earned"]) for a in best["asks"]} == {
        "pay-low": ("met", 1), "pay-high": ("met", 2), "pay-under": ("not", 0)}, "each tier counts on its own"
    low = scoring.compute({"asks": []}, prof, facts={"ask": 80000})
    # Edited 2026-10-27 (PRD 30.10): was -2.
    assert low["total"] == 0 and low["asks"][2]["evidence"] == "likely ask $80,000"
    assert scoring.compute({"asks": []}, prof)["total"] == 0, "no pay posted: nothing to judge on"


def test_every_ask_is_listed_for_the_panel_with_what_it_earned_and_why():
    b = scoring.compute(said(promotion="met", gaps="not"), profile())
    rows = {r["id"]: r for r in scoring.view(b, profile())}
    assert rows["promotion"]["state"] == "checked" and "group" not in rows["promotion"]
    assert rows["promotion"]["earned"] == 2 and rows["promotion"]["evidence"] == "evidence for promotion"
    assert rows["gaps"]["answer"] == "not" and rows["gaps"]["earned"] == 0
    assert rows["established"]["state"] == "unknown", "what nobody has found yet is simply unknown"
    assert "decided" not in rows["established"] and rows["aging"]["by"] == "app"
    added = p.Profile.model_validate({"scoring": {"asks": [*SCORING["asks"], {"id": "new", "says": "Added later"}]}})
    assert {r["id"]: r["state"] for r in scoring.view(b, added)}["new"] == "unscored"
    assert [r["section"] for r in scoring.view(b, profile())] == [
        "company", "company", "posting", "posting", "posting", "qualifications"], "in the order a posting reads"


def test_employer_facts_are_only_what_the_record_holds():
    e = Employer(owner=OWNER, key="x", name="X", founded=2000, employees=None, ownership="unknown", ai_native=False,
                 events=[{"kind": "layoff", "date": "2025-06"}], sources={}, sectors=[], offices=[], aliases=[])
    f = employers.facts(e, date(2026, 6, 1))
    assert f["years_since_founded"] == 26 and f["ai_native"] is False and f["years_since_layoff"] == 1.0
    assert "employees" not in f and "ownership" not in f


# --- records --------------------------------------------------------------------------------------------------------


async def _seed():
    """Three roles, each written in a transaction of its own so each has its own `created_at`: "Globex Inc." comes
    first and names the employer it shares with "GLOBEX" (`employers.link_roles` takes the oldest first)."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=SCORING)
        today = date.today()
        for company, location in [("Globex Inc.", "Oakfield, ST"), ("GLOBEX", "Remote"), ("Initech", "Denver, CO")]:
            session.add(Opportunity(owner=OWNER, slug=company.lower(), company=company, title=f"Lead at {company}",
                                    fit=3, origin="tracker", match_key=f"{company.lower()}|lead",
                                    status="second_impression", found=today, location=location))
            await session.commit()


async def test_settling_links_every_role_and_marks_local_employers(js):
    await _seed()
    async with db.session_factory()() as session:
        result = await employers.settle(session, OWNER)
        await session.commit()
        rows = {e.name: e for e in (await session.scalars(select(Employer))).all()}
        roles = (await session.scalars(select(Opportunity))).all()
    assert result["linked"] == 3 and all(r.employer_id for r in roles)
    assert {r.employer_id for r in roles if "lobex" in r.company.lower() or "GLOBEX" in r.company}.__len__() == 1
    assert rows["Globex Inc."].local is True and rows["Initech"].local is False
    async with db.session_factory()() as session:
        assert (await employers.settle(session, OWNER))["linked"] == 0, "safe to run again"


async def test_a_settle_with_nothing_to_change_takes_no_turn_and_a_mark_of_local_employers_is_judged_before_its_write(
        js, monkeypatch):
    """On a large database the start's step "employer records" held the writer's turn for seconds, judging
    every employer and every role's place in the writer's session. Both are read in a reader's session: a second writer
    gets through while the roles and employers are read, a settle that changes nothing writes nothing, and the locals
    written are those of the run inside a savepoint (where the read is made in the transaction itself)."""
    await _seed()
    async with db.session_factory()() as session:
        await employers.link_roles(session, OWNER)
        await session.commit()
    free: list[bool] = []
    real = employers.profile_store.current

    async def probing(session, owner):
        async with db.session_factory()() as other:  # a second writer, asking while the roles are read
            try:
                await asyncio.wait_for(other.execute(update(Opportunity).where(Opportunity.slug == "none")
                                                     .values(slug="none")), 1)
                free.append(True)
            except TimeoutError:
                free.append(False)
            await other.rollback()
        return await real(session, owner)

    async def locals_() -> dict:
        async with db.session_factory()() as session:
            return {e.name: e.local for e in (await session.scalars(select(Employer))).all()}

    monkeypatch.setattr(employers.profile_store, "current", probing)
    async with db.session_factory()() as session:
        await employers.mark_local(session, OWNER)
        await session.commit()
    marked = await locals_()
    assert free == [True] and marked == {"Globex Inc.": True, "Initech": False}

    written: list = []
    async with db.session_factory()() as session:
        event.listen(session.sync_session, "before_flush",
                     lambda s, c, i: written.extend([*s.new, *s.dirty, *s.deleted]))
        assert await employers.link_roles(session, OWNER) == 0
        await employers.mark_local(session, OWNER)
        await session.commit()
    assert free == [True, True] and written == [], "a settle with nothing to change writes nothing"

    async with db.session_factory()() as session:
        await session.execute(update(Employer).values(local=None))
        await session.commit()
    async with db.session_factory()() as session:
        async with session.begin_nested():
            await employers.mark_local(session, OWNER)
        await session.commit()
    assert await locals_() == marked


async def test_the_oldest_role_names_the_employer_whatever_order_the_table_holds_them_in(js):
    """Found as a test that failed only in the full run: unordered, Postgres hands rows back in the order the table's
    pages hold them, and a worker's tables, emptied by DELETE and reused, put new rows in freed space. A row written
    again (a new version of it) moves behind the others the same way, every time."""
    await _seed()
    async with db.session_factory()() as session:
        await session.execute(update(Opportunity).where(Opportunity.company == "Globex Inc.").values(notes="moved"))
        await session.commit()
        await employers.settle(session, OWNER)
        await session.commit()
        assert {e.name for e in (await session.scalars(select(Employer))).all()} == {"Globex Inc.", "Initech"}


async def test_an_employer_is_local_by_the_gates_own_rule_a_town_with_its_state_or_a_town_alone(js):
    """A town the owner wrote with its state is that town in that state only; one written alone is named in any state;
    an office that names the town and no state is not known to be local."""
    where = {"remote": {"on": True}, "hybrid": {"on": True, "locations": ["Oakfield, OH", "Riverton"]},
             "in_person": {"on": True, "locations": ["Oakfield, OH", "Riverton"]}}
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates={**PROFILE["gates"], "location": where})
        for company, place in [("Ohio Co", "Oakfield, OH"), ("Jersey Co", "Oakfield, NJ"), ("Nowhere Co", "Oakfield"),
                               ("Alone Co", "Riverton, NJ"), ("Written Co", "Oakfield, Ohio"),
                               ("Both Co", "Oakfield, NJ / US-OH-Oakfield"), ("Far Co", "Denver, CO")]:
            session.add(Opportunity(owner=OWNER, slug=company.lower(), company=company, title=f"Lead at {company}",
                                    fit=3, origin="tracker", match_key=f"{company.lower()}|lead",
                                    status="second_impression", found=date.today(), location=place))
        await session.flush()
        await employers.settle(session, OWNER)
        await session.commit()
        local = {e.name: e.local for e in (await session.scalars(select(Employer))).all()}
    assert local == {"Ohio Co": True, "Jersey Co": False, "Nowhere Co": False, "Alone Co": True, "Written Co": True,
                     "Both Co": True, "Far Co": False}


@pytest.mark.parametrize(("a", "b"), [("US Bank", "Bank"), ("US Steel", "Steel")])
def test_a_name_that_starts_with_us_is_not_the_name_without_it(a, b):
    """"US Bank" and "Bank" shared the key "bank", and fold_gate merged them."""
    assert employers.key_of(a) != employers.key_of(b)


@pytest.mark.parametrize(("a", "b"), [
    ("Globex USA", "Globex"), ("Globex US Inc.", "globex"), ("Globex, Inc. USA", "Globex")])
def test_us_at_the_end_of_a_name_is_still_noise(a, b):
    assert employers.key_of(a) == employers.key_of(b)


async def test_renaming_an_employer_to_one_that_exists_is_refused_and_changes_nothing(client):
    """The second employer with the same key broke uq_js_employers_owner_key and the page got a 500."""
    await _seed()
    async with db.session_factory()() as session:
        await employers.settle(session, OWNER)
        await session.commit()
    listed = {e["name"]: e for e in (await client.get("/api/v1/employers")).json()["employers"]}
    initech = listed["Initech"]
    clash = await client.patch(f"/api/v1/employers/{initech['id']}", json={"name": "The Globex Corporation"})
    assert clash.status_code == 422 and "Globex Inc." in clash.json()["detail"]
    assert (await client.get(f"/api/v1/employers/{initech['id']}")).json()["name"] == "Initech"
    fine = await client.patch(f"/api/v1/employers/{initech['id']}", json={"name": "Initech Labs"})
    assert fine.status_code == 200 and fine.json()["name"] == "Initech Labs"
    same = await client.patch(f"/api/v1/employers/{initech['id']}", json={"name": "INITECH LABS"})
    assert same.status_code == 200, "a change to its own name is not a clash"


async def test_the_owner_edits_a_fact_and_it_becomes_theirs(client):
    await _seed()
    async with db.session_factory()() as session:
        await employers.settle(session, OWNER)
        await session.commit()
    listed = (await client.get("/api/v1/employers")).json()["employers"]
    globex = next(e for e in listed if e["name"] == "Globex Inc.")
    assert globex["roles_total"] == 2 and globex["roles_open"] == 2
    edited = await client.patch(f"/api/v1/employers/{globex['id']}", json={"founded": 1980, "ownership": "pe"})
    assert edited.status_code == 200 and edited.json()["owner_fields"] == ["founded", "ownership"]
    assert edited.json()["sources"]["founded"]["source"] == "you"
    bad = await client.patch(f"/api/v1/employers/{globex['id']}", json={"ownership": "a cult"})
    assert bad.status_code == 422
    detail = (await client.get(f"/api/v1/employers/{globex['id']}")).json()
    assert detail["founded"] == 1980 and len(detail["roles"]) == 2
    assert (await client.get(f"/api/v1/employers/{uuid.uuid4()}")).status_code == 404


# --- a round with breakdowns ----------------------------------------------------------------------------------------


async def test_a_round_stores_the_breakdown_and_the_employer_and_no_reader_summary_as_text(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=SCORING)
        started = await run.start_round(session, OWNER, started_by="manual")
        await session.commit()
        run_id = started.id
    role = reported("Northwind", "Director of Programs")  # Globex is an excluded employer here
    role.pop("fit"), role.pop("fit_evidence")  # the reader sends its answers, no total and no line
    role["fit_breakdown"] = said(promotion="met")
    role.update(description="Lead programs. Requires five years.", comp="$150,000 - $170,000", comp_min=150000,
                comp_max=170000, location="Remote", work_mode="remote")
    role["posted"] = (date.today() - timedelta(days=40)).isoformat()
    bad = copy.deepcopy(role)
    bad["fit_breakdown"] = said(invented="met")
    async with db.session_factory()() as session:
        with pytest.raises(run.RunError, match="the owner doesn't have"):
            await run.stage_report(session, await session.get(SearchRun, run_id), [bad], done=False)
    async with db.session_factory()() as session:
        await run.stage_report(session, await session.get(SearchRun, run_id), [role], done=True)
        await session.commit()
        stored = (await session.scalars(select(Opportunity))).one()
        employer = await session.get(Employer, stored.employer_id)
    assert employer.name == "Northwind"
    assert stored.description is None and stored.description_at is None, "a reader's reading is a summary: not kept"
    assert stored.status == stages.LANDING_ZONE
    # The reader gave answers, not a score: the role holds the answers (under the ask's words) and no score yet.
    assert stored.fit is None and set(stored.fit_breakdown) == {"answers"}
    assert [(a["id"], a["answer"], a["says"]) for a in stored.fit_breakdown["answers"]] == [
        ("promotion", "met", "A promotion")]
    assert stored.fit_evidence == "1 met", "the reader sent no line: the app wrote one from the answers"
    # The score is worked out after research, from the answers kept and what the app decides itself.
    stored = (await score_like_research())["Director of Programs"]
    b = stored.fit_breakdown
    rows = {a["id"]: a for a in b["asks"]}
    assert (rows["promotion"]["answer"], rows["promotion"]["earned"]) == ("met", 2)
    assert (rows["aging"]["answer"], rows["aging"]["earned"], rows["aging"]["by"]) == ("met", 0, "app"), \
        "the posting is 40 days old: met, and earns nothing (edited 2026-10-27, PRD 30.10; it took 1 off)"
    # established and small are unknown (no record yet): they earn nothing, and stay in what can be earned.
    # Edited 2026-10-27 (PRD 30.10): the total and earned points were 1.
    assert b["total"] == stored.fit == 2 and b["earned"] == 2.0 and b["possible"] == 4


async def test_the_role_panel_lists_every_criterion_and_each_gate_with_its_fact(client):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=SCORING)
        breakdown = scoring.compute(said(promotion="met"), profile())
        role = Opportunity(owner=OWNER, slug="n", company="Northwind", title="Lead", fit=breakdown["total"],
                           origin="scout", status="second_impression", match_key="northwind|lead", found=date.today(),
                           location="Gotham, NY", move_type="onsite", comp="$80,000 - $90,000",
                           description="The posting.", fit_breakdown=breakdown)
        session.add(role)
        await session.commit()
        role_id = role.id
    body = (await client.get(f"/api/v1/roles/{role_id}")).json()
    assert body["role"]["description"] == "The posting."
    panel = body["scoring"]
    assert panel["kind"] == "asks" and panel["total"] == 2 and panel["possible"] == 4 and panel["percent"] == 50
    assert [a["id"] for a in panel["asks"] if a["earned"]] == ["promotion"]
    assert [s["id"] for s in panel["sections"]] == [
        "company", "posting", "description", "responsibilities", "qualifications", "preferred", "compensation"]
    gates = {g["id"]: g for g in body["gates"]}
    assert gates["location"]["result"] == "fail" and "Gotham" in gates["location"]["fact"]
    assert gates["compensation"]["result"] == "fail" and gates["compensation"]["fact"] == (
        "the posted $80,000-$90,000 has its midpoint at $85,000, at least $1,000 under the $95,000 pay floor, "
        "so the estimated ask is under it")
    assert "employer" not in gates, "an employer you haven't gated has no line"
    assert gates["travel"]["result"] == "unknown" and gates["travel"]["checked_by"] == "reader"


async def test_the_role_api_shows_the_base_reason_and_the_note_of_an_old_score_apart(client):
    glued = {"base": 4, "base_why": "It builds.", "noted": "biggest gap: a credential that is not held.",
             "applied": [], "not_applied": [], "total": 4}
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=SCORING)
        role = Opportunity(owner=OWNER, slug="g", company="Northwind", title="Lead", fit=4, origin="scout",
                           status="second_impression", match_key="northwind|lead", found=date.today(),
                           fit_breakdown=glued)
        session.add(role)
        await session.commit()
        role_id = role.id
    old = (await client.get(f"/api/v1/roles/{role_id}")).json()["scoring"]["old"]
    assert old["base_why"] == "It builds."
    assert old["noted"] == "biggest gap: a credential that is not held."

def _layoffs(events, researched=True):
    return Employer(owner=OWNER, key="x", name="X", events=events,
                    sources={"events": {"source": "news"}} if researched else {},
                    sectors=[], offices=[], aliases=[])


@pytest.mark.parametrize(("dates", "count"), [
    ([], 0),
    (["2026-03"], 1),
    (["2026-03", "2025-09", "2024-12", "2023-01"], 3),
    (["2026-05", "2026-01", "2025-08", "2025-02", "2020-01"], 4),
])
def test_layoffs_in_2_years_counts_dated_layoffs_within_730_days(dates, count):
    events = [{"kind": "layoff", "date": d} for d in dates] + [{"kind": "funding", "date": "2026-04"}]
    assert employers.facts(_layoffs(events), date(2026, 6, 1))["layoffs_in_2_years"] == count


def test_layoffs_in_2_years_is_unknown_until_researched():
    assert "layoffs_in_2_years" not in employers.facts(_layoffs([], researched=False), date(2026, 6, 1))


def test_a_first_match_on_layoff_count_decides_top_down():
    row = {"id": "layoffs", "kind": "best", "section": "company", "members": [
        {"id": "l4", "says": "4 or more layoffs", "points": -3, "fact": "layoffs_in_2_years", "op": ">=", "value": 4,
         "checked_by": "app"},
        {"id": "l2", "says": "2 or 3 layoffs", "points": -2, "fact": "layoffs_in_2_years", "op": ">=", "value": 2,
         "checked_by": "app"},
        {"id": "l1", "says": "A layoff", "points": -1, "fact": "layoffs_in_2_years", "op": ">=", "value": 1,
         "checked_by": "app"}]}
    prof = p.Profile.model_validate({**copy.deepcopy(PROFILE), "scoring": {"asks": [], "groups": [row]}})
    # Edited 2026-10-27 (PRD 30.10): the levels (-3, -2, -1) still hold top down but earn nothing now.
    for n, level, earned in [(5, "l4", 0), (4, "l4", 0), (3, "l2", 0), (2, "l2", 0), (1, "l1", 0), (0, "none", 0)]:
        b = scoring.compute({"asks": []}, prof, {"layoffs_in_2_years": n})
        got = next(r for r in b["asks"] if r["id"] == "layoffs")
        assert (got["level"], got["earned"]) == (level, earned), n
