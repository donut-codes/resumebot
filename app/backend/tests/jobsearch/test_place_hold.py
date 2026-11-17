"""A work location gate that cannot be decided holds a read role off the deck, and a role that holds only a count of
places ("2 Locations") takes the places its own postings name. Found in use: roles sat in Ready to
Launch with the role page's gates card showing the place as undecided, because an unknown passes and only a dial's
Must held a role back. Every place, firm and name here is made up; nothing reaches the network."""

import copy
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.gating import check as gating
from app.jobsearch.gating import regate, stated_place
from app.jobsearch.models import JobPosting, Opportunity
from app.jobsearch.pipeline import deck as pipeline
from app.jobsearch.pipeline import dial_hold
from app.models.work import TrailEvent
from app.timeutil import owner_today
from tests.builders import counted
from tests.support import OWNER, PROFILE, load_profile

FIRST, SECOND, LAUNCH = stages.FIRST_IMPRESSION, stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH
#: A town in a job system's order (country, state, city) that the owner did not list: only remote can pass there.
FENWICK = "US, WA, Fenwick"


def _role(slug: str, status: str, location: str | None, mode: str | None = None, place: str | None = "stated",
          **more) -> Opportunity:
    today = owner_today()
    research = {"place": place} if place else {}
    return Opportunity(**{
        "owner": OWNER, "slug": slug, "company": f"Firm {slug}", "title": f"Lead {slug}", "origin": "scout",
        "match_key": f"firm {slug}|lead {slug}", "status": status, "found": today, "posted": today - timedelta(days=1),
        "location": location, "move_type": mode, "fit": 80, "fit_breakdown": {"math": 1, "percent": 80, "asks": []},
        **{**counted(**research), **more}})


async def _world(*roles: Opportunity, postings: dict[str, list[str]] | None = None) -> None:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=copy.deepcopy(PROFILE["gates"]),
                           settings={**PROFILE["settings"], "apply_per_day": 5})
        session.add_all(roles)
        await session.flush()
        now = datetime.now(UTC)
        for slug, places in (postings or {}).items():
            role = next(r for r in roles if r.slug == slug)
            for n, place in enumerate(places):
                session.add(JobPosting(owner=OWNER, employer_id=uuid.uuid4(), ats="tenantone", ext_id=f"{slug}-{n}",
                                       company=role.company, title=role.title, match_key=role.match_key,
                                       location=place, links=[], first_seen=now + timedelta(seconds=n),
                                       last_seen=now, opportunity_id=role.id))
        await session.commit()


async def _rebalance() -> None:
    async with db.session_factory()() as session:
        await pipeline.rebalance(session, OWNER)
        await session.commit()


async def _roles() -> dict[str, Opportunity]:
    async with db.session_factory()() as session:
        rows = await session.scalars(select(Opportunity).execution_options(populate_existing=True))
        return {r.slug: r for r in rows}


async def _gate_lines(slug: str) -> list[str]:
    async with db.session_factory()() as session:
        role = (await session.scalars(select(Opportunity).where(Opportunity.slug == slug))).one()
        rules = await gating.rules(session, OWNER)
        subject = gating.role_subject(role, rules.index.of(role.employer_id, role.company))
        return [line.result for line in gating.results(subject, rules) if line.kind == "location"]


async def test_a_job_system_ordered_town_is_read_as_a_place_that_fails_in_person_and_hybrid(js):
    await _world(_role("onsite", LAUNCH, FENWICK, "onsite"), _role("hybrid", LAUNCH, FENWICK, "hybrid"),
                 _role("mode", LAUNCH, FENWICK, None), _role("home", LAUNCH, "US, WA, Oakfield", "hybrid"))
    assert await _gate_lines("onsite") == ["fail"]
    assert await _gate_lines("hybrid") == ["fail"]
    assert await _gate_lines("mode") == ["unknown"], "no way of working stated: it may be the remote one"
    assert await _gate_lines("home") == ["pass"], "a listed town, in the same order"


async def test_a_read_role_whose_place_gate_is_undecided_is_held_off_the_deck(js):
    await _world(_role("ready", LAUNCH, FENWICK), _role("deck", SECOND, FENWICK),
                 _role("silent", LAUNCH, None, place="not_stated"), _role("remote", LAUNCH, FENWICK, "remote"),
                 _role("fine", LAUNCH, "US, WA, Oakfield", "hybrid"), _role("unasked", LAUNCH, None, place=None))
    await _rebalance()
    got = await _roles()
    assert (got["ready"].status, got["deck"].status) == (FIRST, FIRST)
    assert got["ready"].screened_out_reason is None, "held back, never Gated out"
    assert got["silent"].status in (SECOND, LAUNCH), "the analyst said the posting does not state it: it goes on"
    assert got["unasked"].status in (SECOND, LAUNCH), "no place held and none asked yet: the analyst's ask comes first"
    assert got["remote"].status in (SECOND, LAUNCH) and got["fine"].status in (SECOND, LAUNCH)


async def test_an_unread_role_with_an_undecided_place_still_competes_to_be_read(js):
    await _world(_role("unread", FIRST, FENWICK, analysis_state=None, analysis=None))
    await _rebalance()
    got = await _roles()
    assert got["unread"].status == FIRST and got["unread"].analysis_state is None


async def test_the_hold_ends_when_the_way_of_working_arrives(js):
    await _world(_role("late", LAUNCH, FENWICK))
    async with db.session_factory()() as session:
        assert await dial_hold.settle(session, OWNER) == 1
        await session.commit()
    assert (await _roles())["late"].status == FIRST
    async with db.session_factory()() as session:
        role = (await session.scalars(select(Opportunity))).one()
        role.move_type = "remote"
        await session.commit()
    await _rebalance()
    assert (await _roles())["late"].status in (SECOND, LAUNCH)


async def test_a_role_go_opened_is_not_held(js):
    await _world(_role("opened", LAUNCH, FENWICK, go_opened_at=datetime.now(UTC)))
    async with db.session_factory()() as session:
        assert await dial_hold.settle(session, OWNER) == 0
    await _rebalance()
    assert (await _roles())["opened"].status == LAUNCH


def test_a_count_of_places_takes_the_places_the_postings_name():
    both = [f"{FENWICK}; US, Remote", "2 Locations"]
    assert stated_place.from_postings("2 Locations", both) == f"{FENWICK}; US, Remote"
    assert stated_place.from_postings(None, [FENWICK]) == FENWICK
    assert stated_place.from_postings("3 Locations", both) is None, "fewer places than the count: still unknown"
    assert stated_place.from_postings(FENWICK, both) is None, "a place the role holds is never replaced"
    assert stated_place.from_postings("2 Locations", ["2 Locations", None]) is None


async def test_the_pass_puts_the_postings_places_on_a_role_that_holds_a_count_and_judges_it(js):
    await _world(_role("remote", LAUNCH, "2 Locations"), _role("away", LAUNCH, "2 Locations", "onsite"),
                 postings={"remote": [f"{FENWICK}; US, Remote", "2 Locations"],
                           "away": ["US, WA, Fenwick; US, WA, Port Ash", "2 Locations"]})
    await _rebalance()
    got = await _roles()
    assert got["remote"].location == f"{FENWICK}; US, Remote" and got["remote"].status in (SECOND, LAUNCH)
    assert got["away"].location == "US, WA, Fenwick; US, WA, Port Ash"
    assert got["away"].status == stages.GATED_OUT and got["away"].screened_out_by == "location"


async def test_saving_hard_pass_holds_the_deck_roles_whose_place_is_undecided(js):
    await _world(_role("deck", SECOND, FENWICK))
    async with db.session_factory()() as session:
        await regate.run(session, OWNER, cause="Hard pass was saved")
        await session.commit()
    assert (await _roles())["deck"].status == FIRST


_MUST_YEARS = {"id": "must-years", "kind": "job_fact", "section": "posting", "fact": "experience_years", "op": "<=",
               "value": 5}


async def _years_world(*roles: Opportunity) -> None:
    """The made-up profile with a Must on the years of experience asked (no role states them: it waits), and roles."""
    gates = copy.deepcopy(PROFILE["gates"])
    gates["other"] = [*gates.get("other", []), _MUST_YEARS]
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates, settings={**PROFILE["settings"], "apply_per_day": 5})
        session.add_all(roles)
        await session.commit()


async def _line(slug: str, event: str) -> str | None:
    async with db.session_factory()() as session:
        return await session.scalar(select(TrailEvent.sentence).join(
            Opportunity, TrailEvent.link_id == Opportunity.id).where(Opportunity.slug == slug,
                                                                     TrailEvent.event == event))


async def test_the_queue_says_only_the_dial_for_a_role_that_waits_on_a_dial_and_an_undecided_place(js):
    """The queue stops at the first reason, as it always has: the place is asked only of a role the dial does not
    already hold back. (The drop for a Hard pass save names both: the next test.)"""
    await _years_world(_role("both", SECOND, FENWICK, gate_facts={}),
                       _role("dial", SECOND, "US, WA, Oakfield", "hybrid", gate_facts={}),
                       _role("place", SECOND, FENWICK, gate_facts={"facts": {"experience_years": {
                           "value": "3", "quote": "a made-up line", "by": "code"}}}))
    await _rebalance()
    got = await _roles()
    assert (got["both"].status, got["dial"].status, got["place"].status) == (FIRST, FIRST, FIRST), "none has a place"
    assert (await _line("both", "off_deck")).endswith(": waiting for the years of experience asked."), "the dial only"
    assert (await _line("dial", "off_deck")).endswith(": waiting for the years of experience asked.")
    # A history line is cut at 140 characters (`trail.record`), so the place's long words may be cut short.
    assert ": waiting for where the work is" in await _line("place", "off_deck"), "the place alone"


async def test_the_drop_for_a_hard_pass_save_says_both_reasons(js):
    await _years_world(_role("both", SECOND, FENWICK, gate_facts={}))
    async with db.session_factory()() as session:
        assert await dial_hold.settle(session, OWNER) == 1
        await session.commit()
    assert await _line("both", dial_hold.HELD_EVENT) == (
        "Back in First Impression: waiting for the years of experience asked, where the work is (the work location "
        "gate cannot be decided yet).")
