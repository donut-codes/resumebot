"""A role is settled when its ask, then its score, then its gates have been worked out, in that one order, from what it
holds now (`research/settle.py`). Tests cross its two doors, `settle.roles` and `settle.old_asks`, and read the role,
its history and what the door returns; a role is settled when settling it again changes nothing (`_stands`). Every name,
number and sentence is made up."""


import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select, update
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import defer

from app import db
from app.jobsearch import stages
from app.jobsearch.fit import comp, scoring
from app.jobsearch.models import Employer, Opportunity
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis, prioritize, settle
from app.models.work import TrailEvent
from tests.builders import analysis_setup, findings, put_in_line, submit_findings
from tests.support import FIVE, OWNER, PROFILE, load_profile

#: The made-up floor is $95,000: a pay under it stops a role whose pay the gate judges on the worked ask.
UNDER_THE_FLOOR = [60_000, 70_000]


OVER_THE_FLOOR = [150_000, 180_000]


#: With one item Proven and one Unproven, this band gives an ask of exactly the floor (it passes), and with both
#: Unproven $80,000 (it fails).
AT_THE_FLOOR = [70_000, 150_000]


#: A band whose ask for the same two items is $130,000.
HIGHER = [100_000, 200_000]


#: The ask `UNDER_THE_FLOOR` gives for them (the bottom of the band).
LOWER_ASK = 60_000


#: A made-up line worth a point to a likely ask over $120,000, and one worth two to a private employer.
ASK_OVER = {"id": "pay-over", "section": "compensation", "says": "Likely ask over 120K", "points": 1, "amount": 120_000,
            "above": True}


PRIVATE = {"id": "ask-private", "section": "posting", "says": "The employer is private", "points": 2,
           "checked_by": "app", "fact": "ownership", "op": "in", "value": "private"}


CAUSE = "A fact the role reads changed"


GATE_LINES = ("regate_stopped", "regate_released", "regate_restated")


async def _role(role_id) -> Opportunity:
    async with db.session_factory()() as session:
        return await session.get(Opportunity, role_id, populate_existing=True)


async def _lines(role_id, *events: str) -> list[TrailEvent]:
    async with db.session_factory()() as session:
        query = select(TrailEvent).where(TrailEvent.link_id == role_id).order_by(TrailEvent.at, TrailEvent.id)
        if events:
            query = query.where(TrailEvent.event.in_(events))
        return list((await session.scalars(query)).all())


async def _world(*companies: str, band: list[int] = AT_THE_FLOOR) -> dict[str, uuid.UUID]:
    """The made-up asks plus two that read the ask and the employer, and these roles researched with `band` (one item
    Proven and one Unproven, a private employer): the ids by company."""
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring={"asks": [*FIVE["asks"], ASK_OVER, PRIVATE]})
        await session.commit()
    for company in companies:
        await put_in_line(ids[company])
        assert (await submit_findings(findings(ids[company], pay={"band": band})))["taken"] is True
    return ids


async def _settle(role_id, change=None, *, cause: str = CAUSE, keep_gated_out: bool = False) -> settle.Settled:
    """Set the new facts on the role (`change`, given the session and the row), settle it and commit."""
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        if change is not None:
            await change(session, role)
        done = await settle.roles(session, [role], cause=cause, keep_gated_out=keep_gated_out)
        await session.commit()
    return done


def _research(role: Opportunity, **pay) -> dict:
    return {**role.analysis, "pay": {**role.analysis["pay"], **pay}}


async def _lower_band(session, role) -> None:
    role.analysis = _research(role, band=UNDER_THE_FLOOR)


async def _raise_band(session, role) -> None:
    role.analysis = _research(role, band=HIGHER)


async def _correct_class(session, role) -> None:
    first, *rest = role.analysis["required"]
    role.analysis = {**role.analysis, "required": [{**first, "class": "U", "by": "owner"}, *rest]}


async def _fill_place(session, role) -> None:
    role.location, role.move_type = "Faraway, TX", "onsite"


async def _change_employer(session, role) -> None:
    employer = await session.get(Employer, role.employer_id)
    employer.gated, employer.gated_reason, employer.ownership = True, "made up", "public"


async def _worse(session, role) -> None:
    await _lower_band(session, role)
    await _change_employer(session, role)


def _json(value):
    return json.loads(json.dumps(value, default=str))


#: What settling sets, and what settling again must leave as it is.
SETTLED = ("analysis", "fit", "fit_breakdown", "status", "screened_out_reason")


async def _stands(role_id) -> None:
    """Settling the role once more changes no value and writes no line: its ask, its score and its stage are the ones
    its facts give, and the score was worked out from the ask it holds."""
    first, lines = await _role(role_id), len(await _lines(role_id))

    again = await _settle(role_id)

    second = await _role(role_id)
    assert [_json(getattr(second, name)) for name in SETTLED] == [_json(getattr(first, name)) for name in SETTLED]
    assert len(await _lines(role_id)) == lines
    assert (again.roles, again.scores_moved, dict(again.gated)) == (1, 0, {})


@pytest.mark.parametrize(("change", "ask", "fit_moves_by", "gates_move"), [
    pytest.param(_correct_class, 80_000, 0, True, id="a required item's class: the ask falls under the floor"),
    pytest.param(_raise_band, 130_000, 1, False, id="the research band: the ask rises past a pay ask, still passing"),
    pytest.param(_lower_band, LOWER_ASK, 0, True, id="the research band: the ask falls under the floor"),
    pytest.param(_fill_place, 95_000, 0, True, id="the place and way of working: a town the owner does not work in"),
    pytest.param(_change_employer, 95_000, -2, True, id="an employer fact: gated, and no longer private"),
])
async def test_after_a_change_the_ask_the_score_and_the_gates_agree_and_one_line_says_why_the_gates_moved(
        js, change, ask, fit_moves_by, gates_move):
    role_id = (await _world("Fabrikam"))["Fabrikam"]
    before = await _role(role_id)
    assert before.analysis["ask"]["ask"] == 95_000, "the floor, to the dollar"
    assert before.status != stages.GATED_OUT and not await _lines(role_id, *GATE_LINES)

    done = await _settle(role_id, change)

    after = await _role(role_id)
    lines = await _lines(role_id, *GATE_LINES)
    assert after.analysis["ask"]["ask"] == ask, "the ask is the method's on the research the role holds"
    assert after.fit - before.fit == fit_moves_by, "the score reads the ask and the employer it holds now"
    assert len(lines) == (1 if gates_move else 0) and (after.status == stages.GATED_OUT) == gates_move
    assert all(f"{CAUSE}, so the gates were checked again." in line.detail for line in lines)
    assert done.roles == 1 and done.scores_moved == int(fit_moves_by != 0)
    assert set(done.gated) == ({role_id} if gates_move else set())
    assert all(said.strip()[:140] == line.sentence for said, line in zip(done.gated.values(), lines, strict=True)), (
        "what the door says is the line the history keeps, which the history cuts at 140 characters")
    await _stands(role_id)


async def test_a_second_settle_changes_no_value_and_writes_no_line(js):
    role_id = (await _world("Fabrikam"))["Fabrikam"]
    await _settle(role_id, _change_employer)

    await _stands(role_id)


async def test_no_roles_settle_nothing(js):
    async with db.session_factory()() as session:
        assert await settle.roles(session, [], cause=CAUSE) == settle.Settled(0, 0, {})


async def test_the_score_reads_the_employer_a_role_is_linked_to_and_never_one_its_company_name_matches(js):
    role_id = (await _world("Fabrikam"))["Fabrikam"]
    assert _earned(await _role(role_id), "ask-private") == 2, "the linked employer is private"
    async with db.session_factory()() as session:  # unlinked, though the company's name still names that record
        (await session.get(Opportunity, role_id)).employer_id = None
        await session.commit()
    async with db.session_factory()() as session:  # the whole-log pass, as a save of Prioritization runs it
        await prioritize.rescore(session, OWNER, await profile_store.current(session, OWNER))
        await session.commit()
    rescored = await _role(role_id)
    assert _earned(rescored, "ask-private") == 0, "a role with no link reads no employer"

    await _settle(role_id)

    settled = await _role(role_id)
    assert (settled.fit, _earned(settled, "ask-private")) == (rescored.fit, 0), "both passes read the same employer"


async def test_a_fractional_hourly_role_has_its_stored_yearly_ask_cleared_and_gets_none(js):
    ids = await _world("Fabrikam", "Initech")
    assert (await _role(ids["Fabrikam"])).analysis["ask"]["ask"] > 0

    async def fractional(session, role):
        role.title, role.comp = f"Fractional {role.title}", "$190-$630 an hour"

    await _settle(ids["Fabrikam"], fractional)
    role = await _role(ids["Fabrikam"])
    assert "ask" not in role.analysis and role.analysis["required"], "the ask goes; the research stays"
    await _stands(ids["Fabrikam"])
    await _settle(ids["Initech"], lambda session, r: _full_time_hourly(r))
    assert (await _role(ids["Initech"])).analysis["ask"]["ask"] > 0, "a full-time hourly role keeps its ask"


async def _full_time_hourly(role) -> None:
    role.comp = "$60-$80 an hour"


async def test_a_saved_change_to_the_owners_comp_settings_does_not_reach_an_ask_worked_out_from_the_same_research(js):
    ids = await _world("Fabrikam", "Initech")
    stored = (await _role(ids["Fabrikam"])).analysis["ask"]
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring={"asks": [*FIVE["asks"], ASK_OVER, PRIVATE]},
                           settings={**PROFILE["settings"], "comp_likely_high": 0.7})
        twin = await session.get(Opportunity, ids["Initech"])  # the same research, with no ask worked out yet
        twin.analysis = {k: v for k, v in twin.analysis.items() if k != "ask"}
        await session.commit()
    await _settle(ids["Initech"])
    assert (await _role(ids["Initech"])).analysis["ask"]["ask"] != stored["ask"], "the new setting moves a fresh ask"

    await _settle(ids["Fabrikam"])

    role = await _role(ids["Fabrikam"])
    assert role.analysis["ask"] == stored and role.status != stages.GATED_OUT


async def test_an_employer_change_moves_the_score_and_the_stop_but_leaves_a_stored_ask_standing(js):
    role_id = (await _world("Fabrikam"))["Fabrikam"]
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        role.analysis = {**role.analysis, "ask": {**role.analysis["ask"], "ask": 99_999}}
        await session.commit()
    before = await _role(role_id)

    done = await _settle(role_id, _change_employer)

    role = await _role(role_id)
    assert role.analysis["ask"]["ask"] == 99_999, "the same research and method: the ask stands"
    assert role.status == stages.GATED_OUT and role.fit != before.fit and done.scores_moved == 1


async def test_a_role_the_owner_moved_by_hand_has_its_ask_and_score_worked_out_and_keeps_its_stage(client):
    role_id = (await _world("Fabrikam"))["Fabrikam"]
    moved = await client.post(f"/api/v1/roles/{role_id}/move", json={"to": stages.FIRST_IMPRESSION})
    assert moved.status_code == 200
    before = await _role(role_id)

    done = await _settle(role_id, _worse)

    role = await _role(role_id)
    assert role.status == stages.FIRST_IMPRESSION and not await _lines(role_id, *GATE_LINES)
    assert role.analysis["ask"]["ask"] < 95_000 and role.fit != before.fit and done.scores_moved == 1
    assert dict(done.gated) == {}


async def test_a_role_past_ready_to_launch_has_its_ask_and_score_worked_out_and_keeps_its_stage(js):
    role_id = (await _world("Fabrikam"))["Fabrikam"]
    async with db.session_factory()() as session:
        (await session.get(Opportunity, role_id)).status = stages.APPLIED
        await session.commit()
    before = await _role(role_id)

    done = await _settle(role_id, _worse)

    role = await _role(role_id)
    assert role.status == stages.APPLIED and role.screened_out_reason is None
    assert role.analysis["ask"]["ask"] < 95_000 and role.fit != before.fit and done.scores_moved == 1
    assert not await _lines(role_id, *GATE_LINES)


@pytest.mark.parametrize("keep", [True, False], ids=["kept in Gated out", "gates may bring it back"])
async def test_a_gated_out_role_is_kept_there_on_request_and_its_ask_and_score_move_all_the_same(js, keep):
    role_id = (await _world("Fabrikam", band=UNDER_THE_FLOOR))["Fabrikam"]
    stopped = await _role(role_id)
    assert stopped.status == stages.GATED_OUT

    done = await _settle(role_id, _raise_band, keep_gated_out=keep)

    role = await _role(role_id)
    assert role.analysis["ask"]["ask"] == 130_000 and role.fit == stopped.fit + 1, "its ask and score moved"
    if keep:
        assert (role.status, role.screened_out_reason) == (stages.GATED_OUT, stopped.screened_out_reason)
        assert not await _lines(role_id, "regate_released") and dict(done.gated) == {}
    else:
        assert role.status != stages.GATED_OUT and role.screened_out_reason is None
        assert len(await _lines(role_id, "regate_released")) == 1 and set(done.gated) == {role_id}


async def _added(**more) -> uuid.UUID:
    """A role of its own that research has not touched, at Second Impression."""
    async with db.session_factory()() as session:
        role = Opportunity(owner=OWNER, slug="globex", company="Globex Corp", title="Programs Lead", origin="scout",
                           match_key="globex corp|programs lead", status=stages.SECOND_IMPRESSION,
                           found=datetime.now(UTC).date(), **{"location": "Remote, US", "move_type": "remote", **more})
        session.add(role)
        await session.commit()
        return role.id


async def test_a_role_with_no_research_is_gated_but_never_asked_or_scored(js):
    await analysis_setup()
    role_id = await _added(location="Faraway, TX", move_type="onsite")

    done = await _settle(role_id)

    role = await _role(role_id)
    assert role.status == stages.GATED_OUT and set(done.gated) == {role_id}
    assert (role.analysis, role.fit, role.fit_breakdown) == ({}, None, {}), "no ask, no score"


async def test_a_researched_role_with_no_answers_and_no_fit_is_scored_all_the_same(js):
    await analysis_setup()
    research = {"pay": {"band": OVER_THE_FLOOR}, "required": [{"item": "Programs", "class": "P"}]}
    role_id = await _added(analysis_state=analysis.DONE, analysis=research)

    await _settle(role_id)

    role = await _role(role_id)
    assert role.analysis["ask"]["ask"] >= 150_000 and role.fit is not None, "worked out, though nobody answered"
    assert scoring.is_asks(role.fit_breakdown) and role.fit_breakdown["answers"] == []


async def test_a_role_neither_researched_nor_scored_keeps_its_answers_and_gets_no_score(js):
    await analysis_setup()
    answers = {"answers": [{"id": "ask-a", "answer": "met", "evidence": "made up", "says": None, "by": "reader"}]}
    role_id = await _added(fit_breakdown=answers)

    await _settle(role_id)

    role = await _role(role_id)
    assert role.fit is None and role.fit_breakdown == answers, "a role waits for its research to be scored"


async def test_roles_loaded_without_their_research_settle_with_no_refresh_by_the_caller(js):
    role_id = (await _world("Fabrikam"))["Fabrikam"]
    async with db.session_factory()() as session:  # the research now holds a lower band; nothing has worked it out yet
        await _lower_band(session, await session.get(Opportunity, role_id))
        await session.commit()
    async with db.session_factory()() as session:
        loaded = (await session.scalars(select(Opportunity).where(Opportunity.id == role_id)
                                        .options(defer(Opportunity.analysis)))).one()
        assert "analysis" in sa_inspect(loaded).unloaded
        done = await settle.roles(session, [loaded], cause=CAUSE)
        await session.commit()
    role = await _role(role_id)
    assert role.status == stages.GATED_OUT and set(done.gated) == {role_id}, "the research was read for the gate"
    assert role.analysis["ask"]["ask"] < 95_000


async def test_research_that_cannot_be_read_keeps_its_ask_and_says_so_and_the_next_role_settles(js, caplog):
    ids = await _world("Fabrikam", "Initech")
    async with db.session_factory()() as session:
        broken = await session.get(Opportunity, ids["Fabrikam"])
        stored = broken.analysis["ask"]
        broken.analysis = {**broken.analysis, "required": [*broken.analysis["required"], {"class": "P"}]}
        await _fill_place(session, broken)
        other = await session.get(Opportunity, ids["Initech"])
        await _lower_band(session, other)
        with caplog.at_level(logging.WARNING):
            done = await settle.roles(session, [broken, other], cause=CAUSE)
        await session.commit()
    assert str(ids["Fabrikam"]) in caplog.text, "the log names the role whose ask was left"
    first, second = await _role(ids["Fabrikam"]), await _role(ids["Initech"])
    assert first.analysis["ask"] == stored, "the ask it had stands"
    assert first.status == stages.GATED_OUT, "its gates still ran, on the place it holds"
    assert second.status == stages.GATED_OUT and second.analysis["ask"]["ask"] < 95_000, "the next role settled"
    assert done.roles == 2 and set(done.gated) == {ids["Fabrikam"], ids["Initech"]}


async def test_score_data_that_cannot_be_read_keeps_the_score_and_says_so_and_the_next_role_settles(js, caplog):
    ids = await _world("Fabrikam", "Initech")
    async with db.session_factory()() as session:
        broken = await session.get(Opportunity, ids["Fabrikam"])
        kept = (broken.fit, broken.fit_breakdown)
        # an answer kept under an id that is not a name: only the score reads the answers
        broken.fit_breakdown = {"answers": [{"id": ["ask-a"], "answer": "met", "evidence": "made up"}]}
        kept = (broken.fit, broken.fit_breakdown)
        await _fill_place(session, broken)
        other = await session.get(Opportunity, ids["Initech"])
        await _lower_band(session, other)
        with caplog.at_level(logging.WARNING):
            done = await settle.roles(session, [broken, other], cause=CAUSE)
        await session.commit()
    assert str(ids["Fabrikam"]) in caplog.text, "the log names the role whose score was left"
    first, second = await _role(ids["Fabrikam"]), await _role(ids["Initech"])
    assert (first.fit, first.fit_breakdown) == kept, "the score it had stands"
    assert first.status == stages.GATED_OUT, "its gates still ran, on the place it holds"
    assert second.status == stages.GATED_OUT and second.analysis["ask"]["ask"] < 95_000, "the next role settled"
    assert done.roles == 2 and set(done.gated) == {ids["Fabrikam"], ids["Initech"]}


async def _under_an_older_method(role_id, research=None) -> None:
    """Leave a role as if its ask were worked out by the method before this one, and its research were `research`."""
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        role.comp, role.comp_source = None, None  # no posted pay: the gate judges the worked ask
        role.analysis = {**(research or role.analysis),
                         "ask": {**role.analysis["ask"], "method_version": comp.METHOD_VERSION - 1}}
        await session.commit()


async def test_an_ask_a_newer_method_moves_under_the_pay_floor_gates_the_role_out_at_start(js):
    role_id = (await _world("Fabrikam", band=OVER_THE_FLOOR))["Fabrikam"]
    async with db.session_factory()() as session:
        research = _research(await session.get(Opportunity, role_id), band=UNDER_THE_FLOOR)
    await _under_an_older_method(role_id, research)
    assert (await _role(role_id)).status != stages.GATED_OUT, "it passed the gates on the ask the old method gave"

    async with db.session_factory()() as session:
        assert await settle.old_asks(session, OWNER) == 1
        await session.commit()

    role = await _role(role_id)
    assert role.analysis["ask"]["ask"] < 95_000
    assert role.status == stages.GATED_OUT and role.screened_out_reason.startswith("Pay must be at least $95,000")
    assert len(await _lines(role_id, "regate_stopped")) == 1
    await _stands(role_id)


async def test_an_ask_a_newer_method_moves_over_the_pay_floor_brings_a_role_stopped_only_on_it_back(js):
    role_id = (await _world("Fabrikam", band=UNDER_THE_FLOOR))["Fabrikam"]
    async with db.session_factory()() as session:
        research = _research(await session.get(Opportunity, role_id), band=OVER_THE_FLOOR)
    await _under_an_older_method(role_id, research)
    assert (await _role(role_id)).status == stages.GATED_OUT, "stopped on the pay the old method's ask gave"

    async with db.session_factory()() as session:
        assert await settle.old_asks(session, OWNER) == 1
        await session.commit()

    role = await _role(role_id)
    assert role.analysis["ask"]["ask"] > 95_000
    assert role.status != stages.GATED_OUT and role.screened_out_reason is None
    assert len(await _lines(role_id, "regate_released")) == 1
    await _stands(role_id)


async def test_a_start_with_no_old_ask_reads_a_list_and_writes_nothing(js):
    role_id = (await _world("Fabrikam"))["Fabrikam"]
    before, lines = await _role(role_id), len(await _lines(role_id))

    written: list = []

    def note_writes(session, context, instances) -> None:
        written.extend([*session.new, *session.deleted, *(o for o in session.dirty if session.is_modified(o))])

    async with db.session_factory()() as session:
        event.listen(session.sync_session, "before_flush", note_writes)
        assert await settle.old_asks(session, OWNER) == 0
        await session.commit()

    assert written == [], "nothing was added, changed or deleted through the session"
    after = await _role(role_id)
    assert (after.analysis, after.fit, after.fit_breakdown) == (before.analysis, before.fit, before.fit_breakdown)
    assert len(await _lines(role_id)) == lines


async def test_an_old_ask_that_comes_out_the_same_is_stamped_again_and_nothing_else_is_written(js):
    role_id = (await _world("Fabrikam"))["Fabrikam"]
    await _under_an_older_method(role_id)
    before, lines = await _role(role_id), len(await _lines(role_id))

    async with db.session_factory()() as session:
        assert await settle.old_asks(session, OWNER) == 1, "it counts the roles it worked an ask for"
        await session.commit()

    after = await _role(role_id)
    assert after.analysis["ask"]["method_version"] == comp.METHOD_VERSION
    assert after.analysis["ask"]["ask"] == before.analysis["ask"]["ask"]
    assert (after.fit, after.fit_breakdown, after.status) == (before.fit, before.fit_breakdown, before.status)
    assert len(await _lines(role_id)) == lines, "no score, no gate line"
    async with db.session_factory()() as session:
        assert await settle.old_asks(session, OWNER) == 0, "safe to run again"


async def test_old_asks_reads_without_the_turn_and_works_the_asks_out_a_batch_at_a_time_as_one_transaction_does(
        js, monkeypatch):
    """On a large database the start's step "old research asks" held the writer's turn for too long, reading every
    researched role to find the old ones. Which are old is read in a reader's session, and the old ones are worked out
    `BATCH` at a time with the turn let go between: a second writer gets through before the first write and between
    batches. The asks, scores and stages written are those of the run inside a savepoint (one transaction)."""
    world = await _world("Fabrikam", "Initech", band=OVER_THE_FLOOR)
    ids = {name: world[name] for name in ("Fabrikam", "Initech")}
    for role_id in ids.values():
        async with db.session_factory()() as session:
            research = _research(await session.get(Opportunity, role_id), band=UNDER_THE_FLOOR)
        await _under_an_older_method(role_id, research)
    columns = ("analysis", "fit", "fit_breakdown", "status", "screened_out_reason")

    async def snapshot() -> dict:
        return {name: {c: _json(getattr(await _role(i), c)) for c in columns} for name, i in ids.items()}

    async def gate_lines() -> int:
        return sum([len(await _lines(i, *GATE_LINES)) for i in ids.values()])

    old = await snapshot()
    monkeypatch.setattr(settle, "BATCH", 1)
    free: list[bool] = []
    real = {"current": profile_store.current, "release": db.release_if_free}

    async def probe() -> None:
        async with db.session_factory()() as other:  # a second writer, asking while the pass holds nothing
            try:
                await asyncio.wait_for(other.execute(update(Opportunity).where(Opportunity.slug == "none")
                                                     .values(slug="none")), 1)
                free.append(True)
            except TimeoutError:
                free.append(False)
            await other.rollback()

    async def current(session, owner):
        if not free:  # the first read in the writer's session, after the old roles were found
            await probe()
        return await real["current"](session, owner)

    async def release(session):
        done = await real["release"](session)
        await probe()
        return done

    monkeypatch.setattr(profile_store, "current", current)
    monkeypatch.setattr(db, "release_if_free", release)
    async with db.session_factory()() as session:
        assert await settle.old_asks(session, OWNER) == 2
        await session.commit()
    monkeypatch.setattr(profile_store, "current", real["current"])
    monkeypatch.setattr(db, "release_if_free", real["release"])
    assert free == [True, True], "once before the first write, once between the two batches of one role"
    batched, lines = await snapshot(), await gate_lines()
    assert batched != old and lines == 2
    assert all(r["status"] == stages.GATED_OUT for r in batched.values()), "both asks came out under the floor"

    async with db.session_factory()() as session:  # the same roles, old again, worked out in one savepoint
        for name, role_id in ids.items():
            await session.execute(update(Opportunity).where(Opportunity.id == role_id).values(**old[name]))
        await session.commit()
    async with db.session_factory()() as session:
        async with session.begin_nested():
            assert await settle.old_asks(session, OWNER) == 2
        await session.commit()
    assert await snapshot() == batched and await gate_lines() == 2 * lines


async def test_research_that_stops_a_role_before_ready_to_launch_writes_the_gates_line_and_the_analysed_line(js):
    ids = await analysis_setup()
    await put_in_line(ids["Fabrikam"])

    reply = await submit_findings(findings(ids["Fabrikam"], pay={"band": UNDER_THE_FLOOR}))

    assert reply["taken"] is True and reply["stage"] == stages.GATED_OUT
    stopped = await _lines(ids["Fabrikam"], "regate_stopped")
    assert len(stopped) == 1 and stopped[0].detail.startswith("The analyst researched it, so the gates were checked")
    analysed = await _lines(ids["Fabrikam"], "analysed")
    assert len(analysed) == 1 and "it failed a gate on the facts: Pay must be at least $95,000" in analysed[0].sentence
    role = await _role(ids["Fabrikam"])
    assert role.analysis["ask"]["ask"] < 95_000 and role.fit is not None, "the ask and the score were worked out first"
    await _stands(ids["Fabrikam"])


#: Asks none of which can earn a point, so a role holding a fit on another scale is not scored on them unless the
#: analyst answers them.
NOTHING_TO_EARN = {"asks": [{"id": "ask-bad", "section": "posting", "says": "A bad sign", "points": -1}]}


OLD_FIT = {"base": 3, "total": 4}


@pytest.mark.parametrize("answers", [True, False], ids=["the analyst answers", "the analyst sends none"])
async def test_a_fit_on_another_scale_is_scored_on_the_asks_only_when_the_analyst_answers_them(js, answers):
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=NOTHING_TO_EARN)
        role = await session.get(Opportunity, ids["Fabrikam"])
        role.fit, role.fit_breakdown = 4, OLD_FIT
        await session.commit()
    await put_in_line(ids["Fabrikam"])
    body = findings(ids["Fabrikam"])
    if answers:
        body["fit_breakdown"] = {"asks": [{"id": "ask-bad", "answer": "met", "evidence": "the posting says so"}]}

    assert (await submit_findings(body))["taken"] is True

    role = await _role(ids["Fabrikam"])
    if answers:
        assert scoring.is_asks(role.fit_breakdown) and role.fit_breakdown["answers"][0]["by"] == "analyst"
    else:
        assert (role.fit, role.fit_breakdown) == (4, OLD_FIT), "kept, as the owner's asks cannot earn anything"


def _earned(role: Opportunity, ask_id: str) -> int:
    return sum(a.get("earned") or 0 for a in (role.fit_breakdown or {}).get("asks") or [] if a.get("id") == ask_id)
