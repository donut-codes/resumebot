"""The owner's rules (`gating.rules`) are built once for as long as what they are made of does not change: the four
documents in force (the ids of their rows), the gate columns of every employer record (a digest of them), the sites the
owner avoids, and the day (`GATE_EMPLOYER_COLUMNS`). Each call asks the database for those three and builds nothing when
they are the ones the kept rules were built from; the rules for a profile a caller gives are never kept; a column of an
employer that no gate reads raises where it is read instead of being quietly old; and the kept rules decide every gate
line as rules built from whole records do. Everything here is made up."""

import asyncio
import re
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import delete, event, inspect, select
from sqlalchemy.orm.exc import DetachedInstanceError

from app import db
from app.jobsearch.gating import check as gating
from app.jobsearch.gating import gate_facts, regate
from app.jobsearch.models import Employer, Opportunity, ProfileVersion
from app.jobsearch.profile import store as profile_store
from app.sites.models import Site
from tests.jobsearch.test_gate_facts import PAY_LOW, REMOTE, world
from tests.jobsearch.test_profile_kept import when_all_have_asked_to_build
from tests.support import FIVE, OWNER, load_profile, pin_clock

NOW = datetime.now(UTC)
GATES = {
    "location": {"remote": {"on": True}, "hybrid": {"on": True, "locations": ["Oakfield"]},
                 "in_person": {"on": True, "locations": ["Oakfield"]}},
    "compensation": {"amount": 100_000, "against": "maximum"},
    "other": [
        {"id": "no-pe", "kind": "employer_fact", "section": "company", "fact": "ownership", "op": "!=", "value": "pe"},
        {"id": "small", "kind": "employer_fact", "section": "company", "fact": "employees", "op": "<", "value": 1000},
        {"id": "no-gambling", "kind": "employer_fact", "section": "company", "fact": "industry", "op": "excludes",
         "value": "gambling"},
    ],
}


class Sent:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def of(self, table: str) -> list[str]:
        return [re.sub(r"\s+", " ", s) for s in self.statements if f"FROM {table}" in s]


@pytest.fixture
def sent(js):
    seen = Sent()

    def noted(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        if not statement.startswith("BEGIN"):  # a transaction's start is the connection's, not a question of the code's
            seen.statements.append(statement)

    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", noted)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", noted)


def made_employers() -> dict[str, Employer]:
    """A few employers of the kinds the gates tell apart."""
    return {
        "gated": Employer(owner=OWNER, key="fabrikam", name="Fabrikam", gated=True, gated_reason="never again"),
        "private": Employer(owner=OWNER, key="initech", name="Initech", aliases=["Initech Holdings"], ownership="pe",
                            employees=40, founded=1999, headquarters="Oakfield", notes="a note", fetch_ok=True,
                            fetch_note="read", open_roles=12, fetched_at=NOW),
        "big": Employer(owner=OWNER, key="hooli", name="Hooli", employees=5000, ownership="public", local=False,
                        work_style="remote", ai_native=True, events=[{"kind": "layoff", "date": "2026-03"}],
                        sources={"events": {"source": "web"}}),
        "casino": Employer(owner=OWNER, key="globex", name="Globex", industry="Casino gambling",
                           sectors=["Gaming", "Hospitality"], employees=300),
        "plain": Employer(owner=OWNER, key="umbrella", name="Umbrella"),
    }


async def employers_world(session, gates: dict | None = None) -> dict[str, Employer]:
    """The made-up profile and the employers."""
    await load_profile(session, OWNER, scoring=FIVE, gates=GATES if gates is None else gates)
    made = made_employers()
    session.add_all(made.values())
    await session.flush()
    return made


# --- when the rules are built and when they are not -------------------------------------------------------------------


async def test_the_rules_are_built_once_and_a_second_call_is_three_statements(js, sent):
    async with db.session_factory()() as session:
        await employers_world(session)
        await session.commit()
    async with db.session_factory()() as session:
        first = await gating.rules(session, OWNER)
        sent.statements.clear()
        again = await gating.rules(session, OWNER)
    assert len(sent.statements) == 3, "the documents in force, the employers' digest, the sites avoided"
    assert len(sent.of("js_employers")) == 1, "one statement over the employers (their digest): no rules are made"
    assert again.index is first.index and again.gated == first.gated and again.avoided == first.avoided
    assert again.profile == first.profile and (again.hard_pass, again.criteria) == (first.hard_pass, first.criteria)
    assert again.profile is not first.profile, "a copy: what a caller changes in it is its own"


async def test_the_digest_of_the_employers_is_zero_for_none_and_tells_a_json_column_from_another(js):
    """The digest is made here (SQLite has no hash to ask for; `row_digest.over`): of no rows it is the count and two
    zeros, the same rows give the same digest each time, and a change in a gate column, a JSON one included, changes
    it, though not one in a column no gate reads."""
    async with db.session_factory()() as session:
        assert await gating._employers_digest(session, OWNER) == (0, 0, 0)
        made = await employers_world(session)
        await session.commit()
    async with db.session_factory()() as session:
        before = await gating._employers_digest(session, OWNER)
        assert before[0] == 5 and None not in before and before == await gating._employers_digest(session, OWNER)
        assert await gating._employers_digest(session, "someone-else") == (0, 0, 0)

    async def after(change) -> tuple:
        async with db.session_factory()() as session:
            employer = await session.get(Employer, made["plain"].id)
            change(employer)
            await session.commit()
        async with db.session_factory()() as session:
            return await gating._employers_digest(session, OWNER)

    assert await after(lambda e: setattr(e, "notes", "not read by any gate")) == before
    one = await after(lambda e: setattr(e, "events", [{"kind": "layoff", "date": "2026-04"}]))
    two = await after(lambda e: setattr(e, "events", [{"kind": "layoff", "date": "2026-05"}]))
    assert len({before, one, two}) == 3 and one[0] == two[0] == 5


async def test_the_rules_say_the_versions_in_force_and_the_day(js, monkeypatch):
    """With no day given the rules are for the owner's day, which here is not the day it is in UTC, and the rules for
    the day after it are another kept entry."""
    pin_clock(monkeypatch, datetime(2030, 1, 3, 2, tzinfo=UTC), "America/New_York")  # 9 pm on the 2nd for the owner
    async with db.session_factory()() as session:
        await employers_world(session)
        await profile_store.save(session, OWNER, "gates", {**GATES, "compensation": {"amount": 90_000,
                                                                                       "against": "maximum"}}, by="t")
        await session.commit()
    async with db.session_factory()() as session:
        rules = await gating.rules(session, OWNER)
        assert (rules.hard_pass, rules.criteria) == (2, 1) and rules.gates.comp_floor == 90_000
        assert rules.today == date(2030, 1, 2)
        day_after = rules.today + timedelta(days=1)
        other_day = await gating.rules(session, OWNER, today=day_after)
        assert other_day.today == day_after and other_day.index is not rules.index, "the day is part of the key"


async def change_and_see(change, look):
    """Build the rules, make `change` in a session of its own and commit, and answer `look(rules)` of the next call. The
    answer is what a test asserts on: rules are a new copy at every call, so that they are not the object built before
    says nothing, and only what `look` finds in them shows that the call saw the change."""
    async with db.session_factory()() as session:
        await gating.rules(session, OWNER)
    async with db.session_factory()() as session:
        await change(session)
        await session.commit()
    async with db.session_factory()() as session:
        return look(await gating.rules(session, OWNER))


async def test_an_employer_gated_shows_at_the_next_call(js):
    async with db.session_factory()() as session:
        await employers_world(session)
        await session.commit()

    async def change(session):
        (await session.scalar(select(Employer).where(Employer.key == "umbrella"))).gated = True

    assert await change_and_see(change, lambda rules: (
        "umbrella" in rules.gated and rules.index.find("Umbrella").gated))


async def test_an_employer_fact_and_a_new_alias_show_at_the_next_call(js):
    async with db.session_factory()() as session:
        await employers_world(session)
        await session.commit()

    async def change(session):
        row = await session.scalar(select(Employer).where(Employer.key == "umbrella"))
        row.employees, row.aliases = 77, ["Umbrella Corporation"]

    assert await change_and_see(change, lambda rules: {
        "employees": rules.facts(rules.index.find("Umbrella"))["employees"],
        "alias": rules.index.find("Umbrella Corporation") is rules.index.find("Umbrella")}) == {
            "employees": 77, "alias": True}


async def test_an_employer_added_or_removed_shows_at_the_next_call(js):
    async with db.session_factory()() as session:
        await employers_world(session)
        await session.commit()

    async def add(session):
        session.add(Employer(owner=OWNER, key="soylent", name="Soylent"))

    assert await change_and_see(add, lambda rules: rules.index.find("Soylent") is not None)

    async def remove(session):
        await session.execute(delete(Employer).where(Employer.key == "soylent"))

    assert await change_and_see(remove, lambda rules: rules.index.find("Soylent") is None)


async def test_a_site_avoided_shows_at_the_next_call(js):
    async with db.session_factory()() as session:
        await employers_world(session)
        await session.commit()

    async def avoid(session):
        session.add(Site(owner=OWNER, url="https://avoid.example.test/jobs", kind="job_board", on=True,
                         list_name="avoid", created_at=NOW))

    assert await change_and_see(avoid, lambda rules: "avoid.example.test" in rules.avoided)


async def test_a_save_of_the_gates_shows_at_the_next_call(js):
    async with db.session_factory()() as session:
        await employers_world(session)
        await session.commit()

    async def save(session):
        await profile_store.save(session, OWNER, "gates", {**GATES, "compensation": {"amount": 120_000,
                                                                                      "against": "maximum"}}, by="t")

    assert await change_and_see(save, lambda rules: rules.gates.comp_floor == 120_000 and rules.hard_pass == 2)


async def test_a_site_taken_off_the_avoid_list_shows_at_the_next_call(js):
    """Nothing else changes: the documents in force and every employer record are the same rows, so the sites are the
    one thing the call can tell by."""
    async with db.session_factory()() as session:
        await employers_world(session)
        session.add(Site(owner=OWNER, url="https://avoid.example.test/jobs", kind="job_board", on=True,
                         list_name="avoid", created_at=NOW))
        await session.commit()

    async def taken_off(session):
        await session.execute(delete(Site).where(Site.owner == OWNER))

    async with db.session_factory()() as session:
        assert "avoid.example.test" in (await gating.rules(session, OWNER)).avoided
    assert await change_and_see(taken_off, lambda rules: not rules.avoided)


async def test_documents_saved_again_under_the_same_version_numbers_show_at_the_next_call(js):
    """The four documents' rows are replaced by new rows that carry the same version numbers (a restored backup, the
    suite's own reset between tests) while every employer record is as it was: only the rows' ids tell the rules kept
    from the ones in force now."""
    async with db.session_factory()() as session:
        await employers_world(session)
        await session.commit()
    async with db.session_factory()() as session:
        old = await gating.rules(session, OWNER)
        assert old.gates.comp_floor == 100_000 and (old.hard_pass, old.criteria) == (1, 1)
        await session.execute(delete(ProfileVersion))
        await session.commit()
        await load_profile(session, OWNER, scoring=FIVE, gates={**GATES, "compensation": {"amount": 111_000,
                                                                                         "against": "maximum"}})
        await session.commit()
    async with db.session_factory()() as session:
        new = await gating.rules(session, OWNER)
    assert (new.hard_pass, new.criteria) == (1, 1), "the same numbers"
    assert new.gates.comp_floor == 111_000, "and the rules of the rows in force now"


async def test_a_change_the_session_has_made_and_not_flushed_shows_to_its_own_next_call(js):
    """As before the rules were kept: the call reads through the session, which sends what it holds first."""
    async with db.session_factory()() as session:
        made = await employers_world(session)
        await session.commit()
        first = await gating.rules(session, OWNER)
        assert "umbrella" not in first.gated
        (await session.get(Employer, made["plain"].id)).gated = True  # not flushed
        assert "umbrella" in (await gating.rules(session, OWNER)).gated
        await session.rollback()
        assert "umbrella" not in (await gating.rules(session, OWNER)).gated


async def test_the_columns_the_nights_reads_rewrite_for_every_employer_do_not_rebuild_the_rules(js, sent):
    async with db.session_factory()() as session:
        await employers_world(session)
        await session.commit()
    async with db.session_factory()() as session:
        first = await gating.rules(session, OWNER)
    async with db.session_factory()() as session:
        for employer in (await session.scalars(select(Employer))).all():
            employer.fetch_ok, employer.fetch_note, employer.open_roles = False, "board gone", 3
            employer.fetched_at, employer.read_full_at, employer.read_asked = NOW, NOW, "abc"
            employer.headquarters, employer.notes = "Elsewhere", "another note"
        await session.commit()
    async with db.session_factory()() as session:
        sent.statements.clear()
        again = await gating.rules(session, OWNER)
    assert again.index is first.index, "nothing a gate reads changed"
    assert len(sent.statements) == 3


async def test_the_rules_for_a_profile_a_caller_gives_are_built_from_it_and_never_kept(js):
    async with db.session_factory()() as session:
        await employers_world(session)
        await session.commit()
    async with db.session_factory()() as session:
        kept = await gating.rules(session, OWNER)
        given = await profile_store.current(session, OWNER)
        given.gates.compensation.amount = 1
        theirs = await gating.rules(session, OWNER, given)
        assert theirs.profile.gates.compensation.amount == 1 and theirs.index is not kept.index
        older = await gating.rules(session, OWNER, given.model_copy(update={"settings": given.settings}))
        assert older.index is not theirs.index, "each is built, from every record, as before"
        after = await gating.rules(session, OWNER)
        assert after.profile.gates.compensation.amount == 100_000
        assert after.index is kept.index, "what is kept is what is in force"


async def test_a_database_emptied_and_filled_again_gives_the_rules_of_what_it_holds_now(js):
    """The rows are gone and come back with the same owner, keys and version numbers, and with one of them the same id
    and all the same words but a flag: no key of the kept rules can be the new one."""
    fixed = uuid.UUID(int=77)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates=GATES)
        session.add(Employer(id=fixed, owner=OWNER, key="fabrikam", name="Fabrikam", gated=False))
        await session.commit()
    async with db.session_factory()() as session:
        old = await gating.rules(session, OWNER)
        assert "fabrikam" not in old.gated and old.hard_pass == 1
        await session.execute(delete(Employer))
        await session.execute(delete(ProfileVersion))
        await session.commit()
        await load_profile(session, OWNER, scoring=FIVE, gates={**GATES, "compensation": {"amount": 111_000,
                                                                                         "against": "maximum"}})
        session.add(Employer(id=fixed, owner=OWNER, key="fabrikam", name="Fabrikam", gated=True))
        await session.commit()
    async with db.session_factory()() as session:
        new = await gating.rules(session, OWNER)
    assert new.hard_pass == 1, "the same number as before it was emptied"
    assert "fabrikam" in new.gated and new.gates.comp_floor == 111_000


# --- what is kept is shared, and is read-only ---------------------------------------------------------------------


async def test_a_column_no_gate_reads_raises_on_a_kept_employer_and_the_rows_belong_to_no_session(js):
    async with db.session_factory()() as session:
        await employers_world(session)
        await session.commit()
    async with db.session_factory()() as session:
        rules = await gating.rules(session, OWNER)
        row = rules.index.find("Initech")
        assert row is not None and row.employees == 40 and row.aliases == ["Initech Holdings"]
        for column in ("headquarters", "notes", "fetch_ok", "open_roles", "board_url", "research"):
            with pytest.raises(DetachedInstanceError):
                getattr(row, column)
        assert all(inspect(e).detached and not inspect(e).modified for e in rules.index.rows)
        assert not [e for e in session.identity_map.values() if isinstance(e, Employer)], "none is the session's own"


async def test_the_kept_rules_decide_every_gate_line_as_rules_built_from_whole_records_do(js):
    """Every gate line, for a subject at each employer: the site, the employer's record and its reason, the work
    location, the pay, and the lines on an employer's ownership, size and industry."""
    async with db.session_factory()() as session:
        await employers_world(session)
        await session.commit()
    async with db.session_factory()() as session:
        kept = await gating.rules(session, OWNER)
        whole_rows = (await session.scalars(select(Employer).where(Employer.owner == OWNER))).all()
        whole = gating.Rules.of(await profile_store.current(session, OWNER), today=kept.today, avoided=kept.avoided,
                                rows=whole_rows, hard_pass=kept.hard_pass, criteria=kept.criteria)
        failed = set()
        for name in ("Fabrikam", "Initech", "Hooli", "Globex", "Umbrella", "Initech Holdings", "Nobody"):
            for place, mode, comp in (("Remote, US", "remote", None), ("Dallas, TX", "onsite", "$60,000-$80,000")):
                role = Opportunity(owner=OWNER, company=name, title="Programs Lead", location=place, move_type=mode,
                                   comp=comp, found=NOW.date())
                one = gating.role_subject(role, kept.index.of(None, name))
                two = gating.role_subject(role, whole.index.of(None, name))
                lines = list(gating.results(one, kept))
                assert lines == list(gating.results(two, whole)), (name, place)
                assert gating.check(one, kept) == gating.check(two, whole)
                failed.update(line.id for line in lines if line.result == gating.FAIL)
        assert {"employer", "no-pe", "small", "no-gambling"} <= failed, f"every kind of line failed somewhere: {failed}"
        assert kept.gated == whole.gated and {k: kept.facts(e) for k, e in kept.index.by_key.items()} == {
            k: whole.facts(e) for k, e in whole.index.by_key.items()}


async def test_the_gates_that_read_the_kept_rules_write_nothing_to_them(js):
    """The gate-facts step, the re-gate and a check of a role read the employers through the shared index: none of them
    changes a record in it (a change would be seen by every caller, and written by none)."""
    async with db.session_factory()() as session:
        session.add_all(made_employers().values())
        await session.commit()
    await world(("Fabrikam", PAY_LOW, REMOTE), ("Initech", PAY_LOW, REMOTE), ("Umbrella", PAY_LOW, REMOTE),
                gates=GATES)
    async with db.session_factory()() as session:
        for company in ("Fabrikam", "Initech", "Umbrella"):
            role = await session.scalar(select(Opportunity).where(Opportunity.company == company))
            role.employer_id = await session.scalar(select(Employer.id).where(Employer.name == company))
        await session.commit()
        assert (await gate_facts.sweep(session, OWNER))["looked"] == 3
        await session.commit()
        await regate.run(session, OWNER, cause="a test")
        await session.commit()
        rules = await gating.rules(session, OWNER)
        assert all(inspect(e).detached and not inspect(e).modified for e in rules.index.rows)
        stopped = {r.company: r.screened_out_gate_id for r in (await session.scalars(select(Opportunity))).all()}
    assert stopped["Fabrikam"] == "employer", "the gated employer's record stopped its role, with the reason it read"
    assert set(stopped.values()) == {"employer", "compensation"} and stopped["Umbrella"] == "compensation"


# --- one build at a time, and none kept from a read a change came in the middle of ---------------------------------


async def test_tasks_asking_at_once_build_the_rules_once(js, monkeypatch):
    async with db.session_factory()() as session:
        await employers_world(session)
        await session.commit()
    built = []
    real = gating._employer_rows
    everyone = when_all_have_asked_to_build(monkeypatch, gating._KEPT, 3)

    async def counting(session, owner):
        built.append(1)
        await asyncio.wait_for(everyone.wait(), 10)  # the other two are at the lock by now
        return await real(session, owner)

    monkeypatch.setattr(gating, "_employer_rows", counting)

    async def ask():
        async with db.read_session_factory()() as session:  # readers: a writer's session would hold the turn from its
            return await gating.rules(session, OWNER)  # first read, and the other two would wait for it, not the lock

    one, two, three = await asyncio.gather(ask(), ask(), ask())
    assert len(built) == 1 and one.index is two.index is three.index


async def test_rules_built_while_an_employer_changed_are_not_kept(js, monkeypatch):
    """A record is changed after the rows were read and before the build ends: the caller gets what it read, the next
    call reads the record as it is (a stale entry could otherwise answer the digest of the state before the change if
    the change were ever undone)."""
    async with db.session_factory()() as session:
        await employers_world(session)
        await session.commit()
    real = gating._employer_rows
    state = {"changed": False}

    async def changing(session, owner):
        rows = await real(session, owner)
        if not state["changed"]:
            state["changed"] = True
            await db.release(session)  # the call's transaction ends, as a read committed one lets another write between
            async with db.session_factory()() as other:
                (await other.scalar(select(Employer).where(Employer.key == "umbrella"))).gated = True
                await other.commit()
        return rows

    monkeypatch.setattr(gating, "_employer_rows", changing)
    async with db.session_factory()() as session:
        first = await gating.rules(session, OWNER)
        assert "umbrella" not in first.gated, "what it read"
        second = await gating.rules(session, OWNER)
        assert "umbrella" in second.gated and second.index is not first.index, "built again: the first was not kept"
        assert (await gating.rules(session, OWNER)).index is second.index


async def test_rules_built_from_rows_a_change_came_before_are_not_kept_for_the_state_it_was_undone_to(js, monkeypatch):
    """A record changes after the call has taken its key (the digest, of the record as it was) and before the rows are
    read, and is changed back before the next call: the rows say the change and the key says the record as it was, so an
    entry kept would answer the next call's very key with the rules of a state that is gone."""
    async with db.session_factory()() as session:
        await employers_world(session)
        await session.commit()

    async def gate(on: bool) -> None:
        async with db.session_factory()() as other:
            (await other.scalar(select(Employer).where(Employer.key == "umbrella"))).gated = on
            await other.commit()

    real = gating._employer_rows
    state = {"changed": False}

    async def changing(session, owner):
        if not state["changed"]:
            state["changed"] = True
            await db.release(session)  # the call's transaction ends first: the change is committed between two of them
            await gate(True)
        return await real(session, owner)

    monkeypatch.setattr(gating, "_employer_rows", changing)
    async with db.session_factory()() as session:
        first = await gating.rules(session, OWNER)
    assert "umbrella" in first.gated, "what it read"
    await gate(False)
    async with db.session_factory()() as session:
        second = await gating.rules(session, OWNER)
    assert "umbrella" not in second.gated and not second.index.find("Umbrella").gated, "the record as it is"
