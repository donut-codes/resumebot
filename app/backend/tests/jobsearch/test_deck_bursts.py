"""The deck's decision asks only what it needs: which Musts on a dial wait for a fact
(`gating.waiting_dials`), whether the work location gate is undecided (`gating.location_undecided`), the standing of
the roles in slices that give the loop back (`placement.standing_async`), and the deck's roles without the columns the
queue never reads. Each answers what the full check answers, with less work. Made-up data only."""

import asyncio
import copy
import itertools
import re
import uuid

from sqlalchemy import event, inspect, select

from app import db
from app.jobsearch import stages
from app.jobsearch.gating import check as gating
from app.jobsearch.gating import stated_place
from app.jobsearch.models import Employer, Opportunity, RoleRank
from app.jobsearch.pipeline import deck, placement
from app.jobsearch.profile import schema as p
from tests.builders import counted, rank_roles
from tests.jobsearch.test_dials import HOME, TODAY, must, profile_with, rules_of, stored, subject
from tests.jobsearch.test_pipeline_deck import _fit, _world
from tests.jobsearch.test_pipeline_deck import _role as _deck_role
from tests.support import OWNER, PROFILE

WAITING = "waiting for "


def _musts() -> list[dict]:
    """One Must of each kind that can wait, and one that never does."""
    return [
        must("experience", "<=", 5),
        must("size", "in", "micro,small", kind="employer_fact", fact="size_band"),
        must("title", "excludes", "intern, trainee"),
        must("degree", "!=", "doctorate"),  # a pick dial whose op is not "in"
        must("freshness", "<=", 30),
        must("employer-named", "=", "yes"),
        must("ownership", "in", "public", kind="employer_fact", fact="ownership"),
    ]


def _table() -> list[gating.Subject]:
    """Subjects that state or leave out each fact, with a title that holds a listed word or not."""
    rows = []
    for facts in ({}, {"experience_years": "9"}, {"experience_years": "2", "degree_required": "doctorate"}):
        for title in ("Programs Lead", "Summer Intern"):
            for employer in (None, Employer(owner=OWNER, key="k", name="K", employees=40),
                             Employer(owner=OWNER, key="j", name="J", employees=5000, ownership="public")):
                for company in ("Fabrikam", ""):
                    rows.append(subject(stored=stored(**facts), title=title, employer=employer, company=company,
                                        posted=None if not facts else TODAY))
    return rows


def _waits_of_results(s: gating.Subject, rules: gating.Rules) -> list[str]:
    return [line.fact.removeprefix(WAITING) for line in gating.results(s, rules) if line.result == gating.WAITS]


def test_waiting_dials_match_the_waits_lines_of_the_full_results():
    rules = rules_of(profile_with(gates=_musts(), location=HOME))
    seen = set()
    for s in _table():
        assert gating.waiting_dials(s, rules) == _waits_of_results(s, rules)
        seen.update(gating.waiting_dials(s, rules))
    assert len(seen) >= 4, "the table must make several dials wait, or it pins nothing"


def test_waiting_dials_with_a_dial_that_is_not_in_the_roles_target_scope():
    rules = rules_of(profile_with(gates=[must("freshness", "<=", 30)]))
    s = subject(posted=None, found=None, target_role="Some Role")
    assert gating.waiting_dials(s, rules) == _waits_of_results(s, rules)


def _role(**more) -> Opportunity:
    return Opportunity(**{"id": uuid.uuid4(), "owner": OWNER, "slug": "r", "company": "Fabrikam", "title": "Intern",
                          "origin": "scout", "match_key": "k", "status": stages.FIRST_IMPRESSION, "found": TODAY,
                          "posted": None, **more})


def test_off_deck_runs_no_location_pay_or_title_check(monkeypatch):
    rules = rules_of(profile_with(gates=[must("experience", "<=", 5), must("title", "excludes", "intern")]))
    role = _role(location="Milwaukee, WI", move_type="onsite")
    inputs = placement.Inputs({role.id: None}, {}, set(), rules, {})
    expected = placement._off_deck(inputs, [role], placement.Purpose.QUEUE)

    def refuse(*_a, **_k):
        raise AssertionError("asked a question the dials never need")

    monkeypatch.setattr(gating, "location_ok", refuse)
    monkeypatch.setattr(gating, "pay_check", refuse)
    monkeypatch.setattr(gating.dials, "title_hits", refuse)
    assert placement._off_deck(inputs, [role], placement.Purpose.QUEUE) == expected
    assert expected == {role.id: WAITING + "the years of experience asked"}


OAKFIELD = {"in_person": {"on": True, "locations": ["Oakfield"]}}
ANYWHERE_REMOTE = {"remote": {"on": True}, "hybrid": {"on": True, "locations": ["Oakfield"]}, **OAKFIELD}
PAY = {"amount": 100_000, "against": "maximum"}


def _place_rules(everywhere: dict, own: dict | None, *, pay: bool) -> gating.Rules:
    data = copy.deepcopy(PROFILE)
    gates: dict = {"location": everywhere, "other": []}
    if pay:
        gates["compensation"] = PAY
    if own is not None:
        gates["by_role"] = {"builders": own}
    data["gates"] = gates
    data["scoring"] = {"asks": []}
    return gating.Rules.of(p.Profile.model_validate(data), today=TODAY, hard_pass=1, criteria=1)


def test_location_undecided_matches_the_location_lines_of_the_full_results():
    scopes = [({}, None), (OAKFIELD, None), (ANYWHERE_REMOTE, None), (OAKFIELD, {"location": ANYWHERE_REMOTE}),
              ({}, {"location": OAKFIELD}), (OAKFIELD, {"compensation": PAY})]
    places = itertools.product(("Oakfield", "Elsewhere, ZZ", "Remote", None), (None, "remote", "onsite"),
                               (None, "builders"), (False, True))
    answers = set()
    for (everywhere, own), (location, mode, target, read) in itertools.product(scopes, places):
        rules = _place_rules(everywhere, own, pay=True)
        s = subject(location=location, work_mode=mode, target_role=target, way_read=read)
        full = any(line.kind == "location" and line.result == gating.UNKNOWN for line in gating.results(s, rules))
        assert gating.location_undecided(s, rules) is full
        answers.add(full)
    assert answers == {True, False}, "the table must hold both answers, or it pins nothing"


def test_place_undecided_never_prices_a_role(monkeypatch):
    rules = _place_rules(ANYWHERE_REMOTE, {"location": OAKFIELD}, pay=True)
    role = _role(location="Elsewhere, ZZ", move_type=None, target_role="builders")

    def refuse(*_a, **_k):
        raise AssertionError("priced a role to tell where it works")

    monkeypatch.setattr(gating, "pay_check", refuse)
    monkeypatch.setattr(gating, "posted_band", refuse)
    monkeypatch.setattr(gating, "job_facts", refuse)
    assert placement._place_undecided(role, rules, None) is True
    assert placement._place_undecided(role, rules, stated_place.NOT_STATED) is False


async def test_standing_gives_the_loop_back_between_slices():
    rules = rules_of(profile_with(gates=[must("experience", "<=", 5)]))
    roles = [_role(slug=f"r{i}", match_key=f"k{i}") for i in range(450)]
    inputs = placement.Inputs({r.id: None for r in roles}, {}, set(), rules, {})
    whole = placement.standing(inputs, roles, placement.Purpose.QUEUE)
    assert all(s.off_deck for s in whole.values()), "every role is barred, so each slice does work"

    turns = 0
    done = False

    async def probe() -> None:
        nonlocal turns
        while not done:
            turns += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(probe())
    await asyncio.sleep(0)
    turns = 0
    sliced = await placement.standing_async(inputs, roles, placement.Purpose.QUEUE, size=100)
    done = True
    await task
    assert turns >= 4, "the loop was given back at every slice"
    assert list(sliced) == list(whole) and sliced == whole


ANSWERS = [{"id": "a-rule", "answer": "pass", "evidence": "a made-up line"}]
#: The columns the queue's decision never reads, which a reader's snapshot leaves unloaded.
NEVER_READ = ("gate_answers", "analysis_made_under")


def _queue_roles():
    return [_deck_role("r1", stages.FIRST_IMPRESSION, **counted(), **_fit(95), gate_answers=ANSWERS),
            _deck_role("r2", stages.SECOND_IMPRESSION, **counted(), **_fit(90), gate_answers=ANSWERS),
            _deck_role("d1", stages.FIRST_IMPRESSION, **counted(), **_fit(80)),
            _deck_role("low", stages.SECOND_IMPRESSION, **counted(), **_fit(40)),
            _deck_role("unread", stages.FIRST_IMPRESSION, gate_answers=ANSWERS)]


def _worked_out(plan) -> tuple:
    """What a plan does, by role id: the same plan whichever session built the roles."""
    return ([r.id for r in plan.lifts], [r.id for r in plan.placed], [(m.role.id, m.target) for m in plan.moves],
            [(m.role.id, m.on, m.key) for m in plan.marks])


async def test_snapshot_does_not_load_the_answers_the_queue_never_reads(js):
    await _world(2, *_queue_roles(), ranks={"unread": 99})
    asked: list[str] = []

    def noted(_conn, _cursor, statement, *_rest) -> None:
        asked.append(re.sub(r"\s+", " ", statement))

    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", noted)
    try:
        async with db.session_factory()() as session:
            snapshot = await deck._snapshot(session, OWNER)
    finally:
        event.remove(engine, "before_cursor_execute", noted)
    assert snapshot is not None
    roles_select = [q for q in asked if q.startswith("SELECT") and "js_opportunities.fit_breakdown" in q]
    assert roles_select and not [q for q in roles_select for column in NEVER_READ if column in q], (
        "the deck's roles are read without the columns it never decides from")
    assert all({"gate_answers", "analysis_made_under"} <= inspect(r).unloaded for r in snapshot.roles)
    async with db.session_factory()() as session:
        whole = await deck._read_snapshot(session, OWNER, await deck._deck_roles(session, OWNER))
        assert whole is not None
        assert _worked_out(deck._decide(snapshot)) == _worked_out(deck._decide(whole))
        assert deck._decide(snapshot).moves, "the queue has something to do, or the equality pins nothing"


async def test_read_snapshot_in_the_callers_transaction_still_loads_gate_answers(js):
    await _world(2, *_queue_roles(), ranks={"unread": 99})
    async with db.session_factory()() as session:
        await session.scalar(select(Opportunity.id).limit(1))  # a transaction is open: the snapshot is read in it
        snapshot = await deck._snapshot(session, OWNER)
        assert snapshot is not None
        assert all(not ({"gate_answers", "analysis_made_under"} & inspect(r).unloaded) for r in snapshot.roles)


async def test_ranked_ids_equal_the_keys_of_ranks(js):
    ids = await _world(1, _deck_role("twice", stages.FIRST_IMPRESSION), _deck_role("none", stages.FIRST_IMPRESSION),
                       _deck_role("once", stages.CLEANSED_DATA), ranks={"twice": 60, "once": 30})
    async with db.session_factory()() as session:
        await rank_roles(session, {ids["twice"]: 70})  # a second copy of the rank
        first = await session.scalar(select(RoleRank).where(RoleRank.role_id == ids["twice"]))
        session.add(RoleRank(owner=OWNER, role_id=ids["twice"], cleansed_id=first.cleansed_id, profile_version=2,
                             fit=80, why=""))  # and one for a newer profile
        theirs = Opportunity(owner="someone-else", slug="theirs", company="Firm", title="Lead", origin="scout",
                             match_key="firm|lead", status=stages.FIRST_IMPRESSION, found=TODAY, posted=TODAY)
        session.add(theirs)
        await session.flush()
        await rank_roles(session, {theirs.id: 50}, owner="someone-else")
        await session.commit()
    async with db.session_factory()() as session:
        wanted = await deck.ranked_ids(session, OWNER)
        assert wanted == set(await deck.ranks(session, OWNER)) == {ids["twice"], ids["once"]}
