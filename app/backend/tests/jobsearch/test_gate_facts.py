"""Gates first (the owner, 2026-10-19, the token efficiency plan spec step 1, `gate_facts.py`): the cheap gates are
decided on a posting's own facts before anyone pays to analyse the role or to open it in a browser. A fail moves the
role to Gated out with the reason and the quoted line; an unknown passes; the order puts this before the analyst and the
browser; a short Haiku read answers what code cannot, on a quote of the posting or not at all; and the analysis stops at
the first gate that fails. Every name, number and sentence is made up."""

import itertools
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch import stages
from app.jobsearch.browser import queue as bq
from app.jobsearch.browser import reader as browser_reader
from app.jobsearch.doors import tools
from app.jobsearch.gating import gate_facts
from app.jobsearch.loops import schedule
from app.jobsearch.missions.search_run import RunError
from app.jobsearch.models import Employer, JobPosting, Opportunity
from app.jobsearch.postings import sourced as sourcing
from app.jobsearch.postings import text as posting_text
from app.jobsearch.research import analysis
from app.jobsearch.states import StopStep
from app.models.work import TrailEvent
from app.trail import service as trail
from tests.builders import findings, mission_of, put_in_line, save_documents, submit_findings
from tests.jobsearch.example_world import EXAMPLE_SYSTEM, OTHER_SYSTEM, THIRD_SYSTEM
from tests.support import FIVE, OWNER, POSTING_WORDS, load_profile, reads_on

NOW = datetime.now(UTC)
#: Remote anywhere, or hybrid or in person in Oakfield; at least 100,000 on the posted maximum; a travel sentence.
GATES = {
    "location": {"remote": {"on": True}, "hybrid": {"on": True, "locations": ["Oakfield"]},
                 "in_person": {"on": True, "locations": ["Oakfield"]}},
    "compensation": {"amount": 100_000, "against": "maximum"},
    "other": [{"id": "travel", "kind": "rule", "section": "responsibilities", "text": "Travel of 20% or more."}],
}
PAY_LOW = "Programs Lead\nPay range: $60,000 - $80,000 per year\n\nAbout the job\nLead the programs team."
PAY_OK = "Programs Lead\nPay range: $115,000 - $140,000 per year\n\nAbout the job\nLead the programs team."
ONSITE_ELSEWHERE = "Programs Lead\nLocation: Riverton, OH\nWork type: On-site\n\nAbout the job\nLead the team."
NOTHING = POSTING_WORDS


async def world(*roles: tuple[str, str, dict], gates: dict | None = None, haiku: bool = False) -> dict:
    """The made-up profile with these gates and these roles `(company, text, fields)` in Found, Sourced and holding the
    text: the ids by company."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES if gates is None else gates)
        await save_documents(session)
        await roster.ensure_roster(session, OWNER)
        made = {}
        for company, text, fields in roles:
            role = Opportunity(**{
                "owner": OWNER, "slug": company.lower(), "company": company, "title": "Programs Lead",
                "origin": "scout", "fit": 3, "match_key": f"{company.lower()}|programs lead",
                "status": stages.SECOND_IMPRESSION,
                "found": NOW.date(), "url": f"https://jobs.example.test/{company.lower()}", "description": text,
                "source_url": f"https://jobs.example.test/{company.lower()}", "source_kind": "employer",
                "sourced_at": NOW, "location": None, "move_type": None,
                "matched_by": "haiku",  # matched already: the title job that starts by itself is not this test's
                **fields})
            session.add(role)
            made[company] = role
        await session.commit()
        return {k: v.id for k, v in made.items()}


async def read(role_id, **kw) -> tuple[gate_facts.Outcome, Opportunity]:
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        outcome = await gate_facts.process(session, role, **kw)
        await session.commit()
        await session.refresh(role)
        return outcome, role


async def trail_of(role_id, event: str) -> list[TrailEvent]:
    async with db.read_session_factory()() as session:
        return list((await session.scalars(select(TrailEvent).where(
            TrailEvent.link_id == role_id, TrailEvent.event == event))).all())


@pytest.fixture
def haiku_on(monkeypatch):
    monkeypatch.setattr(gate_facts, "HAIKU_READS", True)


# --- fail, pass and unknown ----------------------------------------------------------------------------------------


async def test_a_posted_pay_that_fails_the_pay_gate_gates_the_role_out_with_the_reason_and_the_quoted_line(js):
    ids = await world(("Fabrikam", PAY_LOW, {"location": "Remote, US", "move_type": "remote"}))
    outcome, role = await read(ids["Fabrikam"])
    assert outcome.state == gate_facts.STOPPED
    assert role.status == stages.GATED_OUT and role.screened_out_gate_id == "compensation"
    assert role.screened_out_step == StopStep.GATE.value and "$100,000" in role.screened_out_reason
    assert role.comp == "$60,000-$80,000" and role.comp_source == "posting"
    assert role.gate_facts["state"] == gate_facts.GATED and role.gate_facts["stop"] == "compensation"
    (line,) = await trail_of(ids["Fabrikam"], "gate_facts_stopped")
    assert line.sentence.startswith("Gated out before analysis:")
    assert 'Pay range: $60,000 - $80,000 per year' in line.detail and "not analysed" in line.detail


async def test_a_pay_that_passes_is_kept_on_the_role_and_stops_nothing(js):
    ids = await world(("Fabrikam", PAY_OK, {"location": "Remote, US", "move_type": "remote"}))
    outcome, role = await read(ids["Fabrikam"])
    assert outcome.state == gate_facts.CLEAR and outcome.changes == ("pay",)
    assert role.status == stages.SECOND_IMPRESSION and role.comp == "$115,000-$140,000"
    assert role.gate_facts["state"] == gate_facts.SETTLED
    assert role.gate_facts["facts"]["pay"]["quote"] == "Pay range: $115,000 - $140,000 per year"
    assert (await trail_of(ids["Fabrikam"], "gate_facts_read"))[0].detail.startswith("pay: $115,000-$140,000")


async def test_a_posting_that_states_nothing_is_unknown_and_passes(js):
    ids = await world(("Fabrikam", NOTHING, {"location": "Riverton, OH"}))
    outcome, role = await read(ids["Fabrikam"])
    assert outcome.state == gate_facts.CLEAR and outcome.changes == ()
    assert role.status == stages.SECOND_IMPRESSION and role.comp is None and role.move_type is None


async def test_a_way_of_working_the_posting_labels_fails_the_work_location_gate(js):
    ids = await world(("Fabrikam", ONSITE_ELSEWHERE, {"location": "United States"}))
    outcome, role = await read(ids["Fabrikam"])
    assert outcome.state == gate_facts.STOPPED
    assert role.status == stages.GATED_OUT and role.screened_out_gate_id == "location"
    assert role.move_type == "onsite" and role.location == "Riverton, OH"
    (line,) = await trail_of(ids["Fabrikam"], "gate_facts_stopped")
    assert "Work type: On-site" in line.detail and "Location: Riverton, OH" in line.detail


async def test_a_place_that_fails_every_way_of_working_is_a_fail_against_the_owners_place_rules(js):
    """The owner, 2026-10-23: a place abroad with no way of working fails remote (their country only), hybrid and in
    person (their towns only) alike, so no missing fact can rescue it: it is a fail, not an unknown."""
    us_only = {**GATES, "location": {**GATES["location"], "remote": {"on": True, "locations": ["United States"]}}}
    ids = await world(("Initech", NOTHING, {"location": "Tokyo, Japan"}), gates=us_only)
    outcome, role = await read(ids["Initech"])
    assert outcome.state == gate_facts.STOPPED and role.screened_out_gate_id == "location"
    assert "fails whether remote, hybrid or in person" in role.screened_out_reason
    # A place that says nothing, or one a way of working could rescue, is still unknown.
    ids = await world(("Hooli", NOTHING, {"location": "26 Locations"}), ("Globex", NOTHING, {"location": "Dallas, TX"}),
                      gates=us_only)
    for company in ("Hooli", "Globex"):
        assert (await read(ids[company]))[0].state == gate_facts.CLEAR
    # With remote open anywhere, the same place could be worked remote: unknown, it passes.
    ids = await world(("Umbrella", NOTHING, {"location": "Tokyo, Japan"}))
    assert (await read(ids["Umbrella"]))[0].state == gate_facts.CLEAR


async def test_a_role_the_owner_moved_by_hand_or_that_is_researched_already_is_never_read(js):
    ids = await world(("Fabrikam", PAY_LOW, {"location": "Remote, US", "move_type": "remote"}),
                      ("Initech", PAY_LOW, {"analysis_state": analysis.DONE}))
    async with db.session_factory()() as session:
        await trail.record(session, OWNER, event="role_moved", actor="person", who="Pat",
                           sentence="You moved it.", link=("opportunity", ids["Fabrikam"]))
        await session.commit()
    for company in ("Fabrikam", "Initech"):
        outcome, role = await read(ids[company])
        assert outcome.state == gate_facts.LEFT and role.status == stages.SECOND_IMPRESSION and not role.gate_facts


async def test_a_role_is_read_again_only_when_its_text_place_way_of_working_or_pay_changed(js):
    ids = await world(("Fabrikam", PAY_OK, {"location": "Remote, US", "move_type": "remote"}))
    first = (await read(ids["Fabrikam"]))[1].gate_facts
    assert (await read(ids["Fabrikam"]))[1].gate_facts == first  # nothing changed: nothing read, nothing written
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        role.description = PAY_LOW
        role.comp = None
        await session.commit()
    outcome, role = await read(ids["Fabrikam"])
    assert outcome.state == gate_facts.STOPPED and role.screened_out_gate_id == "compensation"


async def test_the_switch_off_leaves_every_role_where_it_was(js, monkeypatch):
    monkeypatch.setattr(gate_facts, "ENABLED", False)
    ids = await world(("Fabrikam", PAY_LOW, {"location": "Remote, US", "move_type": "remote"}))
    outcome, role = await read(ids["Fabrikam"])
    assert outcome.state == gate_facts.LEFT and role.status == stages.SECOND_IMPRESSION
    async with db.session_factory()() as session:
        assert await gate_facts.sweep(session, OWNER) == {"looked": 0, "stopped": 0, "waits": 0}


# --- the order: before the analyst, before the browser, again after sourcing ----------------------------------------


async def test_a_role_a_cheap_gate_stops_never_reaches_the_analyst_and_one_that_passes_does(js):
    ids = await world(("Fabrikam", PAY_LOW, {"location": "Remote, US", "move_type": "remote"}),
                      ("Initech", PAY_OK, {"location": "Remote, US", "move_type": "remote"}))
    await put_in_line(ids["Fabrikam"])
    await put_in_line(ids["Initech"])
    async with db.session_factory()() as session:
        gated = await session.get(Opportunity, ids["Fabrikam"])
        assert gated.status == stages.GATED_OUT and gated.analysis_state is None  # the process ended at the gate
        assert gated.screened_out_gate_id == "compensation"
        passed = await session.get(Opportunity, ids["Initech"])
        assert passed.status == stages.SECOND_IMPRESSION and passed.analysis_state == analysis.WAITING
        taken = await analysis.claim_next(session, OWNER)
        assert taken is not None and taken.id == ids["Initech"]
        assert await analysis.claim_next(session, OWNER) is None  # the gated role is not in the line


async def test_a_role_in_fact_checking_is_judged_on_its_listing_before_a_browser_item_is_queued_for_it(js):
    ids = await world(("Fabrikam", None, {"location": "Riverton, OH", "move_type": "onsite", "sourced_at": None,
                                          "source_url": None, "source_kind": None}),
                      ("Initech", None, {"location": "Remote, US", "move_type": "remote", "sourced_at": None,
                                         "source_url": None, "source_kind": None}))
    assert await schedule.read_gate_facts(OWNER) == 2  # the pipeline's turn reads them, in its own transactions
    async with db.session_factory()() as session:
        waiting = {key[1] for key in await bq._wanted(session, OWNER, {})}
        assert str(ids["Initech"]) in waiting and str(ids["Fabrikam"]) not in waiting
        gated = await session.get(Opportunity, ids["Fabrikam"])
        assert gated.status == stages.GATED_OUT and gated.screened_out_gate_id == "location"
        assert str(ids["Fabrikam"]) not in {r["role_id"] for r in await browser_reader.board_roles(session, OWNER, {})}


async def test_the_browser_hand_out_reads_no_gate_facts_under_the_mission_lock_so_the_pool_keeps_serving(
        js, monkeypatch):
    """The browser queue's refresh read the gate facts
    of every unread role inside the hand-out's transaction, which holds the browser mission's row FOR UPDATE, so each
    other hand-out waited on that lock holding a connection of its own until the pool had none left. With a pool of
    two, two hand-outs and a slow read, an ordinary query must still get a connection."""
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.jobsearch.browser import pace as browser_pace
    from app.jobsearch.missions import core as missions

    await world(*((f"Company{n}", None, {"location": "Remote, US", "move_type": "remote", "sourced_at": None,
                                          "source_url": None, "source_kind": None}) for n in range(3)))
    async with db.session_factory()() as session:
        await missions.resume(session, await mission_of(session, OWNER, missions.BROWSER), by="Tester")
        await session.commit()

    async def slow_process(session, role, **kw):  # a read that takes a while, as hundreds of them did live
        await asyncio.sleep(1.0)
        return gate_facts.Outcome(gate_facts.CLEAR, None, [])

    monkeypatch.setattr(gate_facts, "process", slow_process)
    small = create_async_engine(js.url, pool_size=2, max_overflow=0, pool_timeout=1.5)
    monkeypatch.setattr(db, "_engine", small)
    monkeypatch.setattr(db, "_sessions", async_sessionmaker(small, expire_on_commit=False))

    async def hand_out() -> None:  # what next_item and code_take do first
        async with db.session_factory()() as session:
            await browser_pace.lock_mission(session, OWNER)
            await bq.refresh(session, OWNER)
            await session.commit()

    async def ordinary() -> int:
        await asyncio.sleep(0.2)
        async with db.session_factory()() as session:
            return await session.scalar(text("select 1"))

    try:
        done = await asyncio.gather(hand_out(), hand_out(), ordinary(), return_exceptions=True)
    finally:
        await small.dispose()
    assert not [d for d in done if isinstance(d, Exception)], done
    assert done[2] == 1


async def test_the_nightly_sourcing_reads_the_gate_facts_before_it_looks_for_a_posting_and_again_after(js, monkeypatch):
    ids = await world(("Fabrikam", None, {"location": "Riverton, OH", "move_type": "onsite", "sourced_at": None,
                                          "source_url": None, "source_kind": None}),
                      ("Initech", None, {"location": "Remote, US", "move_type": "remote", "sourced_at": None,
                                         "source_url": None, "source_kind": None}))
    tried: list = []


    def stored_source(role, rows, employer, hosts):  # what a releasing sweep decides with
        tried.append(role.company)
        return None

    monkeypatch.setattr(sourcing, "stored_source", stored_source)
    async with db.session_factory()() as session:
        assert await sourcing.sweep(session, OWNER) == {"looked": 1, "sourced": 0}
        await session.commit()
        assert tried == ["Initech"]  # the gated role was never looked for
        assert (await session.get(Opportunity, ids["Fabrikam"])).status == stages.GATED_OUT


async def test_a_text_saved_after_sourcing_is_read_at_once_so_the_role_never_waits_for_the_analyst(js):
    ids = await world(("Fabrikam", None, {"location": "Remote, US", "move_type": "remote"}))
    async with db.session_factory()() as session:
        from app.jobsearch.postings import text as posting_text

        role = await session.get(Opportunity, ids["Fabrikam"])
        role.source_url, role.source_kind = "https://careers.example.test/jobs/1", "employer"  # the role's source
        assert await posting_text.take(session, role, posting_text.Capture(PAY_LOW + "\n\n" + NOTHING),
                                       now=NOW) == posting_text.FETCHED
        await session.commit()
        assert role.status == stages.GATED_OUT and role.screened_out_gate_id == "compensation"


# --- the sweep: what it reads, what it locks, and how long it holds ---------------------------------------------

SNIPPET = "Programs Lead\n6 Locations"
REMOTE = {"location": "Remote, US", "move_type": "remote"}


def posting_of(role_id, text: str | None, *, system: str = EXAMPLE_SYSTEM, ext: str = "1") -> JobPosting:
    """A posting of the role, as a job system's own: `text` is what it holds."""
    return JobPosting(owner=OWNER, employer_id=uuid.uuid4(), ats=system, ext_id=ext, company="Fabrikam",
                      title=f"Programs Lead {ext}", match_key=f"fabrikam|programs lead {ext}", first_seen=NOW,
                      last_seen=NOW, links=[], opportunity_id=role_id, text=text, detail_at=NOW)


async def add_postings(*postings: JobPosting) -> None:
    async with db.session_factory()() as session:
        session.add_all(postings)
        await session.commit()


async def test_a_second_turn_reads_and_locks_nothing_for_roles_whose_text_is_empty_or_held_by_a_posting(
        js, transactions):
    """The sweep decided "nothing changed" from the role's own description, while
    `process` stores the fingerprint of the text the role holds (its own, else a posting's whole text). A role with no
    description and a role whose description is a snippet while a posting holds the whole text never matched, so each
    was locked, refreshed and read again in every batch of every turn (many transactions an hour, most of them holding a
    row lock for nothing)."""
    ids = await world(("Fabrikam", None, REMOTE), ("Initech", SNIPPET, REMOTE), ("Hooli", NOTHING, REMOTE))
    await add_postings(posting_of(ids["Initech"], POSTING_WORDS))
    assert await schedule.read_gate_facts(OWNER) == 3, "each is read once, as it always was"
    transactions.clear()
    assert await schedule.read_gate_facts(OWNER) == 0, "nothing changed since: nothing is read"
    assert not [s for s in transactions.timeline if "FOR UPDATE" in s], "and no role is locked to find that out"
    async with db.session_factory()() as session:  # the text of a posting is the text it was read on
        role = await session.get(Opportunity, ids["Initech"])
        assert role.gate_facts["fp"] == gate_facts.fingerprint(role, POSTING_WORDS)


@pytest.mark.parametrize("own, postings", [
    (None, []), ("   ", []), (SNIPPET, []), (POSTING_WORDS, []),
    (None, [(EXAMPLE_SYSTEM, POSTING_WORDS)]), (SNIPPET, [(EXAMPLE_SYSTEM, POSTING_WORDS)]),
    (POSTING_WORDS, [(EXAMPLE_SYSTEM, POSTING_WORDS + " and more words of the posting" * 3)]),
    (SNIPPET, [("scout", POSTING_WORDS)]), (None, [(EXAMPLE_SYSTEM, SNIPPET)]),
    (SNIPPET, [(EXAMPLE_SYSTEM, "<!DOCTYPE html>\nSkip to content\n" + POSTING_WORDS)]),
    (None, [(EXAMPLE_SYSTEM, SNIPPET), (OTHER_SYSTEM, POSTING_WORDS), (THIRD_SYSTEM, POSTING_WORDS[:400])]),
], ids=["no text", "only spaces", "a snippet", "a whole text", "a posting's text alone", "a posting's over a snippet",
        "own whole text over a fuller posting", "a scout's posting is no text", "a posting's snippet is no text",
        "a page dump is no text", "the fullest of three postings"])
async def test_the_sweep_judges_a_role_on_the_text_the_process_judges_it_on(js, own, postings):
    """For every shape of a role's text, what the sweep compares (`stale`) is what `process` stored, so a role that was
    read is never read again until something about it changes, and `held_from` says what `held` says."""
    ids = await world(("Fabrikam", own, REMOTE))
    await add_postings(*(posting_of(ids["Fabrikam"], text, system=system, ext=str(n))
                         for n, (system, text) in enumerate(postings)))
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        rows = list((await session.scalars(select(JobPosting).where(JobPosting.opportunity_id == role.id))).all())
        assert posting_text.held_from(role, rows) == await posting_text.held(session, role)
        assert await gate_facts.stale(session, OWNER, limit=10) == [role.id], "never read: it is stale"
    outcome, _ = await read(ids["Fabrikam"])
    assert outcome.state != gate_facts.LEFT
    async with db.session_factory()() as session:
        assert await gate_facts.stale(session, OWNER, limit=10) == [], "read in its present state: it is not"


async def test_a_sweep_in_batches_that_fails_keeps_what_it_saved_and_the_next_sweep_reads_the_rest(js, monkeypatch):
    """Each batch is saved before the next begins: a failure in the second leaves the first read and the others stale,
    nothing half read, and the sweep that follows reads exactly those."""
    ids = await world(("Fabrikam", PAY_OK, REMOTE), ("Initech", PAY_OK, REMOTE), ("Hooli", PAY_OK, REMOTE))
    monkeypatch.setattr(gate_facts, "BATCH", 1)
    real, tried = gate_facts.read_batch, []

    async def failing(session, owner, batch, rules=None):
        tried.append(batch)
        if len(tried) == 2:
            raise RuntimeError("the second batch failed")
        return await real(session, owner, batch, rules)

    monkeypatch.setattr(gate_facts, "read_batch", failing)
    assert (await gate_facts.sweep_in_batches(OWNER))["looked"] == 1, "the first batch was read, the rest were not"
    assert len(tried) == 2, "the sweep ended at the failure"
    async with db.session_factory()() as session:
        assert set(await gate_facts.stale(session, OWNER, limit=10)) == set(ids.values()) - set(tried[0])
    monkeypatch.setattr(gate_facts, "read_batch", real)
    assert (await gate_facts.sweep_in_batches(OWNER))["looked"] == 2
    async with db.session_factory()() as session:
        assert await gate_facts.stale(session, OWNER, limit=10) == []


async def test_a_role_that_changed_since_it_was_read_is_stale_again_in_each_of_the_ways_it_can(js):
    ids = await world(*((company, text, {**REMOTE, "created_at": NOW - timedelta(days=age)})
                        for company, text, age in (("Fabrikam", POSTING_WORDS, 4), ("Initech", POSTING_WORDS, 3),
                                                   ("Hooli", POSTING_WORDS, 2), ("Globex", None, 1))))
    assert await schedule.read_gate_facts(OWNER) == 4
    async with db.session_factory()() as session:
        assert await gate_facts.stale(session, OWNER, limit=10) == []
        (await session.get(Opportunity, ids["Fabrikam"])).description = POSTING_WORDS + " Edited."  # its text
        (await session.get(Opportunity, ids["Initech"])).location = "Dallas, TX"  # its place
        (await session.get(Opportunity, ids["Hooli"])).comp = "$90,000"  # its pay
        await session.commit()
    async with db.session_factory()() as session:
        assert set(await gate_facts.stale(session, OWNER, limit=10)) == {ids["Fabrikam"], ids["Initech"], ids["Hooli"]}
        assert await gate_facts.stale(session, OWNER, limit=2) == [ids["Fabrikam"], ids["Initech"]], "oldest first"


async def test_a_role_a_session_holds_is_not_read_and_the_others_are_and_a_second_turn_reads_none(js):
    """Old: also a role another transaction had locked (`FOR UPDATE`) was left for the next turn. New: SQLite has no row
    lock (one writer: another transaction cannot hold a row while this one reads), so only the role a session holds
    (researching it) is left, and the second turn has nothing stale."""
    ids = await world(("Fabrikam", PAY_OK, REMOTE), ("Initech", PAY_OK, REMOTE), ("Hooli", PAY_OK, REMOTE))
    async with db.session_factory()() as session:  # a session is researching Fabrikam
        mine = await session.get(Opportunity, ids["Fabrikam"])
        mine.analysis_state, mine.analysis_claimed_at = analysis.WAITING, NOW
        await session.commit()
    assert await schedule.read_gate_facts(OWNER) == 2, "Initech and Hooli: Fabrikam is the session's"
    async with db.session_factory()() as session:
        assert not (await session.get(Opportunity, ids["Fabrikam"])).gate_facts
        assert (await session.get(Opportunity, ids["Initech"])).gate_facts["state"] == gate_facts.SETTLED
        assert (await session.get(Opportunity, ids["Hooli"])).gate_facts["state"] == gate_facts.SETTLED
    assert await schedule.read_gate_facts(OWNER) == 0, "nothing else is stale; Fabrikam is the session's still"


async def test_the_roles_a_batch_locks_are_locked_in_id_order_and_read_oldest_first(js, monkeypatch):
    """Every pass takes role rows in id order (`as_read.lock`), so none is half of a cycle of waits with another: the
    batch locks by id, though it reads the oldest role first."""
    ids = await world(*(
        (company, PAY_OK, {**REMOTE, "id": uuid.UUID(int=n), "created_at": NOW - timedelta(days=n)})
        for company, n in (("Fabrikam", 1), ("Initech", 2), ("Hooli", 3))))  # the oldest has the highest id
    read: list[str] = []
    real = gate_facts.process

    async def noting(session, role, **kw):
        read.append(role.company)
        return await real(session, role, **kw)

    monkeypatch.setattr(gate_facts, "process", noting)
    with reads_on("js_opportunities") as reads:
        async with db.session_factory()() as session:
            await gate_facts.read_batch(session, OWNER, list(ids.values()))
            await session.commit()
    (statement, locked), *_ = [(s, i) for s, i in reads if re.search(r"ORDER BY js_opportunities\.id\b", s)]
    assert "FOR UPDATE" not in statement and "SKIP LOCKED" not in statement, statement
    assert locked == sorted(ids.values()), "the first read of the roles by id binds them in id order"
    assert read == ["Hooli", "Initech", "Fabrikam"], "the oldest role is read first, as before"


async def test_a_role_the_owner_moved_by_hand_is_not_read_and_does_not_keep_the_others_from_being_read(js):
    ids = await world(("Fabrikam", PAY_LOW, REMOTE), ("Initech", PAY_OK, REMOTE))
    async with db.session_factory()() as session:
        await trail.record(session, OWNER, event="role_moved", actor="person", who="Pat",
                           sentence="You moved it.", link=("opportunity", ids["Fabrikam"]))
        await session.commit()
    assert await schedule.read_gate_facts(OWNER) == 1
    async with db.session_factory()() as session:
        assert (await session.get(Opportunity, ids["Fabrikam"])).status == stages.SECOND_IMPRESSION
        assert not (await session.get(Opportunity, ids["Fabrikam"])).gate_facts
        assert await gate_facts.stale(session, OWNER, limit=10) == [], "the moved role is the owner's, never stale"


async def test_a_turn_with_nothing_stale_opens_one_read_and_a_turn_with_work_a_short_write_a_batch(
        js, transactions, monkeypatch):
    """The Found roles are read once a turn, in a transaction that takes no lock; each batch of roles to read is then
    three short transactions, committed before the next: the texts the roles are judged on (`_judged_texts` ends its
    transaction before its CPU loop), what is read ahead of the lock (`_prepare` ends that transaction), and the lock
    with its writes. No writer turn is held while the facts are worked out.

    Edited with the release in `_judged_texts`: old, one read and then a read and a write for each batch (7
    transactions for 3 batches); new, two reads, then two reads and a write for each batch (11).
    The write is still the last of each batch and the only one that changes anything.

    Edited with the rules built before the writer turn: old, two reads and a write for
    each batch (9 for 3 batches); new, the rules in a reader session of their own first, so three reads and a write
    for each batch (12)."""
    monkeypatch.setattr(schedule, "GATE_FACTS_BATCH", 2)
    ids = await world(*((f"Company{n}", None if n % 2 else PAY_OK, REMOTE) for n in range(5)))
    await add_postings(posting_of(ids["Company1"], POSTING_WORDS))
    transactions.clear()
    assert await schedule.read_gate_facts(OWNER) == 5
    stale, placing, *batches = transactions.ended
    assert not transactions.writes(stale) and not transactions.writes(placing), (
        "the read of what is stale takes no lock and changes nothing: the roles and postings, then (the texts judged "
        "without the turn) where the owner's moves are asked")
    assert len(batches) == 12, "5 roles in batches of 2: three batches of three reads and a write each"
    assert not any(transactions.writes(b) for i, b in enumerate(batches) if i % 4 != 3), (
        "the rules and what is read ahead change nothing")
    assert all(transactions.writes(b) for b in batches[3::4]), "the lock and the writes are a transaction of their own"
    transactions.clear()
    assert await schedule.read_gate_facts(OWNER) == 0
    assert len(transactions.ended) == 1 and not transactions.writes(transactions.ended[0]), (
        "nothing stale: one read, and no transaction that holds a row")


# --- the short Haiku read ------------------------------------------------------------------------------------------


UNCLEAR = ("Programs Lead\nLocation: Riverton, OH\nAbout the job\nLead the programs team.\n"
           "This is an on-site role for the whole team.\nThe job needs travel of up to 40% of the year.")


async def hand(session, owner=OWNER, sid="titles-1") -> list[dict]:
    roles = await gate_facts.waiting(session, owner, skip_leased=True)
    items = [await gate_facts.item(session, r) for r in roles]
    gate_facts.hand_out(roles, sid)
    await session.flush()
    return items


async def test_with_the_switch_off_no_role_waits_for_a_haiku_read(js, monkeypatch):
    monkeypatch.setattr(gate_facts, "HAIKU_READS", False)
    ids = await world(("Fabrikam", UNCLEAR.replace("This is an on-site role for the whole team.", ""), {}))
    outcome, role = await read(ids["Fabrikam"])
    assert outcome.state == gate_facts.CLEAR and role.gate_facts["state"] == gate_facts.SETTLED
    async with db.session_factory()() as session:
        assert await gate_facts.waiting(session, OWNER) == []


async def test_a_fact_on_a_quote_of_the_posting_is_kept_and_the_gates_decide_on_it(js, haiku_on):
    ids = await world(("Fabrikam", UNCLEAR, {"location": "Riverton, OH"}))
    outcome, role = await read(ids["Fabrikam"])  # code read the sentence "This is an on-site role" itself
    assert outcome.state == gate_facts.STOPPED  # so nothing is left for a model to read
    ids = await world(("Initech", UNCLEAR.replace("This is an on-site role for the whole team.",
                                                  "Team members are expected on site every day."),
                       {"location": "Riverton, OH"}))
    await read(ids["Initech"])
    async with db.session_factory()() as session:
        await hand(session)
        taken = await gate_facts.take_answers(session, OWNER, [{
            "id": str(ids["Initech"])[:8], "kind": "facts",
            "facts": {"work_mode": {"value": "on-site", "quote": "Team members are expected on site every day."}}}])
        await session.commit()
    assert taken["taken"] == 1 and taken["stopped"] == 1 and taken["problems"] == []
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Initech"])
        assert role.status == stages.GATED_OUT and role.move_type == "onsite"
        assert role.gate_facts["facts"]["work_mode"] == {
            "value": "onsite", "quote": "Team members are expected on site every day.", "by": "haiku"}
        assert "Read by a short Haiku read" in (await trail_of(ids["Initech"], "gate_facts_stopped"))[0].detail


async def test_a_place_read_with_no_stated_way_of_working_never_fails_a_role(js, haiku_on):
    quote = "This role is based out of our Riverton office."
    ids = await world(("Fabrikam", UNCLEAR.replace("This is an on-site role for the whole team.", quote),
                       {"location": "Not stated"}))
    await read(ids["Fabrikam"])
    async with db.session_factory()() as session:
        await hand(session)
        taken = await gate_facts.take_answers(session, OWNER, [{
            "id": str(ids["Fabrikam"])[:8], "kind": "facts",
            "facts": {"place": {"value": "Riverton, OH", "quote": quote}}}])
        await session.commit()
    assert taken["taken"] == 1 and taken["stopped"] == 0
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        assert role.status == stages.SECOND_IMPRESSION and role.move_type is None  # an unknown passes
        assert role.location == "Not stated"


async def test_a_fact_whose_quote_is_not_in_the_posting_is_dropped_and_stops_nothing(js, haiku_on):
    ids = await world(("Fabrikam", UNCLEAR.replace("This is an on-site role for the whole team.", ""),
                       {"location": "Riverton, OH"}))
    await read(ids["Fabrikam"])
    async with db.session_factory()() as session:
        await hand(session)
        taken = await gate_facts.take_answers(session, OWNER, [{
            "id": str(ids["Fabrikam"])[:8], "kind": "facts",
            "facts": {"work_mode": {"value": "onsite", "quote": "Everyone works in the office five days."}}}])
        await session.commit()
    assert taken["taken"] == 1 and taken["stopped"] == 0
    assert "the quote is not in the posting's lines" in taken["problems"][0]
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        assert role.status == stages.SECOND_IMPRESSION and role.move_type is None  # an unknown passes
        assert role.gate_facts["state"] == gate_facts.SETTLED


async def test_the_title_session_tools_hand_out_and_take_the_reads(js, haiku_on):
    ids = await world(("Fabrikam", UNCLEAR.replace("This is an on-site role for the whole team.", ""),
                       {"location": "Riverton, OH"}))
    await read(ids["Fabrikam"])
    async with db.session_factory()() as session:
        reply = await tools.titles_next_reply(session, OWNER, "titles-9")
        await session.commit()
    assert [i["kind"] for i in reply["postings"]] == ["facts"] and "what_to_look_for" not in reply
    assert "header and condition lines" in reply["how"]
    async with db.session_factory()() as session:
        again = await tools.titles_next_reply(session, OWNER, "titles-9")
    assert again["batch_done"] is True  # one batch a session
    async with db.session_factory()() as session:
        done = await tools.titles_report_reply(session, OWNER, [{
            "id": reply["postings"][0]["id"], "kind": "facts", "facts": {"work_mode": {"value": "hybrid",
                                                                                       "quote": "x y z"}}}])
        await session.commit()
    assert done["taken"] == 1 and done["gated"] == 0 and done["titling_done"] is True
    async with db.session_factory()() as session:
        assert await gate_facts.waiting(session, OWNER) == []


# --- the analysis stops at the first gate that fails ---------------------------------------------------------------


async def test_a_gate_that_fails_on_an_employer_fact_stops_the_role_before_an_analyst_takes_it(js):
    ids = await world(("Fabrikam", PAY_OK, {"location": "Remote, US", "move_type": "remote"}))
    await put_in_line(ids["Fabrikam"])
    async with db.session_factory()() as session:
        session.add(Employer(owner=OWNER, key="fabrikam", name="Fabrikam", gated=True, gated_reason="never again"))
        await session.flush()
        role = await session.get(Opportunity, ids["Fabrikam"])
        role.employer_id = (await session.scalar(select(Employer.id).where(Employer.key == "fabrikam")))
        await session.commit()
        assert await analysis.claim_next(session, OWNER) is None
        await session.commit()
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        assert role.status == stages.GATED_OUT and role.analysis_state is None
        assert role.screened_out_gate_id == "employer"
        assert (await trail_of(ids["Fabrikam"], "gate_facts_stopped"))[0].detail.startswith("Checked again before an")


async def test_the_master_resume_is_not_sent_once_a_gate_fails_and_the_analyst_is_told_to_stop(js):
    ids = await world(("Fabrikam", PAY_OK, {"location": "Remote, US", "move_type": "remote"}))
    await put_in_line(ids["Fabrikam"])
    async with db.session_factory()() as session:
        task = await analysis.analysis_reply(session, OWNER, "task", session_id="s1")
        assert task["role"]["id"] == str(ids["Fabrikam"])
        session.add(Employer(owner=OWNER, key="fabrikam", name="Fabrikam", gated=True))
        await session.flush()
        role = await session.get(Opportunity, ids["Fabrikam"])
        role.employer_id = await session.scalar(select(Employer.id).where(Employer.key == "fabrikam"))
        await session.commit()
        reply = await analysis.analysis_reply(session, OWNER, "resume", session_id="s1")
        await session.commit()
    assert reply["stopped"] is True and "stop here" in reply["note"]
    assert "resume" not in reply
    async with db.session_factory()() as session:
        assert (await session.get(Opportunity, ids["Fabrikam"])).status == stages.GATED_OUT
    refused = await submit_findings_refused(ids["Fabrikam"])  # a writer of its own: this session's turn is let go
    assert "isn't waiting for research" in refused


async def submit_findings_refused(role_id) -> str:
    try:
        await submit_findings(findings(role_id))
    except analysis.AnalysisError as exc:
        return str(exc)
    return ""


# --- the pay shortcut (the owner, 2026-10-19): the posted range alone settles the ask-based pay gate when it can --

ASK_GATES = {**GATES, "compensation": {"amount": 105_000, "against": "ask"}}
REMOTE = {"location": "Remote, US", "move_type": "remote"}


def posting_paying(line: str) -> str:
    return f"Programs Lead\n{line}\n\nAbout the job\nLead the programs team."


_COMPANIES = itertools.count()


async def pay_read(line: str, gates: dict | None = None, **fields):
    name = f"Co{next(_COMPANIES)}"
    ids = await world((name, posting_paying(line), {**REMOTE, **fields}), gates=gates or ASK_GATES)
    return await read(ids[name])


async def read_no_pay():
    ids = await world(("Fabrikam", NOTHING, REMOTE), gates=ASK_GATES)
    return await read(ids["Fabrikam"])


async def test_a_posted_midpoint_a_thousand_or_more_under_the_floor_fails_the_pay_gate_with_the_evidence(js):
    outcome, role = await pay_read("Pay range: $90,000 - $108,000 per year")  # midpoint 99,000
    assert outcome.state == gate_facts.STOPPED and role.status == stages.GATED_OUT
    assert role.screened_out_gate_id == "compensation"
    assert "$90,000-$108,000" in role.screened_out_reason and "$99,000" in role.screened_out_reason
    assert "$105,000" in role.screened_out_reason


async def test_a_single_figure_just_under_the_floor_is_not_failed_the_round_up_edge(js):
    outcome, role = await pay_read("Salary: $104,500 per year")
    assert outcome.state == gate_facts.CLEAR and role.status == stages.SECOND_IMPRESSION
    outcome, role = await pay_read("Salary: $104,000 per year")
    assert outcome.state == gate_facts.STOPPED


async def test_a_bottom_at_or_above_the_floor_passes_the_gate_and_says_so_with_evidence(js):
    outcome, role = await pay_read("Pay range: $105,000 - $140,000 per year")
    assert outcome.state == gate_facts.CLEAR and role.status == stages.SECOND_IMPRESSION
    assert role.gate_facts["pay_gate"]["result"] == "pass"
    assert "$105,000-$140,000" in role.gate_facts["pay_gate"]["evidence"]


async def test_the_gray_zone_and_a_posting_with_no_range_are_left_for_the_analysis(js):
    outcome, role = await pay_read("Pay range: $85,000 - $130,000 per year")  # midpoint 107,500, bottom under
    assert outcome.state == gate_facts.CLEAR and role.gate_facts["pay_gate"] is None
    outcome, role = await pay_read("Pay range: $95,000 - $114,000 per year")  # midpoint 104,500: not certain
    assert outcome.state == gate_facts.CLEAR and role.status == stages.SECOND_IMPRESSION
    outcome, role = await read_no_pay()
    assert outcome.state == gate_facts.CLEAR and role.gate_facts["pay_gate"] is None


async def test_an_hourly_range_is_annualised_before_the_bound(js):
    outcome, _ = await pay_read("Pay: $40 - $50 per hour")  # 83,200 - 104,000, midpoint 93,600
    assert outcome.state == gate_facts.STOPPED
    outcome, _ = await pay_read("Pay: $52 - $60 per hour")  # bottom 108,160
    assert outcome.state == gate_facts.CLEAR


async def test_the_bound_follows_the_floor_setting_not_a_constant(js):
    gates = {**GATES, "compensation": {"amount": 85_000, "against": "ask"}}
    outcome, _ = await pay_read("Pay range: $90,000 - $108,000 per year", gates)
    assert outcome.state == gate_facts.CLEAR
    gates = {**GATES, "compensation": {"amount": 125_000, "against": "ask"}}
    outcome, _ = await pay_read("Pay range: $90,000 - $108,000 per year", gates)
    assert outcome.state == gate_facts.STOPPED


async def test_the_analysts_own_band_is_ignored(js):
    ids = await world(("Fabrikam", NOTHING, {**REMOTE, "comp": "$60,000-$80,000", "comp_source": "research"}),
                      gates=ASK_GATES)
    outcome, role = await read(ids["Fabrikam"])
    assert outcome.state == gate_facts.CLEAR and role.status == stages.SECOND_IMPRESSION


async def test_a_changed_floor_decides_the_pay_gate_again_without_reading_the_posting_again(js):
    ids = await world(("Fabrikam", posting_paying("Pay range: $90,000 - $108,000 per year"), REMOTE),
                      gates={**GATES, "compensation": {"amount": 85_000, "against": "ask"}})
    outcome, role = await read(ids["Fabrikam"])
    assert outcome.state == gate_facts.CLEAR
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=ASK_GATES)
        await session.commit()
    outcome, role = await read(ids["Fabrikam"])
    assert outcome.state == gate_facts.STOPPED and role.status == stages.GATED_OUT


def test_a_role_the_v4_method_passed_is_never_failed_by_the_bound():
    """Replay: over many posted bands, single figures and coverages, whenever the analysis (comp.py, method v4) asks at
    or above the floor, the posted-range bound does not fail the role."""
    from app.jobsearch.fit import comp
    from app.jobsearch.measures.pay_place import Gates, posted_pay_verdict
    from app.jobsearch.profile import schema as p
    floor = 105_000
    profile = p.Profile.model_validate({"settings": {}, "gates": {"compensation": {"amount": floor}}})
    gates = Gates(comp_floor=floor, comp_against="ask")
    checked = failed_and_below = 0
    for low in range(90_000, 120_001, 500):
        for width in (0, 500, 1_000, 1_999, 2_000, 5_000, 20_000, 60_000):
            for proven, unproven in ((0, 4), (2, 2), (4, 0)):
                req = [{"item": f"p{i}", "class": "P"} for i in range(proven)] + [
                    {"item": f"u{i}", "class": "U"} for i in range(unproven)]
                band = [low, low + width] if width else [low]
                out = comp.compute(comp.CompInputs(band=band, required=req), profile)
                verdict, _ = posted_pay_verdict(low, low + width, gates)
                checked += 1
                if verdict == "fail":
                    assert out["ask"] < floor, (band, out["ask"])
                    failed_and_below += 1
    assert checked > 1000 and failed_and_below > 100


async def test_a_facts_hand_out_reads_the_pending_roles_light_and_the_items_still_carry_the_lines(js, haiku_on):
    """Every `jobs.titles_next` loaded every pending role whole (its research, its score breakdown, its whole posting
    text), twice, with the scout's connection row locked, for the length of every claim. The pending roles are read
    light and a role's text is read only for the ones shown."""
    import re

    from sqlalchemy import event

    ids = await world(*((f"Pending{n}", UNCLEAR.replace("This is an on-site role for the whole team.", ""),
                         {"location": "Riverton, OH"}) for n in range(3)))
    for company in ids:
        await read(ids[company])
    seen: list[str] = []

    def noted(_conn, _cursor, statement, _parameters, _context, _many):
        seen.append(statement)

    engine = db.engine().sync_engine
    async with db.session_factory()() as session:
        event.listen(engine, "before_cursor_execute", noted)
        try:
            reply = await tools.titles_next_reply(session, OWNER, "titles-9")
        finally:
            event.remove(engine, "before_cursor_execute", noted)
        await session.commit()
    assert len(reply["postings"]) == 3 and all(p["kind"] == "facts" and p["lines"] for p in reply["postings"])
    scans = [s for s in seen
             if "FROM js_opportunities" in s and "json_extract(js_opportunities.gate_facts" in s and "status IN" in s]
    assert scans, "the pending roles are looked for"
    heavy = re.compile(r"js_opportunities\.(analysis|description|fit_breakdown|gate_answers|analysis_made_under)\b")
    assert not [s[:120] for s in scans if heavy.search(s)], "no scan of the pending roles loads a heavy column"


async def test_the_titles_adapter_lists_the_pending_roles_light_and_still_names_them_in_order(js, haiku_on):
    """Found by the review of the light hand-out: `titles.free`, the title kind's adapter on the ask's claim (under the
    advisory lock), the dispatcher's rungs and the repeat of the ask, loaded every pending role whole (research,
    breakdown, posting text) and used only its id. It lists them without those columns, in the same order."""
    import re

    from sqlalchemy import event

    from app.jobsearch.work import titles

    ids = await world(*((f"Pending{n}", UNCLEAR.replace("This is an on-site role for the whole team.", ""),
                         {"location": "Riverton, OH"}) for n in range(3)))
    for company in ids:
        await read(ids[company])
    seen: list[str] = []

    def noted(_conn, _cursor, statement, _parameters, _context, _many):
        seen.append(statement)

    engine = db.engine().sync_engine
    async with db.session_factory()() as session:
        event.listen(engine, "before_cursor_execute", noted)
        try:
            free = await titles.free(session, OWNER, scope="facts")
        finally:
            event.remove(engine, "before_cursor_execute", noted)
    async with db.session_factory()() as session:
        in_order = [r.id for r in await gate_facts.waiting(session, OWNER, skip_leased=True)]
    assert sorted(free) == sorted(ids.values()), "all three are named"
    assert free == in_order, "in the order the full read gives them"
    scans = [s for s in seen
             if "FROM js_opportunities" in s and "json_extract(js_opportunities.gate_facts" in s and "status IN" in s]
    assert scans, "the pending roles are looked for"
    heavy = re.compile(r"js_opportunities\.(analysis|description|fit_breakdown|gate_answers|analysis_made_under)\b")
    assert not [s[:120] for s in scans if heavy.search(s)], "no scan of the pending roles loads a heavy column"


async def test_two_sessions_asking_at_the_same_moment_are_handed_different_roles_and_the_last_role_goes_to_one(
        js, haiku_on, monkeypatch):
    """Claims stay exclusive with the reads narrowed: the writer's turn orders the two asks, so the second session sees
    the first's lease; with one role left, only one is handed it. (Old: a tool call took the scout's connection row
    first, with a row lock. New: no row lock exists on SQLite; the kind's turn, `hand_outs.decide`, orders the two asks,
    and the one writer's turn orders their writes.)"""
    import asyncio

    monkeypatch.setattr(gate_facts, "BATCH", 1)
    ids = await world(*((name, UNCLEAR.replace("This is an on-site role for the whole team.", ""),
                         {"location": "Riverton, OH"}) for name in ("Fabrikam", "Initech")))
    for role_id in ids.values():
        await read(role_id)

    async def ask(sid: str) -> list[str]:
        async with db.session_factory()() as session:
            try:
                reply = await tools.titles_next_reply(session, OWNER, sid)
            except RunError:  # nothing waits for this session: the door says so
                return []
            await session.commit()
            return [p["id"] for p in reply["postings"]]

    first, second = await asyncio.gather(ask("titles-1"), ask("titles-2"))
    assert len(first) == len(second) == 1 and first != second, "two roles, one for each"
    async with db.session_factory()() as session:
        assert await gate_facts.waiting(session, OWNER, skip_leased=True) == [], "both are leased now"

    async with db.session_factory()() as session:  # one role waits, two sessions ask: one is handed it
        initech = await session.get(Opportunity, ids["Initech"])
        fabrikam = await session.get(Opportunity, ids["Fabrikam"])
        initech.gate_facts = {**initech.gate_facts, "haiku": {"asks": ["work_mode"], "handed": 0}}
        fabrikam.gate_facts = {**fabrikam.gate_facts, "state": gate_facts.SETTLED}
        await session.commit()
    third, fourth = await asyncio.gather(ask("titles-3"), ask("titles-4"))
    assert sorted([len(third), len(fourth)]) == [0, 1], "the one role went to one session only"
