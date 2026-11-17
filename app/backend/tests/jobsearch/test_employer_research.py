"""The employer researcher (docs/PRD.md path 36 and 39.17 to 39.19): a nightly mission puts a batch of employers in
front of a small session in the order of 36.1; code checks what it reports, fact by fact (36.3); the job board is
looked for by the app's own detection and from what the researcher read; duplicates and splits go through the app's
own merge and split and nothing else (36.5, 36.6); and the gates and scores of the employer's roles are worked out
again on the new facts (39.18). Every name, address and number here is made up."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from process_engine.model import AGENT
from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch import stages
from app.jobsearch.employers import pairs as employer_pairs
from app.jobsearch.employers import records as employer_records
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.fit import scoring
from app.jobsearch.missions import core as missions
from app.jobsearch.models import Employer, MissionRun, Opportunity
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis, prioritize
from app.jobsearch.research import employer as employer_research
from app.jobsearch.work import hand_outs, session_kinds
from app.models.work import TASK_DONE, Task, TrailEvent
from app.trail import service as trail
from tests.employer_builders import BY_YOU, make_employer, make_role
from tests.jobsearch.example_world import example_world  # noqa: F401  (the fixture)
from tests.support import FIVE, OWNER, POSTING_WORDS, PROFILE, answered, load_profile

pytestmark = pytest.mark.usefixtures("example_world")

R = employer_research
#: Never PE-owned: a gate on a fact only research brings (docs/PRD.md 25.4). Nothing else is gated, so a role's place
#: and pay never stop it in these tests.
NO_PE = {"id": "no-pe", "kind": "employer_fact", "fact": "ownership", "op": "!=", "value": "pe", "section": "company"}
GATES = {"employer_names": [], "other": [NO_PE]}
NO_GATES = {"employer_names": [], "other": []}
#: What a researcher finds for an employer, and where.
FOUND = {"website": "https://www.acme-corp.test", "industry": "Software", "employees": 300, "ownership": "private",
         "headquarters": "Oakfield, OH", "founded": 1990}
SOURCES = {name: f"https://about.example.test/{name}" for name in FOUND}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Every page a step reads is a made-up one: nothing answers (404) unless a test says so."""

    def make(handler=None):
        return lambda: Fetcher(httpx.AsyncClient(
            transport=httpx.MockTransport(handler or (lambda request: httpx.Response(404))), follow_redirects=True),
            per_host_seconds=0)

    monkeypatch.setattr(polite, "make", make())
    return lambda handler: monkeypatch.setattr(polite, "make", make(handler))


async def _world(**parts) -> None:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, **{"gates": NO_GATES, **parts})
        await roster.ensure_roster(session, OWNER)
        await missions.ensure(session, OWNER)
        await session.commit()


async def _employer(name: str, roles: tuple | None = None, **fields) -> Employer:
    """An employer with roles of these titles (or (title, status) pairs), by default one role on deck (only an employer
    with a role on deck or later is researched, `R.WORTH_RESEARCH`); its id is what a test keeps."""
    async with db.session_factory()() as session:
        employer = await make_employer(session, name, **fields)
        for item in ("Lead " + name,) if roles is None else roles:
            title, status = (item, stages.SECOND_IMPRESSION) if isinstance(item, str) else item
            session.add(make_role(employer, title, status))
        await session.commit()
        return employer


async def _get(model, ident):
    async with db.session_factory()() as session:
        return await session.get(model, ident)


async def _send(*employer_ids) -> None:
    """Put these employers in front of the researcher, as the mission does."""
    async with db.session_factory()() as session:
        for ident in employer_ids:
            await R.start(session, OWNER, await session.get(Employer, ident))
        await session.commit()


async def _report(employer_id, facts=None, sources=None, **more) -> dict:
    """The researcher's report, handed in as its tool does, and the app's reply."""
    body = {"employer": str(employer_id), "facts": FOUND if facts is None else facts,
            "sources": SOURCES if sources is None else sources, **more}
    async with db.session_factory()() as session:
        reply = await R.submit(session, OWNER, body)
        await session.commit()
        return reply


async def _roles(employer_id=None) -> dict[str, Opportunity]:
    async with db.session_factory()() as session:
        query = select(Opportunity)
        if employer_id is not None:
            query = query.where(Opportunity.employer_id == employer_id)
        return {r.title: r for r in (await session.scalars(query)).all()}


async def _names() -> list[str]:
    async with db.session_factory()() as session:
        return sorted(e.name for e in (await session.scalars(select(Employer))).all())


# --- which employers, and in what order (36.1) ----------------------------------------------------------------------


async def test_the_night_takes_only_employers_with_a_role_on_deck_or_later_the_most_open_roles_first(js):
    await _world()
    long_ago = datetime.now(UTC) - timedelta(days=100)
    await _employer("Acme", ["Lead A"], open_roles=3)
    await _employer("Beta", [("Lead B", stages.APPLIED)], open_roles=10)
    await _employer("Gone", [("Lead G", stages.GATED_OUT)], open_roles=99)
    await _employer("Cobalt", (), open_roles=50)
    await _employer("Delta", ("Lead D",), open_roles=5)
    await _employer("Echo", ("Lead E",), open_roles=5, researched_at=long_ago)
    async with db.session_factory()() as session:
        picked = [e.name for e in await R.picks(session, OWNER, None, 90)]
    assert picked == ["Beta", "Delta", "Echo", "Acme"], (
        "only an employer with a role on deck or later (Applied counts; Gated out and no role do not: the owner, "
        "2026-10-21), the most open roles first, the never researched first")


async def test_an_employer_researched_lately_a_gated_one_and_one_with_the_researcher_are_left_out(js):
    await _world()
    now = datetime.now(UTC)
    await _employer("Fresh", researched_at=now - timedelta(days=10))
    await _employer("Stale", researched_at=now - timedelta(days=100))
    await _employer("Hard Pass", gated=True)
    busy = await _employer("Busy")
    await _employer("Never")
    await _send(busy.id)
    async with db.session_factory()() as session:
        assert [e.name for e in await R.picks(session, OWNER, None, 90)] == ["Never", "Stale"]
        assert [e.name for e in await R.picks(session, OWNER, None, 5)] == ["Never", "Fresh", "Stale"], (
            "the days are the owner's number")
        assert [e.name for e in await R.picks(session, OWNER, 1, 90)] == ["Never"], "a night's number, the best first"


async def test_an_employer_that_failed_last_time_goes_after_the_others_and_is_tried_again(js):
    await _world()
    await _employer("Aardvark", research_state=R.FAILED)
    await _employer("Zebra")
    async with db.session_factory()() as session:
        assert [e.name for e in await R.picks(session, OWNER, None, 90)] == ["Zebra", "Aardvark"]


# --- the mission ----------------------------------------------------------------------------------------------------


async def _mission(session, key=missions.RESEARCH) -> missions.Mission:
    return (await session.scalars(select(missions.Mission).where(missions.Mission.key == key))).one()


def test_the_numbers_of_the_mission_take_minus_one_for_no_limit_and_days_do_not():
    check = missions.check_settings
    assert check(missions.RESEARCH, {})["target"] == 25 and check(missions.RESEARCH, {})["again_after_days"] == 90
    assert check(missions.RESEARCH, {"target": -1})["target"] == -1, "no limit"
    assert check(missions.RESEARCH, {"target": "40", "again_after_days": "30", "finish_by": "05:30"}) == {
        "target": 40, "again_after_days": 30, "finish_by": "05:30"}
    for bad, words in (({"target": 0}, "employers to research: 1 to 1000, or -1 for no limit"),
                       ({"target": "lots"}, "employers to research: a whole number, or -1 for no limit"),
                       ({"again_after_days": 0}, "research again after: 1 to 3650 days"),
                       ({"again_after_days": -1}, "research again after: 1 to 3650 days"),
                       ({"again_after_days": "soon"}, "research again after: a whole number of days"),
                       ({"again_after_days": None}, "research again after: a whole number of days"),
                       ({"finish_by": "late"}, "finish by")):
        with pytest.raises(missions.MissionError, match=words):
            check(missions.RESEARCH, bad)


async def test_a_run_sends_the_batch_in_order_and_says_what_came_of_it(js):
    await _world()
    first = await _employer("Acme", ["Lead A"])
    second = await _employer("Beta", ["Lead B"], open_roles=4)
    third = await _employer("Cobalt")
    async with db.session_factory()() as session:
        mission = await _mission(session)
        await missions.change(session, mission, by="Tester", values={"target": 2})
        entry = await missions.start(session, OWNER, mission, by="you")
        await session.commit()
        assert entry.outcome == "running" and entry.started["target"] == 2
        assert entry.started["employers"] == [str(second.id), str(first.id)], "the one with more open roles first"
        assert entry.started["reason"] == "employers_asked" and entry.started["knock"] == [str(second.id)]
        assert entry.summary == "Sent 2 employers to the researcher: Beta, Acme."
        run_id = entry.id
    assert (await _get(Employer, third.id)).research_state is None, "past the night's number"
    await _report(second.id, {"ownership": "private"}, {"ownership": "its About page"})
    async with db.session_factory()() as session:
        run = await session.get(MissionRun, run_id)
        await missions.refresh(session, run)
        assert run.outcome == "running" and run.started["done"] == 1
        assert missions.run_view(run, missions.RESEARCH)["progress"] == {"done": 1, "target": 2}
    await _report(first.id)
    async with db.session_factory()() as session:
        run = await session.get(MissionRun, run_id)
        await missions.refresh(session, run)
        assert run.outcome == "done"
        assert run.summary == "Researched 2 of 2 employers: 7 facts set. Stopped: target met."
        view = await missions.view(session, await _mission(session))
    assert view["last_run"]["progress"] == {"done": 2, "target": 2} and view["numbers"] == []


async def test_the_mission_runs_nightly_after_the_boards_are_read_and_not_in_claudes_peak_hours(js):
    await _world()
    await _employer("Acme")
    now = datetime.now(UTC)
    async with db.session_factory()() as session:
        reading = await _mission(session, missions.READ)
        session.add(MissionRun(owner=OWNER, mission_id=reading.id, started_by="schedule",
                               started_at=now - timedelta(hours=1), ended_at=now - timedelta(minutes=30),
                               outcome="done", summary="Read.", started={}))
        mission = await _mission(session)
        mission.created_at, mission.state = now - timedelta(days=1), "on"
        assert await missions.due(session, mission), "the read finished after the mission was made"
        assert await missions._tick(session, OWNER, mission, True, now) == [], "Claude's peak hours hold it"
        knocks = await missions._tick(session, OWNER, mission, False, now)
        await session.commit()
        employer = (await session.scalars(select(Employer))).one()
        assert knocks == [(str(employer.id), "employers_asked")], "the runner is woken for it"
        assert not await missions.due(session, mission, now), "once for a night: its run is the last start"


async def test_a_stopped_night_sends_back_the_employers_no_session_began_and_they_are_picked_again(js):
    await _world()
    done = await _employer("Acme", ["Lead A"], open_roles=3)
    left = await _employer("Beta")
    async with db.session_factory()() as session:
        mission = await _mission(session)
        entry = await missions.start(session, OWNER, mission, by="you")
        await session.commit()
        run_id = entry.id
    await _report(done.id)
    async with db.session_factory()() as session:
        run = await session.get(MissionRun, run_id)
        finish = datetime.now(UTC) + timedelta(days=2)
        await missions.advance(session, OWNER, await _mission(session), run, False, finish)
        await session.commit()
        assert run.outcome == "done" and run.started["stopped"] == "finish"
        assert [e.name for e in await R.picks(session, OWNER, None, 90)] == ["Beta"], "the researched one is not again"
    back = await _get(Employer, left.id)
    assert back.research_state is None and back.research_instance_id is None, "back in line, not failed"


async def test_claudes_usage_limit_ends_the_run_and_sends_the_rest_back(js):
    await _world()
    await _employer("Acme")
    async with db.session_factory()() as session:
        mission = await _mission(session)
        entry = await missions.start(session, OWNER, mission, by="you")
        await session.commit()
        run_id = entry.id
        assert await missions.usage_limit_hit(session, OWNER, "You've hit your limit") == 1
        await session.commit()
        run = await session.get(MissionRun, run_id)
        assert run.outcome == "done" and run.started["stopped"] == "usage"
        assert (await session.scalars(select(Employer))).one().research_state is None


# --- the line a session takes from ----------------------------------------------------------------------------------


async def test_the_task_gives_the_employer_its_roles_the_pairs_code_found_and_what_the_owner_reads(js):
    await _world(gates={"employer_names": [], "other": [NO_PE]})
    acme = await _employer("Fabrikam", ["Lead A", "Lead B"], website="https://www.fabrikam-corp.test", industry="Paint",
                           owner_fields=["industry"], sources={"industry": {"source": "you", "by": "owner"}})
    other = await _employer("Fabrikam Dental", ["Lead C"])
    await _send(acme.id)
    async with db.session_factory()() as session:
        task = await R.task_reply(session, OWNER)
        await session.commit()
    assert task["ok"] and task["employer"]["id"] == str(acme.id) and task["employer"]["name"] == "Fabrikam"
    assert task["employer"]["facts"] == {"website": "https://www.fabrikam-corp.test", "industry": "Paint"}
    assert task["employer"]["owner_set"] == ["industry"], "the researcher is told what it cannot change"
    assert task["employer"]["job_board"] is None
    assert sorted(r["title"] for r in task["roles"]) == ["Lead A", "Lead B"] and task["more_roles"] == 0
    assert [(c["employer"], c["name"], c["roles"], c["code_merges_these_on_its_own"])
            for c in task["may_be_the_same_as"]] == [(str(other.id), "Fabrikam Dental", 1, False)]
    assert task["may_be_the_same_as"][0]["looks_the_same_because"], "and why they look the same"
    assert task["the_owner_reads"] == [{"fact": "ownership", "report": "ownership"}]
    assert "# Researching an employer" in task["instructions"] and "employer:" in task["response_template"]


async def test_the_task_says_which_pairs_code_merges_on_its_own_and_offers_no_agency_and_client(js):
    await _world()
    acme = await _employer("Acme Labs", ["Lead A"], website="https://www.acme-corp.test")
    await _employer("Acme", ["Lead B"], website="https://acme-corp.test")  # alike names on one website: code merges
    await _employer("Northwind", ["Lead C"], website="https://www.acme-corp.test")  # one website only: kept as two
    await _employer("Staffco", ["Lead D"], website="https://www.acme-corp.test")
    async with db.session_factory()() as session:  # a role of Acme Labs that came through Staffco keeps the tag
        session.add(make_role(await session.get(Employer, acme.id), "Lead E", company="Acme Labs (via Staffco)"))
        await session.commit()
    await _send(acme.id)
    async with db.session_factory()() as session:
        task = await R.task_reply(session, OWNER)
        await session.commit()
    assert {c["name"]: c["code_merges_these_on_its_own"] for c in task["may_be_the_same_as"]} == {
        "Acme": True, "Northwind": False}, "Staffco is the agency of one of Acme Labs' roles: not offered"


async def test_nothing_waiting_says_to_stop_and_a_second_session_is_handed_a_different_employer(js):
    await _world()
    one, two = await _employer("Acme"), await _employer("Beta")
    async with db.session_factory()() as session:
        assert (await R.task_reply(session, OWNER))["task"] is None
    await _send(one.id, two.id)
    async with db.session_factory()() as session:
        first = (await R.task_reply(session, OWNER))["employer"]["id"]
        await session.commit()
    async with db.session_factory()() as session:
        second = (await R.task_reply(session, OWNER))["employer"]["id"]
        await session.commit()
        third = await R.task_reply(session, OWNER)
    assert {first, second} == {str(one.id), str(two.id)} and first != second
    assert third["task"] is None and "No employer is waiting" in third["note"]


async def test_an_employer_taken_over_and_over_and_never_answered_fails_and_a_silent_session_is_swept(js):
    await _world()
    stuck, queued = await _employer("Acme"), await _employer("Beta")
    await _send(stuck.id)
    for hours in range(hand_outs.MAX_CLAIMS):  # a session takes it, goes silent, and the lease runs out
        async with db.session_factory()() as session:
            assert (await R.claim_next(session, OWNER, datetime.now(UTC) + timedelta(hours=hours))) is not None
            await session.commit()
    async with db.session_factory()() as session:
        assert await R.claim_next(session, OWNER, datetime.now(UTC) + timedelta(hours=9)) is None
        await session.commit()
    assert (await _get(Employer, stuck.id)).research_state == R.FAILED, "no session managed to send it"
    await _send(queued.id)
    async with db.session_factory()() as session:
        assert await R.settle_stuck(session, OWNER, now=datetime.now(UTC) + timedelta(hours=3)) == 0, (
            "nobody took it: it waits in line")
        assert (await R.claim_next(session, OWNER)).id == queued.id
        await session.commit()
        assert await R.settle_stuck(session, OWNER, now=datetime.now(UTC) + timedelta(hours=3)) == 1
        await session.commit()
    assert (await _get(Employer, queued.id)).research_state == R.FAILED
    async with db.session_factory()() as session:
        assert "didn't finish" in (await session.scalars(select(TrailEvent).where(
            TrailEvent.event == "employer_research_failed"))).first().sentence


# --- what code checks of a report (36.3) ----------------------------------------------------------------------------


async def test_a_report_with_a_job_board_careers_link_a_word_for_employees_and_a_fact_with_no_source_keeps_the_rest(js):
    await _world()
    acme = await _employer("Acme")
    await _send(acme.id)
    facts = {**FOUND, "careers_url": "https://www.board.test/company/acme/jobs", "employees": "a lot",
             "headquarters": "Oakfield, OH", "offices": ["Oakfield, OH", "Riverton, OH"]}
    sources = {k: v for k, v in SOURCES.items() if k != "headquarters"} | {
        "careers_url": "https://www.acme-corp.test/careers", "employees": "its About page", "offices": "its site"}
    reply = await _report(acme.id, facts, sources)
    refused = {r["field"]: r["reason"] for r in reply["refused"]}
    assert set(refused) == {"careers_url", "employees", "headquarters"}, "those three, and no other"
    assert "job board" in refused["careers_url"] and "integer" in refused["employees"]
    assert "no source" in refused["headquarters"]
    assert sorted(reply["set"]) == ["founded", "industry", "offices", "ownership", "website"]
    kept = await _get(Employer, acme.id)
    assert (kept.industry, kept.ownership, kept.founded, kept.offices) == (
        "Software", "private", 1990, ["Oakfield, OH", "Riverton, OH"])
    assert kept.employees is None and kept.headquarters is None and kept.careers_url is None


async def test_each_fact_names_where_it_came_from_and_who_set_it(js):
    await _world()
    acme = await _employer("Acme")
    await _send(acme.id)
    await _report(acme.id)
    kept = await _get(Employer, acme.id)
    assert kept.sources["employees"]["source"] == "https://about.example.test/employees"
    assert kept.sources["employees"]["by"] == "Researcher" and kept.sources["employees"]["at"]
    assert set(kept.sources) == set(FOUND)
    assert kept.research["set"] and kept.research_state == R.DONE and kept.researched_at is not None


async def test_research_that_looked_for_layoffs_and_found_none_marks_the_record_checked(js):
    from datetime import date

    from app.jobsearch.employers import core as employers
    await _world()
    acme = await _employer("Acme")
    await _send(acme.id)
    reply = await _report(acme.id, {**FOUND, "events": []}, {**SOURCES, "events": "news search, no layoff found"})
    assert "events" in reply["set"] and not reply["refused"]
    kept = await _get(Employer, acme.id)
    assert kept.events == [] and kept.sources["events"]["source"] == "news search, no layoff found"
    facts = employers.facts(kept, date(2026, 6, 1))
    assert "years_since_layoff" in facts and facts["years_since_layoff"] is None, "checked, none: the ask is not met"


async def test_research_that_did_not_look_for_layoffs_leaves_the_fact_unknown(js):
    from datetime import date

    from app.jobsearch.employers import core as employers
    await _world()
    acme = await _employer("Acme")
    await _send(acme.id)
    reply = await _report(acme.id, {**FOUND, "events": []}, SOURCES)  # an empty list with no source is not a search
    assert [r["field"] for r in reply["refused"]] == ["events"]
    kept = await _get(Employer, acme.id)
    assert "events" not in (kept.sources or {})
    assert "years_since_layoff" not in employers.facts(kept, date(2026, 6, 1))
    other = await _employer("Initech")
    await _send(other.id)
    await _report(other.id, FOUND, SOURCES)  # said nothing of layoffs at all
    assert "years_since_layoff" not in employers.facts(await _get(Employer, other.id), date(2026, 6, 1))


async def test_a_field_the_owner_set_is_never_changed_and_a_careers_site_is_kept_only_where_none_is(js):
    await _world()
    mine = await _employer("Acme", employees=50, owner_fields=["employees"], careers_url="https://jobs.acme-corp.test")
    await _send(mine.id)
    reply = await _report(mine.id, {**FOUND, "careers_url": "https://www.acme-corp.test/careers"},
                          {**SOURCES, "careers_url": "https://www.acme-corp.test/careers"})
    kept = await _get(Employer, mine.id)
    assert (kept.employees, kept.careers_url) == (50, "https://jobs.acme-corp.test"), "the owner's word, the first site"
    assert sorted(reply["left_alone"]) == ["careers_url", "employees"] and "employees" not in reply["set"]
    assert kept.research["left_alone"] == reply["left_alone"] and kept.industry == "Software"
    fresh = await _employer("Beta")
    await _send(fresh.id)
    await _report(fresh.id,
                  {"website": "https://www.beta-corp.test", "careers_url": "https://www.beta-corp.test/careers"},
                  {"website": "its site", "careers_url": "its site"})
    assert (await _get(Employer, fresh.id)).careers_url == "https://www.beta-corp.test/careers", "where there is none"


@pytest.mark.parametrize(("fact", "value", "why"), [
    ("website", "https://www.board.test/company/acme", "job board"),
    ("website", "https://acme.wd5.tenantsys.test/External", "job board"),
    ("website", "javascript:alert(1)", "web address"),
    ("website", "http://localhost/admin", "public web address"),
    ("careers_url", "https://careers.another-corp.test/jobs", "not the employer's own domain"),
    ("reviews_url", "http://reviews.example.test/overview/acme", "public https address"),
    ("employees", True, "number"),
    ("employees", -5, "greater than or equal to 0"),
    ("founded", 900, "greater than or equal to 1000"),
    ("ownership", "a cooperative", "Input should be"),
    ("ai_native", "maybe", "boolean"),
    ("offices", "Oakfield", "valid list"),
])
async def test_each_kind_of_fact_is_held_to_its_own_rule_and_refused_with_its_reason(js, fact, value, why):
    await _world()
    acme = await _employer("Acme", website="https://www.acme-corp.test")
    await _send(acme.id)
    reply = await _report(acme.id, {fact: value, "industry": "Software"}, {fact: "somewhere", "industry": "its site"})
    assert [r["field"] for r in reply["refused"]] == [fact] and why in reply["refused"][0]["reason"]
    assert reply["set"] == ["industry"], "the rest stands"


async def test_a_careers_site_on_a_known_job_system_and_a_reviews_page_a_search_showed_are_kept(js):
    await _world()
    acme = await _employer("Acme")
    await _send(acme.id)
    reply = await _report(acme.id, {
        "website": "https://www.acme-corp.test", "careers_url": "https://boards.feedsys.test/acme",
        "reviews_url": "https://reviews.example.test/overview/working-at-acme.htm", "ownership": "unknown"},
        {"website": "its site", "careers_url": "its site", "reviews_url": "a web search result"})
    assert reply["refused"] == [] and sorted(reply["set"]) == ["careers_url", "reviews_url", "website"], (
        "an ownership of unknown is no finding: it is left out, not refused")
    kept = await _get(Employer, acme.id)
    assert kept.reviews_url.startswith("https://reviews.example.test/") and kept.ownership == "unknown"


async def test_a_report_for_an_employer_not_waiting_or_that_is_not_a_report_is_refused_whole(js):
    await _world()
    acme = await _employer("Acme")
    async with db.session_factory()() as session:
        for body, words in (({"employer": str(acme.id), "facts": FOUND}, "isn't waiting"),
                            ({"employer": "not an id"}, "no such employer"), ("a string", "an object")):
            with pytest.raises(R.ResearchError, match=words):
                await R.submit(session, OWNER, body)
    await _send(acme.id)
    async with db.session_factory()() as session:
        with pytest.raises(R.ResearchError, match="not a report"):
            await R.submit(session, OWNER, {"employer": str(acme.id)})
    assert (await _get(Employer, acme.id)).research_state == R.WAITING
    reply = await _report(acme.id, {})
    assert reply["taken"] and reply["set"] == [], "none found is a report: the employer was looked at"
    assert (await _get(Employer, acme.id)).research_state == R.DONE


# --- the job board --------------------------------------------------------------------------------------------------


async def test_a_job_board_the_researcher_read_is_kept_when_it_names_a_known_system_no_other_employer_holds(js):
    await _world()
    acme = await _employer("Acme")
    other = await _employer("Initech", ats="feedsys", board={"slug": "initech"})
    await _send(acme.id)
    reply = await _report(acme.id, {}, {"board_url": "its careers page links to it"},
                          board_url="https://boards.feedsys.test/acme")
    assert reply["refused"] == []
    kept = await _get(Employer, acme.id)
    assert (kept.ats, kept.board["slug"], kept.ats_found_by) == ("feedsys", "acme", "analyst")
    assert kept.board_url and kept.careers_url == kept.board_url, "a careers site the board gives, where none is kept"
    assert (await _get(Employer, other.id)).board == {"slug": "initech"}


@pytest.mark.parametrize(("link", "source", "why"), [
    ("https://www.board.test/company/acme", "its page", "job board"),
    ("https://www.acme-corp.test/careers", "its page", "names no job system the app knows"),
    ("https://boards.feedsys.test/initech", "its page", "another employer's"),
    ("https://boards.feedsys.test/acme", "", "no source"),
    ("http://localhost/jobs", "its page", "public web address"),
])
async def test_a_job_board_that_is_a_job_board_unknown_another_employers_or_unsourced_is_refused(js, link, source, why):
    await _world()
    acme = await _employer("Acme")
    await _employer("Initech", ats="feedsys", board={"slug": "initech"})
    await _send(acme.id)
    reply = await _report(acme.id, {"industry": "Software"}, {"industry": "its site", "board_url": source},
                          board_url=link)
    assert [r["field"] for r in reply["refused"]] == ["board_url"] and why in reply["refused"][0]["reason"]
    kept = await _get(Employer, acme.id)
    assert kept.ats is None and kept.industry == "Software"


async def test_the_app_looks_for_the_board_itself_from_the_careers_page_the_researcher_found(js, no_network):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/careers":
            return httpx.Response(200, html='<html><a href="https://boards.feedsys.test/acme">Jobs</a></html>')
        return httpx.Response(404)

    no_network(handle)
    await _world()
    acme = await _employer("Acme")
    await _send(acme.id)
    reply = await _report(acme.id, {"website": "https://www.acme-corp.test",
                                    "careers_url": "https://www.acme-corp.test/careers"},
                          {"website": "its site", "careers_url": "its site"})
    assert reply["refused"] == []
    kept = await _get(Employer, acme.id)
    assert (kept.ats, kept.board) == ("feedsys", {"slug": "acme"}) and kept.ats_found_by == "link"
    assert kept.research["came_to"]["board"] is True


async def test_a_board_the_owner_set_is_never_changed_by_what_the_researcher_found(js):
    await _world()
    mine = await _employer("Acme", ats="slugsys", board={"slug": "acme-owner"}, ats_found_by="owner",
                           owner_fields=["ats"])
    await _send(mine.id)
    await _report(mine.id, {}, {"board_url": "its page"}, board_url="https://boards.feedsys.test/acme")
    kept = await _get(Employer, mine.id)
    assert (kept.ats, kept.board, kept.ats_found_by) == ("slugsys", {"slug": "acme-owner"}, "owner")


# --- duplicates and splits, through the app's own feature (36.5, 36.6, 39.19) ---------------------------------------


async def test_two_employers_that_share_one_website_beside_alike_names_merge_by_themselves_every_role_kept(js):
    await _world()
    big = await _employer("Initech Labs", ["Lead A", "Lead B"], website="https://www.initech-corp.test")
    small = await _employer("Initech", ["Lead C"])
    await _send(small.id)
    reply = await _report(small.id, {"website": "https://initech-corp.test/about"}, {"website": "its About page"})
    assert reply["taken"]
    assert await _names() == ["Initech Labs"], "one record left: the one with more roles"
    left = await _get(Employer, big.id)
    assert "Initech" in left.aliases and left.researched_at is not None, "the other name kept as one it is called"
    assert sorted(await _roles(big.id)) == ["Lead A", "Lead B", "Lead C"], "every role kept"
    async with db.session_factory()() as session:
        merged = (await session.scalars(select(TrailEvent).where(TrailEvent.event == "employer_merged"))).one()
        done = (await session.scalars(select(TrailEvent).where(TrailEvent.event == "employer_researched"))).one()
    assert "they share the website initech-corp.test and their names are alike" in merged.sentence
    assert merged.actor == "app"
    assert done.link_id == big.id and "merged 1 duplicate" in done.sentence, "Activity says it, on the record left"


async def test_a_website_the_researcher_found_and_one_the_owner_set_confirm_a_pair_whatever_its_names(js):
    await _world()
    big = await _employer("Initech", ["Lead A", "Lead B"], website="https://www.initech-corp.test",
                          owner_fields=["website"], sources={"website": BY_YOU})
    small = await _employer("Initrode", ["Lead C"])
    await _send(small.id)
    reply = await _report(small.id, {"website": "https://initech-corp.test/about"}, {"website": "its About page"})
    assert reply["taken"]
    assert await _names() == ["Initech"], "the researcher's word and the owner's word agree: one employer"
    assert sorted(await _roles(big.id)) == ["Lead A", "Lead B", "Lead C"]
    async with db.session_factory()() as session:
        merged = (await session.scalars(select(TrailEvent).where(TrailEvent.event == "employer_merged"))).one()
    assert "which you or the researcher confirmed for both" in merged.sentence


async def test_a_website_only_the_analyst_set_is_not_confirmed_by_the_researcher_agreeing_with_it(js):
    """Two unalike names on one website, one set by the per-role analyst: the researcher finds the same address for the
    other. That is one confirmed side and one not: no merge here, and the night keeps them as two."""
    await _world()
    analyst = {"website": {"source": "a posting it read", "at": "2026-10-13T00:00:00+00:00", "by": "analyst"}}
    await _employer("Initech", ["Lead A", "Lead B"], website="https://www.initech-corp.test", sources=analyst)
    small = await _employer("Initrode", ["Lead C"])
    await _send(small.id)
    reply = await _report(small.id, {"website": "https://initech-corp.test/about"}, {"website": "its About page"})
    assert reply["taken"] and await _names() == ["Initech", "Initrode"]
    async with db.session_factory()() as session:
        (pair,) = await employer_pairs.candidates(session, OWNER)
        assert (pair.decision.action, pair.decision.rule) == ("never", "website_unconfirmed")
        assert await employer_pairs.settle(session, OWNER) == {"merged": 0}
    assert await _names() == ["Initech", "Initrode"]


async def test_two_employers_on_one_job_board_with_names_that_look_alike_merge_by_themselves(js):
    await _world()
    big = await _employer("Northwind", ["Lead A", "Lead B"], ats="feedsys", board={"slug": "northwind"})
    small = await _employer("Northwind Labs", ["Lead C"])
    await _send(small.id)
    reply = await _report(small.id, {}, {"board_url": "its careers page"},
                          board_url="https://boards.feedsys.test/northwind")
    assert reply["refused"] == []
    left = await _get(Employer, big.id)
    assert await _names() == ["Northwind Labs"], "one record, under the fuller name"
    assert "Northwind" in left.aliases, "the other name kept as one it is also called"
    assert sorted(await _roles(big.id)) == ["Lead A", "Lead B", "Lead C"], "every role kept"


async def test_a_pair_with_a_like_name_and_nothing_shared_stays_two_employers_and_nobody_is_asked(js):
    await _world()
    fab = await _employer("Fabrikam", ["Lead A"])
    dental = await _employer("Fabrikam Dental", ["Lead B"])
    await _send(fab.id)
    reply = await _report(fab.id, {}, {}, same_as=[{"employer": str(dental.id), "verdict": "same", "reasons": [
        "Its About page says Fabrikam Dental is Fabrikam's dental arm"]}])
    assert reply["refused"] == []
    assert await _names() == ["Fabrikam", "Fabrikam Dental"], "nothing merged: the researcher's word alone never does"
    async with db.session_factory()() as session:
        assert (await session.scalars(select(Task).where(Task.kind == employer_pairs.KIND_MERGE))).all() == []
        fresh = await session.get(Employer, fab.id)
        assert fresh.research["came_to"]["splits"] == 0 and "asked" not in fresh.research["came_to"]
        assert "asked you" not in (await session.scalars(select(TrailEvent).where(
            TrailEvent.event == "employer_researched"))).one().sentence


async def test_a_pair_the_researcher_says_is_different_is_remembered_and_never_a_candidate_again(js):
    await _world()
    fab = await _employer("Fabrikam", ["Lead A"])
    dental = await _employer("Fabrikam Dental", ["Lead B"])
    await _send(fab.id)
    await _report(fab.id, {}, {}, same_as=[{"employer": str(dental.id), "verdict": "different", "reasons": [
        "A dental practice, not the company"]}])
    async with db.session_factory()() as session:
        remembered = (await session.scalars(select(Task).where(Task.kind == employer_pairs.KIND_MERGE))).one()
        assert remembered.state == TASK_DONE and remembered.answer == employer_pairs.SEPARATE
        assert await employer_pairs.candidates(session, OWNER) == []
    assert await _names() == ["Fabrikam", "Fabrikam Dental"]


async def test_a_verdict_on_an_employer_the_task_did_not_offer_is_refused_and_the_rest_of_the_report_stands(js):
    await _world()
    acme = await _employer("Acme", ["Lead A"])
    stranger = await _employer("Hooli")
    await _send(acme.id)
    reply = await _report(acme.id, {"industry": "Software"}, {"industry": "its site"}, same_as=[
        {"employer": str(stranger.id), "verdict": "same", "reasons": ["a hunch"]},
        {"employer": "x", "verdict": "maybe"}],
        split=[{"roles": ["not a role"], "new_name": "Contoso", "reasons": ["a hunch"]}])
    assert sorted(r["field"] for r in reply["refused"]) == ["same_as 1", "same_as 2", "split 1"]
    assert reply["set"] == ["industry"] and await _names() == ["Acme", "Hooli"]


async def _address(employer_id, titles, url):
    """Give these roles of the employer the address they were found on."""
    async with db.session_factory()() as session:
        for role in (await session.scalars(select(Opportunity).where(
                Opportunity.employer_id == employer_id, Opportunity.title.in_(titles)))).all():
            role.apply_url = url
        await session.commit()


async def test_a_split_the_researcher_proposes_moves_the_roles_whose_own_postings_show_the_other_company(js):
    await _world()
    north = await _employer("Northwind", ["Lead A", "Lead B", "Lead C"])
    await _address(north.id, ["Lead A", "Lead B"], "https://careers.contoso.test/jobs/1")
    roles = await _roles(north.id)
    await _send(north.id)
    both = [str(roles["Lead A"].id), str(roles["Lead B"].id), str(roles["Lead C"].id)]
    reply = await _report(north.id, {}, {}, split=[{"roles": both, "new_name": "Contoso",
                                                    "reasons": ["Their postings are on contoso.test"]}])
    assert reply["refused"] == []
    assert await _names() == ["Contoso", "Northwind"]
    assert sorted(await _roles(north.id)) == ["Lead C"], "the role with no address that shows it stayed"
    contoso = next(e for e in await _all_employers() if e.name == "Contoso")
    assert sorted(await _roles(contoso.id)) == ["Lead A", "Lead B"], "the roles moved to a record with its own name"
    async with db.session_factory()() as session:
        asked = (await session.scalars(select(Task).where(Task.kind == employer_pairs.KIND_SPLIT))).all()
        assert asked == [], "nobody is asked"
        assert (await session.get(Employer, north.id)).research["came_to"]["splits"] == 1  # type: ignore[union-attr]


async def test_a_split_the_researcher_proposes_moves_nothing_and_asks_nobody_when_the_postings_do_not_show_it(js):
    await _world()
    north = await _employer("Northwind", ["Lead A", "Lead B", "Lead C"])
    await _address(north.id, ["Lead A", "Lead B"], "https://www.board.test/jobs/view/1")
    roles = await _roles(north.id)
    await _send(north.id)
    both = [str(roles["Lead A"].id), str(roles["Lead B"].id)]
    reply = await _report(north.id, {}, {}, split=[{"roles": both, "new_name": "Contoso",
                                                    "reasons": ["Their postings are on contoso.test"]}])
    assert reply["refused"] == []
    assert await _names() == ["Northwind"] and sorted(await _roles(north.id)) == ["Lead A", "Lead B", "Lead C"]
    async with db.session_factory()() as session:
        assert (await session.scalars(select(Task))).all() == []
        assert (await session.get(Employer, north.id)).research["came_to"]["splits"] == 0  # type: ignore[union-attr]


def test_a_split_is_not_a_question_for_the_owner_in_the_words_of_a_run():
    rows = [Employer(name="A", research_state=employer_research.DONE,
                     research={"came_to": {"splits": 2, "merged": 0, "board": False}})]
    assert "question" not in employer_research.run_words(rows, 1)
    assert employer_research.run_words(rows, 1) == "Researched 1 of 1 employer."
    said = employer_research._sentence(Employer(name="A"), {}, {"board": False, "merged": 0, "splits": 2})
    assert "moved roles to another employer" in said and "question" not in said


async def _all_employers() -> list[Employer]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(Employer))).all())


async def test_the_researchers_merge_is_the_one_the_employer_page_uses(js):
    """`propose_merge` and the page's "Merge into..." both end in `employer_records.merge` (docs/PRD.md 36.7)."""
    await _world()
    big = await _employer("Initech", ["Lead A"], website="https://www.initech-corp.test")
    small = await _employer("Initech Labs", ["Lead B"], website="https://initech-corp.test")
    async with db.session_factory()() as session:
        done = await employer_pairs.propose_merge(
            session, OWNER, small.id, big.id, employer_pairs.Evidence("same", ("one site",)), by=R.BY)
        await session.commit()
        assert done.outcome == employer_pairs.MERGED and done.employer.id == big.id
        event = (await session.scalars(select(TrailEvent).where(TrailEvent.event == "employer_merged"))).one()
    assert event.who == "Researcher", "Activity names the researcher"


# --- the gates and the scores again, on the facts (39.18, 39.19) ----------------------------------------------------


async def test_a_researched_role_whose_employer_is_pe_owned_goes_to_gated_out_with_that_gate_and_the_version(js):
    await _world(gates=GATES)
    acme = await _employer("Acme", [("Lead A", stages.SECOND_IMPRESSION), ("Lead B", stages.READY_TO_LAUNCH),
                                    ("Lead C", stages.APPLIED), ("Lead D", stages.SECOND_IMPRESSION)])
    async with db.session_factory()() as session:
        roles = {r.title: r for r in (await session.scalars(select(Opportunity))).all()}
        await trail.record(session, OWNER, event="role_moved", actor="person", sentence="You moved it.",
                           link=("opportunity", roles["Lead D"].id))
        hard_pass = await profile_store.newest_version(session, OWNER, "gates")
        await session.commit()
    assert (await _roles())["Lead A"].status == stages.SECOND_IMPRESSION, "a fact not known passes"
    await _send(acme.id)
    await _report(acme.id, {"ownership": "pe"}, {"ownership": "its About page"})
    after = await _roles()
    assert after["Lead A"].status == stages.GATED_OUT and after["Lead B"].status == stages.GATED_OUT
    stopped = after["Lead A"]
    assert (stopped.screened_out_by, stopped.screened_out_gate_id, stopped.screened_out_step) == (
        "employer", "no-pe", "gate")
    assert stopped.screened_out_hard_pass == hard_pass and "ownership pe" in stopped.screened_out_reason
    assert after["Lead C"].status == stages.APPLIED, "Applied or later is never moved"
    assert after["Lead D"].status == stages.SECOND_IMPRESSION, "a role the owner moved by hand stays where it is"
    async with db.session_factory()() as session:
        said = (await session.scalars(select(TrailEvent).where(
            TrailEvent.event == "regate_stopped", TrailEvent.link_id == stopped.id))).one()
        line = (await session.scalars(select(TrailEvent).where(TrailEvent.event == "employer_researched"))).one()
    assert "Acme was researched" in said.detail and "passed them before" in said.detail, "its history says it passed"
    assert "2 moved" in line.detail


async def test_a_role_only_the_gate_stopped_comes_back_when_a_better_fact_clears_it(js):
    await _world(gates=GATES)
    acme = await _employer("Acme", ["Lead A"])
    await _send(acme.id)
    await _report(acme.id, {"ownership": "pe"}, {"ownership": "its About page"})
    assert (await _roles())["Lead A"].status == stages.GATED_OUT
    await _send(acme.id)
    await _report(acme.id, {"ownership": "private"}, {"ownership": "the company's own site"})
    back = (await _roles())["Lead A"]
    assert back.status == stages.SECOND_IMPRESSION
    assert back.screened_out_reason is None and back.screened_out_gate_id is None
    async with db.session_factory()() as session:
        assert (await session.scalars(select(TrailEvent).where(TrailEvent.event == "regate_released"))).one()


async def test_a_researched_roles_score_is_worked_out_again_from_the_facts_the_research_brought(js):
    small = {"id": "ask-e", "section": "company", "says": "Under 1,000 employees", "points": 4,
             "fact": "employees", "op": "<", "value": 1000}
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=NO_GATES, scoring={"asks": [*FIVE["asks"], small]},
                           settings=PROFILE["settings"])
        await roster.ensure_roster(session, OWNER)
        profile = await profile_store.current(session, OWNER)
        acme = await make_employer(session, "Fabrikam")
        role = make_role(acme, "Lead A")
        role.fit = None
        role.fit_breakdown = {"answers": scoring.record([], answered("met", "met", "not")["asks"], profile, "reader")}
        role.analysis_state, role.analysis = prioritize.RESEARCHED, {"required": [{"item": "x", "class": "P"}]}
        session.add(role)
        await session.flush()
        prioritize.work_out(role, profile, employer=acme, version=1)
        before = role.fit
        await session.commit()
        employer_id = acme.id
    await _send(employer_id)
    await _report(employer_id, {"employees": 300}, {"employees": "its About page"})
    after = (await _roles())["Lead A"]
    assert after.fit == before + 4, (
        "the small-employer ask was 'can't tell' and earned nothing, and now holds in full (4)")
    assert after.analysis_state == prioritize.RESEARCHED and after.status == stages.SECOND_IMPRESSION


async def test_a_merge_by_hand_works_the_gates_of_the_moved_roles_out_again(js):
    await _world(gates=GATES)
    keep = await _employer("Acme", ["Lead A"], ownership="pe")
    away = await _employer("Acme Labs", ["Lead B"], ownership="private")
    async with db.session_factory()() as session:
        await employer_records.merge(session, OWNER, keep.id, away.id, why="you merged them by hand")
        await session.commit()
    roles = await _roles()
    assert {t: r.status for t, r in roles.items()} == {"Lead A": stages.GATED_OUT, "Lead B": stages.GATED_OUT}, (
        "both are at the PE-owned record now, and the gate runs again on it")
    assert roles["Lead B"].screened_out_gate_id == "no-pe"


async def test_a_split_works_the_gates_of_the_moved_roles_out_again_but_leaves_those_the_old_hard_pass_stopped(js):
    await _world(gates=GATES)
    north = await _employer("Northwind", ["Lead A", "Lead B"], ownership="private")
    roles = await _roles(north.id)
    async with db.session_factory()() as session:
        await employer_records.split(session, OWNER, north.id, role_ids=[roles["Lead A"].id], new_name="Contoso",
                                     why="a test")
        contoso = (await session.scalars(select(Employer).where(Employer.name == "Contoso"))).one()
        contoso.ownership = "pe"
        await session.flush()
        await employer_records.split(session, OWNER, north.id, role_ids=[roles["Lead B"].id], into_id=contoso.id,
                                     why="a test")
        await session.commit()
    stopped = (await _roles())["Lead B"]
    assert stopped.status == stages.GATED_OUT and stopped.screened_out_gate_id == "no-pe"


# --- a gate or an ask that reads a fact a record lacks (39.18) ------------------------------------------------------


async def test_a_new_gate_on_a_fact_a_record_lacks_sends_the_employers_of_the_roles_it_reaches_back_to_research(js):
    await _world()
    lately = datetime.now(UTC) - timedelta(days=2)
    lacks = await _employer("Acme", ["Lead A"], researched_at=lately)
    has = await _employer("Beta", ["Lead B"], researched_at=lately, ownership="private")
    no_role = await _employer("Cobalt", (), researched_at=lately)
    gated_out = await _employer("Delta", [("Lead D", stages.GATED_OUT)], researched_at=lately)
    said: list[str] = []
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "gates", GATES, by="owner", why="never PE", said=said)
        await session.commit()
    assert said == [R.said_for(1)] and said[0].startswith("1 employer lacks a fact"), "the save says what waits"
    assert (await _get(Employer, lacks.id)).researched_at is None, "put back in line for the next night"
    for kept in (has, no_role, gated_out):
        assert (await _get(Employer, kept.id)).researched_at is not None
    async with db.session_factory()() as session:
        assert [e.name for e in await R.picks(session, OWNER, None, 90)] == ["Acme"]
        lacks_now = await session.get(Employer, lacks.id)
        lacks_now.researched_at = lately
        again: list[str] = []  # saved again as it is: nothing new is read, nothing is sent
        await profile_store.save(session, OWNER, "gates", GATES, by="owner", said=again)
        assert again == [] and lacks_now.researched_at == lately


async def test_a_gate_that_was_already_there_sends_no_one_and_a_gate_for_another_tab_reaches_only_its_roles(js):
    tabs = {**PROFILE["criteria"], "target_roles": [
        {**PROFILE["criteria"]["target_roles"][0], "id": "builders", "name": "Builders"},
        {**PROFILE["criteria"]["target_roles"][0], "id": "analysts", "name": "Analysts", "titles": ["Analyst"]}]}
    await _world(criteria=tabs)
    lately = datetime.now(UTC) - timedelta(days=2)
    builder = await _employer("Acme", researched_at=lately)
    analyst = await _employer("Beta", researched_at=lately)
    async with db.session_factory()() as session:
        session.add_all([make_role(builder, "Lead A", target_role="builders"),
                         make_role(analyst, "Lead B", target_role="analysts")])
        await session.commit()
    mine = {**NO_PE, "roles": ["builders"]}
    said: list[str] = []
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "gates", {"employer_names": [], "other": [mine]}, by="owner",
                                 said=said)
        await session.commit()
    assert (await _get(Employer, builder.id)).researched_at is None, "the tab it is for"
    assert (await _get(Employer, analyst.id)).researched_at is not None, "a tab it is not for"
    assert said == [R.said_for(1)]


async def test_saving_hard_pass_over_http_says_what_was_checked_and_what_waits_for_tonight(client):
    await _world()
    await _employer("Acme", ["Lead A"], researched_at=datetime.now(UTC) - timedelta(days=2))
    saved = await client.put("/api/v1/profile/gates", json={"data": GATES, "why": "never PE"})
    assert saved.status_code == 200, saved.text
    said = saved.json()["said"]
    assert said.startswith("Re-checked ") and said.endswith("the researcher looks for it tonight."), said


async def test_a_new_ask_on_a_fact_a_record_lacks_does_the_same(js):
    await _world()
    lately = datetime.now(UTC) - timedelta(days=2)
    acme = await _employer("Acme", ["Lead A"], researched_at=lately)
    small = {"id": "ask-e", "section": "company", "says": "Under 1,000 employees", "points": 1,
             "fact": "employees", "op": "<", "value": 1000}
    said: list[str] = []
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "scoring", {"asks": [*FIVE["asks"], small]}, by="owner", said=said)
        await session.commit()
    assert (await _get(Employer, acme.id)).researched_at is None
    assert said[-1] == R.said_for(1)


async def test_a_new_row_whose_statements_read_a_fact_a_record_lacks_does_the_same(js):
    await _world()
    lately = datetime.now(UTC) - timedelta(days=2)
    acme = await _employer("Acme", ["Lead A"], researched_at=lately)
    beta = await _employer("Beta", ["Lead B"], researched_at=lately)
    first_match = {"id": "size", "kind": "best", "section": "company", "members": [
        {"id": "small", "says": "Under 200 employees", "points": 2, "fact": "employees", "op": "<", "value": 200,
         "checked_by": "app"},
        {"id": "medium", "says": "Under 1,000 employees", "points": 1, "fact": "employees", "op": "<", "value": 1000,
         "checked_by": "app"}]}
    said: list[str] = []
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "scoring", {"asks": FIVE["asks"], "groups": [first_match]},
                                 by="owner", said=said)
        await session.commit()
    for employer in (acme, beta):
        assert (await _get(Employer, employer.id)).researched_at is None, "both go back in line"
    assert said[-1] == R.said_for(2)
    async with db.session_factory()() as session:
        profile = await profile_store.current(session, OWNER)
    assert R.all_facts_read(profile) == {"employees"}, "so the researcher is told to look for it"


async def test_an_any_of_with_one_statement_that_reads_a_fact_asks_for_that_fact_too(js):
    await _world()
    any_of = {"id": "known", "kind": "any", "section": "company", "points": 1, "members": [
        {"id": "public", "says": "The employer is a public agency"},
        {"id": "founded", "says": "Founded before 1990", "fact": "years_since_founded", "op": ">", "value": 35,
         "checked_by": "app"}]}
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "scoring", {"asks": FIVE["asks"], "groups": [any_of]}, by="owner")
        profile = await profile_store.current(session, OWNER)
    assert R.all_facts_read(profile) == {"years_since_founded"}


# --- who is told, and what the Agents page says ---------------------------------------------------------------------


async def test_the_runner_is_told_researcher_before_analysis(js):
    await _world()
    acme = await _employer("Acme")
    async with db.session_factory()() as session:
        role = make_role(await session.get(Employer, acme.id), "Lead A", description=POSTING_WORDS)
        session.add(role)
        await session.flush()
        await analysis.start(session, OWNER, role)
        await session.commit()
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.ANALYSIS
    await _send(acme.id)
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.RESEARCHER, (
            "the employers' facts come first")
        assert (await R.claim_next(session, OWNER)).id == acme.id
        await session.commit()
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.ANALYSIS, (
            "taken: the roles' research is next")


async def test_a_session_of_the_researcher_shows_on_the_agents_page_as_researching_the_employer(js):
    await _world()
    acme = await _employer("Acme")
    await _send(acme.id)
    async with db.session_factory()() as session:
        await roster.touch(session, OWNER, alive=True, kind=roster.RESEARCHER)
        conn = await roster.connection_by_slug(session, OWNER, roster.SCOUT)
        shown = await roster.work_of(session, OWNER, conn)
        await session.commit()
    assert (shown.kind, shown.title) == (roster.RESEARCHER, "Researching Acme")
    assert roster._status(conn, shown, datetime.now(UTC))[0] == "working"


async def test_the_researchers_process_is_the_analysts_role_with_an_instruction_file_of_its_own(js):
    from app.jobsearch import process

    steps = [e for e in process.library().get(process.RESEARCH_EMPLOYER).elements.values() if e.kind == AGENT]
    assert [(e.id, e.role, e.instructions, e.template) for e in steps] == [
        (process.RESEARCHER_STEP, "analyst", "jobsearch.researcher", "jobsearch.researcher.report")]
    await _world()
    acme = await _employer("Acme")
    await _send(acme.id)
    async with db.session_factory()() as session:
        instance = (await session.get(Employer, acme.id)).research_instance_id
        assert await process.engine().where_is(session, instance) is not None, "it waits at the researcher's step"
