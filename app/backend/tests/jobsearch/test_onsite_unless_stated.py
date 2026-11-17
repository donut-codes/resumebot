"""A posting that states no way of working is judged as on site (the owner, 2026-11-05, then "yes" to every posting and
not only Boardone's; docs/PRD.md 30.2, 39.11, 46.7 and
48.14 e).

The work location gate judges a posting read whole by every reader of a way of working (the cleaned copy is whole, and
the gate-facts step is done with it, its Haiku read included) as in person where neither the role, its copy nor the
words of its place state a way. The assumption is judged, never stored: a stated way is never replaced, a way that
arrives later replaces it, and the line says "on-site assumed" so the card and a stop can name it. A listing nobody has
read assumes nothing, as an unknown passes. Every name, place and sentence here is made up."""

import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import defer

from app import db
from app.jobsearch import stages
from app.jobsearch.employers import core as employers
from app.jobsearch.gating import check as gating
from app.jobsearch.gating import gate_facts, regate, stated_place
from app.jobsearch.measures.pay_place import ASSUMED_WAY_WORDS, states_a_way, text_states_a_way
from app.jobsearch.models import CleansedPosting, JobPosting, Opportunity, RawPosting
from app.jobsearch.pipeline import dial_hold, placement
from app.jobsearch.postings import cleansed, raw_copies
from app.timeutil import owner_today
from tests.builders import counted
from tests.support import OWNER, load_profile

NOW = datetime.now(UTC)
SECOND = stages.SECOND_IMPRESSION
#: Remote anywhere; hybrid in Oakfield; in person in Fairview only. So with no way of working stated, a posting in
#: Oakfield passes as "hybrid or in person" and fails as in person, and one in Riverton is in none of the places.
GATES = {"location": {"remote": {"on": True}, "hybrid": {"on": True, "locations": ["Oakfield"]},
                      "in_person": {"on": True, "locations": ["Fairview"]}}}
#: A posting that states no way of working, whole enough to read.
SILENT = ("Programs Lead\n\nAbout the job\nLead the programs team and keep the books in order.\n"
          "What the job needs\n- Five years of programs experience\n")
SAYS_REMOTE_IN_WORDS = SILENT + "This is a fully remote position.\n"
#: What the gate-facts step leaves of a role it has read to the end: settled, with the mark that says no ask for the way
#: of working was left outstanding and the posting's copy was whole and said nothing of one (`gate_facts.process`).
SETTLED = {"state": gate_facts.SETTLED, "way": True}


async def _world(slug: str, location: str | None, *, mode: str | None = None, text: str = SILENT,
                 gate: dict | None = SETTLED, copied: bool = True, partial: bool = False,
                 copy_mode: str | None = None, status: str = SECOND, gates: dict | None = None, **more) -> None:
    """A role in the made-up owner's profile (`GATES`, or `gates`) with its gate-facts state `gate` and, when `copied`,
    the cleaned copy of its one posting (`cleansed.gate_copy_of` finds it): whole unless `partial`."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=copy.deepcopy(gates or GATES))
        role = Opportunity(**{
            "owner": OWNER, "slug": slug, "company": f"Firm {slug}", "title": f"Lead {slug}", "origin": "scout",
            "match_key": f"firm {slug}|lead {slug}", "status": status, "found": NOW.date(), "description": text,
            "source_url": f"https://jobs.example.test/{slug}", "source_kind": "employer", "sourced_at": NOW,
            "location": location, "move_type": mode, "fit": 3, "gate_facts": gate or {}, **more})
        session.add(role)
        await session.flush()
        if copied:
            employer = await employers.find_or_make(session, OWNER, role.company)
            session.add(JobPosting(
                owner=OWNER, employer_id=employer.id, ats="tenantone", ext_id=slug, company=role.company,
                title=role.title, match_key=role.match_key, first_seen=NOW, last_seen=NOW, links=[], location="x",
                url=f"https://jobs.example.test/{slug}", opportunity_id=role.id, text=text))
            raw = RawPosting(owner=OWNER, source_id=employer.id, reader="legacy", kind="legacy", ext_id=slug,
                             fetched_at=NOW, body=b"x", body_sha256=slug.ljust(64, "0"), body_bytes=1, no_raw=True)
            session.add(raw)
            await session.flush()
            session.add(CleansedPosting(owner=OWNER, raw_id=raw.id, cleaner_version=1, locations=[location or ""],
                                        work_mode=copy_mode, text_clean=text, marks={}, partial=partial))
            await session.flush()
            await raw_copies.repoint(session, OWNER)  # the pointer that the code which writes copies keeps
        await session.commit()


async def _line(slug: str = "role", **edit) -> gating.Line:
    """The role's work location line as the role page reads it: the role with the copy the gates read (`edit`: columns
    set on the role first, in a transaction that is never saved)."""
    async with db.session_factory()() as session:
        role = (await session.scalars(select(Opportunity).where(Opportunity.slug == slug))).one()
        for column, value in edit.items():
            setattr(role, column, value)
        rules = await gating.rules(session, OWNER)
        subject = gating.role_subject(role, rules.index.of(role.employer_id, role.company),
                                      await cleansed.gate_copy_of(session, OWNER, role.id))
        (line,) = [x for x in gating.results(subject, rules) if x.kind == "location"]
        await session.rollback()
        return line


# --- the rule ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("place", "result"), [
    ("Riverton, OH", gating.FAIL),  # in none of the owner's places: the gate waited for a way before
    ("Oakfield, OH", gating.FAIL),  # a hybrid place, not an in-person one: it passed as "hybrid or in person" before
    ("Fairview, OH", gating.PASS)])
async def test_a_posting_read_whole_that_states_no_way_of_working_is_judged_as_on_site(js, place, result):
    """Old: no way of working stated, so Riverton was unknown (it passed, and a read role waited for it), Oakfield
    passed as "hybrid or in person" and Fairview passed. New: judged as in person, so Riverton and Oakfield fail and
    Fairview passes; each line says the way was assumed."""
    await _world("role", place)
    line = await _line()
    assert line.result == result
    assert ASSUMED_WAY_WORDS in line.fact and place in line.fact


async def test_a_stop_on_the_assumption_names_it_and_stores_no_way_of_working(js):
    await _world("role", "Riverton, OH")
    async with db.session_factory()() as session:
        role = (await session.scalars(select(Opportunity))).one()
        rules = await gating.rules(session, OWNER)
        copy_of = await cleansed.gate_copy_of(session, OWNER, role.id)
        subject = gating.role_subject(role, None, copy_of)
        stop = gating.check(subject, rules)
        assert stop is not None and stop.gate_id == "location" and ASSUMED_WAY_WORDS in stop.role_words
        assert subject.work_mode is None, "judged, never stored on the subject"
        assert role.move_type is None and copy_of is not None and copy_of.work_mode is None
        assert "work_mode" not in gating.job_facts(subject, rules.profile, rules.today), (
            "a Prefer or Must on the way of working still sees none stated")


@pytest.mark.parametrize(("place", "edit", "copy_mode", "result"), [
    ("Riverton, OH", {"move_type": "remote"}, None, gating.PASS),  # the role holds remote
    ("Riverton, OH", {"move_type": "hybrid"}, None, gating.FAIL),  # and hybrid: failed as hybrid, not as assumed
    ("Fairview, OH", {"move_type": "onsite"}, None, gating.PASS),
    ("Riverton, OH", {}, "remote", gating.PASS),  # the copy's way (a listing's tag, or read from the text)
    ("Riverton, OH", {}, "hybrid", gating.FAIL),
    ("Remote, US", {}, None, gating.PASS),  # the words of the place state remote
    ("Hybrid - Riverton, OH", {}, None, gating.FAIL),  # and hybrid
    ("Riverton, OH (not remote)", {}, None, gating.FAIL)])  # and deny remote: read by the gate, never assumed
async def test_a_stated_way_of_working_is_never_replaced_by_the_assumption(js, place, edit, copy_mode, result):
    await _world("role", place, copy_mode=copy_mode)
    line = await _line(**edit)
    assert line.result == result
    assert ASSUMED_WAY_WORDS not in line.fact


async def test_a_way_of_working_that_arrives_later_replaces_the_assumption(js):
    """Nothing is stored, so the next judgement reads the way the role holds then: the assumption failed the role, the
    way the posting states later passes it."""
    await _world("role", "Riverton, OH")
    assert (await _line()).result == gating.FAIL
    later = await _line(move_type="remote")
    assert later.result == gating.PASS and ASSUMED_WAY_WORDS not in later.fact


async def test_the_gate_still_cannot_tell_a_place_that_names_no_town_even_as_on_site(js):
    """The assumption is of the way, not of the place: "United States" names no town, so on site it is still unknown."""
    await _world("role", "United States")
    line = await _line()
    assert line.result == gating.UNKNOWN and ASSUMED_WAY_WORDS not in line.fact


@pytest.mark.parametrize("held", [
    {"gate": None}, {"gate": {"state": gate_facts.PENDING}}, {"copied": False}, {"partial": True}],
    ids=["no gate-facts read", "a Haiku read still to come", "no cleaned copy", "a partial copy"])
async def test_nothing_is_assumed_before_every_reader_has_had_the_whole_posting(js, held):
    """Old and new: unknown, so the role passes (an unknown passes) and a read role waits for its place."""
    await _world("role", "Riverton, OH", **held)
    line = await _line()
    assert line.result == gating.UNKNOWN and ASSUMED_WAY_WORDS not in line.fact


async def test_a_listing_nobody_has_read_assumes_nothing(js):
    """A posting at a board read, or a report from a reader, is a listing: it has not been read, so nothing it does not
    say is assumed (the unknown passes, and the text is read later)."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=copy.deepcopy(GATES))
        rules = await gating.rules(session, OWNER)
        posting = JobPosting(owner=OWNER, ats="tenantone", ext_id="x", company="Firm", title="Lead",
                             location="Riverton, OH", first_seen=NOW, last_seen=NOW, links=[], text=SILENT)
        assert gating.check(gating.posting_subject(posting, None), rules) is None
        assert gating.check(gating.report_subject({"company": "Firm", "location": "Riverton, OH", "title": "Lead"},
                                                  None, None), rules) is None


@pytest.mark.parametrize(("mode", "place", "says"), [
    ("remote", None, True), ("hybrid", None, True), ("onsite", None, True), ("restricted_remote", None, True),
    (None, None, False), ("", "Riverton, OH", False), ("unknown", "Riverton, OH", False),
    ("local", "Riverton, OH", False),  # a word an imported tracker holds, which the gate reads as none
    (None, "Remote, US", True), (None, "Hybrid - Riverton", True), (None, "Riverton, OH (on-site)", True),
    (None, "Riverton, OH (not remote)", True), (None, "United States", False),
    # Words a board writes for a way the gate's own reading of a place does not know (found in review, 2026-11-05).
    (None, "Anywhere in Country", True), (None, "Virtual Office", True),
    (None, "District of Columbia, Virtual Address", True), (None, "Nationwide", True),
    (None, "New Jersey - Home Teleworkers; Pennsylvania - Home Teleworkers", True),
    (None, "US, Flexible, Riverton, OH", True), (None, "Riverton, OH", False)])
def test_what_states_a_way_of_working(mode, place, says):
    assert states_a_way(mode, place) is says


@pytest.mark.parametrize(("text", "says"), [
    ("Lead the programs team and keep the books in order.", False),
    ("Interviews can be held from one of our US hubs or remotely in the United States.", True),
    ("Location-based hybrid policy: most staff are in the office at least 25% of the time.", True),
    ("This role follows a hybrid work model, three days in the office.", True),
    ("#LI-REMOTE", True), ("#LI-Hybrid", True), ("Work arrangement Remote", True), ("hybrid/remote position", True),
    ("This role is currently designated as remote.", True), ("You can work from home two days a week.", True),
    ("Teleworking is possible.", True),
    # A remote that is denied states no way, as the gate reads a place (`_REMOTE_DENIED`); on-site words none either.
    ("This job is not remote.", False), ("Remote work is not available.", False), ("No remote work.", False),
    ("This is an on-site role in the main office.", False), ("", False), (None, False)])
def test_what_in_a_text_states_a_way_of_working(text, says):
    assert text_states_a_way(text) is says


# --- the readers go first ---------------------------------------------------------------------------------------------


async def _read(slug: str = "role") -> tuple[gate_facts.Outcome, Opportunity]:
    async with db.session_factory()() as session:
        role = (await session.scalars(select(Opportunity).where(Opportunity.slug == slug))).one()
        outcome = await gate_facts.process(session, role)
        await session.commit()
        await session.refresh(role)
        return outcome, role


async def test_the_gate_facts_step_stops_a_role_on_the_assumption_once_no_read_is_to_come(js):
    """Old: a role in Riverton whose posting states no way of working passed and went on to the analyst. New: with no
    Haiku read to come (the tests' default: it is off) the step is done with it, so the place gate stops it, and the
    stop says the way was assumed."""
    await _world("role", "Riverton, OH", gate=None)
    outcome, role = await _read()
    assert outcome.state == gate_facts.STOPPED and role.status == stages.GATED_OUT
    assert role.screened_out_gate_id == "location" and ASSUMED_WAY_WORDS in role.screened_out_reason
    assert role.move_type is None and "work_mode" not in role.gate_facts["facts"], "nothing was stored as the way"


async def test_the_haiku_read_is_asked_for_the_way_before_the_assumption_stops_a_role(js, monkeypatch):
    """Old: the gate was undecided, so the way was asked and the role waited. Still so: the step does not stop the role
    on a way it has not finished looking for, whatever its stored state says; a Haiku answer that finds none ends the
    wait and the role is stopped on the assumption, and one that finds a way is judged on that way."""
    monkeypatch.setattr(gate_facts, "HAIKU_READS", True)
    await _world("role", "Riverton, OH", gate=None)
    await _world("found", "Riverton, OH", gate=None, text=SILENT + "Team members work from anywhere.\n")
    outcome, role = await _read()
    assert outcome.state == gate_facts.WAITS and role.status == SECOND
    assert role.gate_facts["state"] == gate_facts.PENDING and role.gate_facts["haiku"]["asks"][-1] == "work_mode"
    await _read("found")
    async with db.session_factory()() as session:
        waiting = await gate_facts.waiting(session, OWNER)
        gate_facts.hand_out(waiting, "titles-1")
        await session.flush()
        found_id = (await session.scalars(select(Opportunity.id).where(Opportunity.slug == "found"))).one()
        said = {"work_mode": {"value": "remote", "quote": "Team members work from anywhere."}}
        taken = await gate_facts.take_answers(session, OWNER, [
            {"id": str(role.id)[:8], "kind": "facts", "facts": {}},
            {"id": str(found_id)[:8], "kind": "facts", "facts": said}])
        await session.commit()
    assert taken["taken"] == 2 and taken["stopped"] == 1 and taken["problems"] == []
    async with db.session_factory()() as session:
        stopped = (await session.scalars(select(Opportunity).where(Opportunity.slug == "role"))).one()
        found = (await session.scalars(select(Opportunity).where(Opportunity.slug == "found"))).one()
        assert stopped.status == stages.GATED_OUT and ASSUMED_WAY_WORDS in stopped.screened_out_reason
        assert found.status == SECOND and found.move_type == "remote", "the way the read found is judged, not on site"


async def test_a_changed_text_is_asked_for_its_way_again_whatever_state_the_role_was_settled_in(js, monkeypatch):
    """A role the step settled earlier holds a settled state while its changed text is read once more: the ask is made
    on the place as the posting states it, never on the assumed on-site that its old state would have armed."""
    monkeypatch.setattr(gate_facts, "HAIKU_READS", True)
    await _world("role", "Riverton, OH", gate={"state": gate_facts.SETTLED, "fp": "an older text"})
    outcome, role = await _read()
    assert outcome.state == gate_facts.WAITS and role.status == SECOND
    assert role.gate_facts["state"] == gate_facts.PENDING


# --- the rest of the readers of the one subject -----------------------------------------------------------------------


async def test_a_read_role_whose_only_gap_was_the_way_of_working_no_longer_waits_for_its_place(js):
    """Old: a read role (its research said the place is stated) in a town the gate cannot judge without a way waited off
    the deck for it. New: once the gate-facts step is done with it the gate decides (on site), so nothing waits; a role
    the step has not read still waits, as before."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=copy.deepcopy(GATES))
        for slug, gate in (("done", SETTLED), ("unread", {}), ("pending", {"state": gate_facts.PENDING})):
            session.add(Opportunity(
                owner=OWNER, slug=slug, company=f"Firm {slug}", title=f"Lead {slug}", origin="scout",
                match_key=f"firm {slug}|lead {slug}", status=SECOND, found=owner_today(),
                posted=owner_today() - timedelta(days=1), location="Riverton, OH", gate_facts=gate,
                **counted(place="stated")))
        await session.commit()
    async with db.session_factory()() as session:
        roles = list((await session.scalars(select(Opportunity).options(
            defer(Opportunity.analysis), defer(Opportunity.description)))).all())
        standing = await placement.load(session, OWNER, roles, placement.Purpose.DROP)
        got = {r.slug: standing[r.id].off_deck for r in roles}
        assert await dial_hold.settle(session, OWNER) == 2
    waits = "waiting for where the work is (the work location gate cannot be decided yet)"
    assert got == {"done": None, "unread": waits, "pending": waits}


async def test_the_analyst_is_not_asked_for_a_way_of_working_the_assumption_settles(js):
    """Old: a role in a town the gate cannot judge without a way refused the analyst's findings until they sent one.
    New: where the posting was read whole and states none, the gate is decided, so the town is all it needs; with the
    way stated by the analyst it is judged on that."""
    await _world("role", "Riverton, OH")
    async with db.session_factory()() as session:
        role = (await session.scalars(select(Opportunity))).one()
        rules = await gating.rules(session, OWNER)
        gate_copy = await cleansed.gate_copy_of(session, OWNER, role.id)
        assert not stated_place.undecided(role, rules, copy=gate_copy)
        assert stated_place.undecided(role, rules, copy=gate_copy, read=False), "as the posting states it, it is not"
        assert stated_place.problem(role, rules, None, None, SILENT, False, gate_copy) is None
        assert not stated_place.undecided(role, rules, work_mode="remote", copy=gate_copy)


# --- what a posting says in its own words is never assumed away (found in review, 2026-11-05) -------------------------

#: Sentences of live postings (reworded) whose copy says remote or hybrid in words code does not read as the
#: posting's way of working (`posting_facts.mode_from_text`): the assumption must leave their roles alone.
SAYS_A_WAY = [
    "Interviews can be held from one of our US hubs or remotely in the United States.",
    "Location-based hybrid policy: most staff are in the office at least 25% of the time.",
    "This role follows a hybrid work model, three days in the office.",
    "This is a hybrid work arrangement.",
    "#LI-REMOTE",
    "#LI-Hybrid",
    "This role is currently designated as remote.",
    "Work arrangement Remote",
    "This is a hybrid/remote position.",
    "You may work from home some days.",
]
HYBRID_POLICY = SILENT + SAYS_A_WAY[1] + "\n"


@pytest.mark.parametrize("sentence", SAYS_A_WAY)
async def test_a_posting_whose_text_says_remote_or_hybrid_is_never_assumed_to_be_on_site(js, sentence):
    """Old: only a way code read as the posting's own (a label, "this is a remote role") or the words of the place kept
    a role from the assumption, so a posting that said "hybrid policy" in a sentence was stopped as on site (a tenth
    of the stops on a large database). New: any word of remote or hybrid work in the whole copy, outside a denial of
    remote, leaves the way unknown: the role passes, as an unknown does, for the analyst or a later read."""
    await _world("role", "Riverton, OH", gate=None, text=SILENT + sentence + "\n")
    outcome, role = await _read()
    assert outcome.state != gate_facts.STOPPED and role.status == SECOND and role.screened_out_gate_id is None
    assert (await _line()).result in (gating.UNKNOWN, gating.PASS)


async def test_a_posting_that_says_it_is_fully_remote_is_read_as_remote_and_passes(js):
    await _world("role", "Riverton, OH", gate=None, text=SAYS_REMOTE_IN_WORDS)
    outcome, role = await _read()
    assert outcome.state == gate_facts.CLEAR and role.status == SECOND
    assert role.move_type == "remote" and role.gate_facts["facts"]["work_mode"]["quote"].startswith("This is a fully")


async def test_a_denial_of_remote_work_in_the_text_is_no_statement_of_a_way(js):
    """The gate reads "not remote" in a place as no remote (`pay_place._REMOTE_DENIED`), and so does this: it is still
    judged as in person."""
    await _world("role", "Riverton, OH", gate=None, text=SILENT + "Remote work is not available for this job.\n")
    outcome, role = await _read()
    assert outcome.state == gate_facts.STOPPED and ASSUMED_WAY_WORDS in role.screened_out_reason


async def test_the_haiku_read_goes_first_for_a_posting_that_says_hybrid_in_words_code_does_not_read(js, monkeypatch):
    monkeypatch.setattr(gate_facts, "HAIKU_READS", True)
    await _world("role", "Riverton, OH", gate=None, text=HYBRID_POLICY)
    outcome, role = await _read()
    assert outcome.state == gate_facts.WAITS and role.status == SECOND
    assert role.gate_facts["state"] == gate_facts.PENDING and gate_facts.ASK_MODE in role.gate_facts["haiku"]["asks"]


@pytest.mark.parametrize("place", [
    "Anywhere in Country", "Virtual Office", "District of Columbia, Virtual Address",
    "New Jersey - Home Teleworkers; Pennsylvania - Home Teleworkers", "US, Flexible, Riverton, OH"])
async def test_a_place_whose_words_state_a_way_the_gate_does_not_read_is_never_assumed_on_site(js, place):
    """Old: the gate's reading of a place knew remote, hybrid and on-site words only, so these places were judged as
    in person and stopped. New: they state a way (not one the gate can judge), so the way stays unknown and the role
    passes."""
    await _world("role", place, gate=None)
    outcome, role = await _read()
    assert outcome.state != gate_facts.STOPPED and role.status == SECOND and role.screened_out_gate_id is None
    line = await _line()
    assert line.result != gating.FAIL and ASSUMED_WAY_WORDS not in line.fact


# --- the readers really go first: the mark that says the way was looked for -----------------------------------------


async def _earlier_read(slug: str = "role", **held) -> None:
    """The role as an earlier read of its present text left it: settled, with `held` kept beside (no `way` mark unless
    `held` has one): what every role read before the mark existed looks like."""
    async with db.session_factory()() as session:
        role = (await session.scalars(select(Opportunity).where(Opportunity.slug == slug))).one()
        text = (await gate_facts._judged_texts(session, OWNER, [role]))[role.id]
        role.gate_facts = {"fp": gate_facts.fingerprint(role, text), "state": gate_facts.SETTLED, "facts": {},
                           "reader": gate_facts.READER, **held}
        await session.commit()


def _step_role(held: dict, status: str = SECOND, analysis_state: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(gate_facts=held, status=status, analysis_state=analysis_state)


@pytest.mark.parametrize(("role", "read"), [
    (_step_role({"state": gate_facts.SETTLED, "way": True}), True),
    (_step_role({"state": gate_facts.GATED, "way": True}), True),  # stopped on the assumption itself
    (_step_role({"state": gate_facts.SETTLED, "way": False}), False),  # the copy was not whole, or said a way
    (_step_role({"state": gate_facts.PENDING, "way": True}), False),
    (_step_role({}), False),
    (_step_role({"state": gate_facts.SETTLED}), False),  # read before the mark: the step reads it again (`_due`)
    (_step_role({"state": gate_facts.GATED}), False),  # a stop before any ask, since released: never asked the way
    # The step no longer reads a role researched or in Ready to Launch; the analyst was its reader.
    (_step_role({"state": gate_facts.SETTLED}, SECOND, "done"), True),
    (_step_role({"state": gate_facts.SETTLED}, stages.READY_TO_LAUNCH), True),
    (_step_role({"state": gate_facts.GATED}, stages.GATED_OUT), False),  # a role stopped for another gate, long ago
    (_step_role({"state": gate_facts.SETTLED, "way": False}, SECOND, "done"), False)])
def test_when_the_way_of_working_has_been_looked_for(role, read):
    assert gating.facts_read(role) is read


async def test_a_role_read_before_the_way_was_looked_for_is_asked_first_and_not_stopped_on_the_assumption(
        js, monkeypatch):
    """Old: state `settled` counted as every reader done, so a role read when its asks held no way (a quarter of the
    roles the assumption stopped on a large database) failed as on site with no read of the way ever made. New: a
    settled role with no `way` mark is due (`stale`), and the read puts it back to a Haiku read of the way before it is
    judged."""
    monkeypatch.setattr(gate_facts, "HAIKU_READS", True)
    await _world("role", "Riverton, OH", gate=None)
    await _earlier_read(haiku={"asks": ["pay"], "handed": 1, "answered": NOW.isoformat()})
    async with db.session_factory()() as session:
        assert await gate_facts.stale(session, OWNER, limit=10)
    outcome, role = await _read()
    assert outcome.state == gate_facts.WAITS and role.status == SECOND
    assert role.gate_facts["state"] == gate_facts.PENDING and role.gate_facts["haiku"] == {
        "asks": ["work_mode"], "handed": 0}
    assert "way" not in role.gate_facts, "not looked for until the answer comes"
    async with db.session_factory()() as session:
        assert await gate_facts.stale(session, OWNER, limit=10) == [], "and nothing is read again meanwhile"


async def test_a_role_asked_the_way_before_is_not_asked_again_and_is_judged_once_marked(js, monkeypatch):
    monkeypatch.setattr(gate_facts, "HAIKU_READS", True)
    await _world("role", "Riverton, OH", gate=None)
    await _earlier_read(haiku={"asks": ["place", "work_mode"], "handed": 1, "answered": NOW.isoformat()})
    outcome, role = await _read()
    assert outcome.state == gate_facts.STOPPED and role.status == stages.GATED_OUT
    assert ASSUMED_WAY_WORDS in role.screened_out_reason and role.gate_facts["way"] is True


async def test_a_role_read_before_the_mark_is_marked_and_stopped_when_no_read_is_to_come(js):
    await _world("role", "Riverton, OH", gate=None)
    await _earlier_read()
    outcome, role = await _read()
    assert outcome.state == gate_facts.STOPPED and ASSUMED_WAY_WORDS in role.screened_out_reason
    assert role.gate_facts["way"] is True
    await _world("said", "Riverton, OH", gate=None, text=HYBRID_POLICY)
    await _earlier_read("said")
    outcome, said = await _read("said")
    assert outcome.state == gate_facts.CLEAR and said.status == SECOND and said.gate_facts["way"] is False, (
        "marked as looked for, and the copy said a way: nothing is assumed, and it is not looked at again")


async def test_a_role_stopped_for_another_gate_before_its_way_was_asked_is_asked_when_it_comes_back(js, monkeypatch):
    """A stop made before the asks (the role's state is `gated`) says nothing of the way: a role the re-gate releases
    carries no mark and is read again."""
    monkeypatch.setattr(gate_facts, "HAIKU_READS", True)
    await _world("role", "Riverton, OH", gate=None)
    await _earlier_read(state=gate_facts.GATED, stop="compensation")
    outcome, role = await _read()
    assert outcome.state == gate_facts.WAITS and role.gate_facts["state"] == gate_facts.PENDING


async def test_the_re_gate_keeps_a_role_the_assumption_stopped_where_it_is(js):
    """Old and new: the stop is judged on the same subject wherever it is asked, so the re-gate states it again and does
    not release the role (a role whose stored state said nothing would pass: the way is unknown)."""
    await _world("role", "Riverton, OH", gate=None)
    outcome, role = await _read()
    assert outcome.state == gate_facts.STOPPED and role.gate_facts["state"] == gate_facts.GATED
    async with db.session_factory()() as session:
        await regate.run(session, OWNER, cause="Some stops were judged under an older Hard pass")
        await session.commit()
    async with db.session_factory()() as session:
        kept = (await session.scalars(select(Opportunity))).one()
        assert kept.status == stages.GATED_OUT and kept.screened_out_gate_id == "location"
        assert ASSUMED_WAY_WORDS in kept.screened_out_reason


async def test_the_step_ends_a_read_with_the_mark_and_the_answers_end_theirs(js, monkeypatch):
    monkeypatch.setattr(gate_facts, "HAIKU_READS", True)
    await _world("role", "Riverton, OH", gate=None, text=SILENT + "Team members work from anywhere.\n")
    await _world("fair", "Fairview, OH", gate=None)
    outcome, fair = await _read("fair")
    assert outcome.state == gate_facts.CLEAR and fair.gate_facts["state"] == gate_facts.SETTLED
    assert fair.gate_facts["way"] is True, "no ask was left: the way was looked for, and the copy says none"
    outcome, role = await _read()
    assert outcome.state == gate_facts.WAITS and "way" not in role.gate_facts
    async with db.session_factory()() as session:
        gate_facts.hand_out(await gate_facts.waiting(session, OWNER), "titles-1")
        await session.flush()
        taken = await gate_facts.take_answers(session, OWNER, [{"id": str(role.id)[:8], "kind": "facts", "facts": {}}])
        await session.commit()
    assert taken["taken"] == 1 and taken["stopped"] == 1
    async with db.session_factory()() as session:
        answered = (await session.scalars(select(Opportunity).where(Opportunity.slug == "role"))).one()
        assert answered.gate_facts["way"] is True and answered.status == stages.GATED_OUT


# --- a role that passes only for want of a way is asked for it ------------------------------------------------------


async def test_a_role_that_passes_as_hybrid_or_in_person_and_fails_as_in_person_is_asked_for_the_way(js, monkeypatch):
    """Old: the Haiku read was asked the way only when the place was undecided as the posting states it, so a town in
    the owner's hybrid places (passing for want of a way) went from pass to fail with no ask. New: it is asked when the
    assumed verdict differs from the stated one; a town that passes either way is not."""
    monkeypatch.setattr(gate_facts, "HAIKU_READS", True)
    await _world("hybrid", "Oakfield, OH", gate=None)
    await _world("fair", "Fairview, OH", gate=None)
    outcome, hybrid = await _read("hybrid")
    assert outcome.state == gate_facts.WAITS and hybrid.gate_facts["haiku"]["asks"] == ["work_mode"]
    outcome, fair = await _read("fair")
    assert outcome.state == gate_facts.CLEAR and fair.gate_facts["haiku"]["asks"] == []
    async with db.session_factory()() as session:
        role = (await session.scalars(select(Opportunity).where(Opportunity.slug == "hybrid"))).one()
        rules = await gating.rules(session, OWNER)
        gate_copy = await cleansed.gate_copy_of(session, OWNER, role.id)
        assert stated_place.asks_the_way(role, rules, gate_copy)
        assert not stated_place.asks_the_way(role, rules, None), "no whole copy: nothing is assumed, so nothing differs"


# --- what the deck's wait reads --------------------------------------------------------------------------------------


async def test_the_deck_assumes_a_way_only_for_a_role_whose_read_saw_the_copy_whole_and_silent(js):
    """Old: placement judged on the state alone (`settled`), so a read role whose posting was a snippet when it was read
    was decided as on site there while the gates left it unknown. New: it goes by the mark the step stored with the
    state (a read made before the mark exists counts for a researched role: the analyst read it)."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=copy.deepcopy(GATES))
        for slug, gate in (("seen", SETTLED), ("snippet", {"state": gate_facts.SETTLED, "way": False}),
                           ("older", {"state": gate_facts.SETTLED})):
            session.add(Opportunity(
                owner=OWNER, slug=slug, company=f"Firm {slug}", title=f"Lead {slug}", origin="scout",
                match_key=f"firm {slug}|lead {slug}", status=SECOND, found=owner_today(),
                posted=owner_today() - timedelta(days=1), location="Riverton, OH", gate_facts=gate,
                **counted(place="stated")))
        await session.commit()
    async with db.session_factory()() as session:
        roles = list((await session.scalars(select(Opportunity).options(
            defer(Opportunity.analysis), defer(Opportunity.description)))).all())
        standing = await placement.load(session, OWNER, roles, placement.Purpose.DROP)
        got = {r.slug: standing[r.id].off_deck for r in roles}
    waits = "waiting for where the work is (the work location gate cannot be decided yet)"
    assert got == {"seen": None, "snippet": waits, "older": None}


# --- the words of a stop -------------------------------------------------------------------------------------------


async def test_a_place_that_fails_every_way_says_so_and_not_the_assumption(js):
    """Old: with the assumption in force a place that fails remote, hybrid and in person alike said "on-site
    assumed", as if the stop rested on it. New: it says it fails whatever the way (the owner, 2026-10-23); no
    assumption is named."""
    us_only = {**GATES, "location": {**GATES["location"], "remote": {"on": True, "locations": ["United States"]}}}
    await _world("role", "Tokyo, Japan", gates=us_only)
    line = await _line()
    assert line.result == gating.FAIL and "fails whether remote, hybrid or in person" in line.fact
    assert ASSUMED_WAY_WORDS not in line.fact
