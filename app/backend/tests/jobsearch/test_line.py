"""Where a role stands in the analyst's line and how it leaves it (`research/line.py`): the standing in Python and in
SQL say the same, each cause of leaving acts on the standings it names and clears what it names, and a role stopped
inside the process's own step keeps its instance. Every name is made up; the process engine is a spy, nothing is read
from the network."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.models import SOURCE_NEXT, Opportunity
from app.jobsearch.pipeline import quiet
from app.jobsearch.postings import noise
from app.jobsearch.research import line
from app.jobsearch.research.line import Standing
from app.jobsearch.work import stuck
from tests.support import DEFAULT_OWNER as OWNER

NOW = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
INSTANCE = uuid.uuid4()
KEYS = {line.SESSION_KEY: "s1", line.RUNNER_KEY: "r1", "claims": 2, "refused": 1, line.UNREAD_ASKED: True,
        "sent_back": 1, "posting_not_found": True}
GONE = "https://careers.example.test/jobs/1"


class _Session:
    """Only what `leave` asks of a session: a flush."""

    async def flush(self) -> None:
        return None


SESSION: object = _Session()


@pytest.fixture
def cancelled(monkeypatch) -> list[tuple[uuid.UUID, str]]:
    """The instances cancelled, with what each was told."""
    seen: list[tuple[uuid.UUID, str]] = []

    async def cancel(_session, instance_id, why, what):
        seen.append((instance_id, why))
        return True

    monkeypatch.setattr(stuck, "cancel_instance", cancel)
    return seen


def _role(standing: Standing, *, ask: bool = False, status: str = stages.SECOND_IMPRESSION, **more) -> Opportunity:
    state = {Standing.OUT: None, Standing.FREE: line.WAITING, Standing.TAKEN: line.WAITING,
             Standing.NEEDS_TEXT: line.NO_TEXT, Standing.DONE: line.DONE, Standing.FAILED: line.FAILED}[standing]
    research = {**KEYS, **({line.ASKED_BY: line.ASKED_BY_OWNER} if ask else {})}
    return Opportunity(
        id=uuid.uuid4(), owner=OWNER, slug="a", company="Firm", title="Lead", status=status, analysis_state=state,
        analysis_claimed_at=NOW if standing is Standing.TAKEN else None, analysis_asked_at=NOW,
        analysis_instance_id=INSTANCE if state else None, analysis=research, **more)


async def _leave(role, cause, **kw) -> bool:
    return await line.leave(SESSION, role, cause, **kw)  # type: ignore[arg-type]


# --- the standing ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("standing", list(Standing))
def test_a_role_stands_where_its_state_and_claim_say(standing):
    assert Standing.of(_role(standing)) is standing


async def test_the_standing_in_sql_says_what_the_standing_in_python_says(js):
    made = {(s, ask): _role(s, ask=ask) for s in Standing for ask in (False, True)}
    for n, role in enumerate(made.values()):
        role.slug = f"r{n}"
        role.match_key = f"firm|lead {n}"
        role.found = role.posted = NOW.date()
    async with db.session_factory()() as session:
        session.add_all(made.values())
        await session.commit()
        for standing in Standing:
            for ask in (None, True, False):
                found = set(await session.scalars(select(Opportunity.id).where(
                    Standing.where(standing, owner_ask=ask))))
                expected = {r.id for (s, asked), r in made.items()
                            if s is standing and (ask is None or asked == ask)}
                assert found == expected, (standing, ask)
        both = set(await session.scalars(select(Opportunity.id).where(Standing.where(Standing.FREE, Standing.TAKEN))))
        assert both == {r.id for (s, _), r in made.items() if s in (Standing.FREE, Standing.TAKEN)}


# --- leaving: each cause acts on the standings it names -------------------------------------------------------------


ACTS_ON = {
    line.SWEEP: {Standing.FREE, Standing.TAKEN},
    line.OFF_DECK: {Standing.FREE},
    line.GATE_FAILED: {Standing.FREE, Standing.TAKEN, Standing.NEEDS_TEXT},
    line.NOT_SOURCED: {Standing.FREE, Standing.TAKEN, Standing.NEEDS_TEXT, Standing.FAILED},
    line.SEND_BACK_TEXT: set(Standing),
    line.ROLE_CLOSED: {Standing.FREE, Standing.TAKEN, Standing.NEEDS_TEXT, Standing.FAILED},
}


def test_every_cause_in_the_table_is_tested_here():
    assert set(ACTS_ON) == set(line.CAUSES)


@pytest.mark.parametrize("standing", list(Standing))
@pytest.mark.parametrize("cause", line.CAUSES, ids=lambda c: c.name)
async def test_a_cause_acts_on_the_standings_it_names_and_leaves_any_other_untouched(cause, standing, cancelled):
    role = _role(standing)
    before = (role.analysis_state, role.analysis_claimed_at, role.analysis_instance_id, dict(role.analysis))
    assert await _leave(role, cause) is (standing in ACTS_ON[cause])
    if standing not in ACTS_ON[cause]:
        assert (role.analysis_state, role.analysis_claimed_at, role.analysis_instance_id, role.analysis) == before
        assert cancelled == []


@pytest.mark.parametrize("standing", [Standing.FREE, Standing.TAKEN])
async def test_a_sweep_cancels_the_instance_and_empties_the_role_of_the_line(standing, cancelled):
    role = _role(standing)
    assert await _leave(role, line.SWEEP, why="the night was stopped")
    assert cancelled == [(INSTANCE, "the night was stopped")]
    assert (role.analysis_state, role.analysis_claimed_at, role.analysis_instance_id) == (None, None, None)
    assert not {line.SESSION_KEY, line.RUNNER_KEY, "claims"} & set(role.analysis)
    assert role.analysis["refused"] == 1 and role.analysis_asked_at == NOW, "only what it names is cleared"


async def test_a_sweep_leaves_the_owners_ask_in_line_and_a_gate_does_not_clear_it(cancelled):
    asked = _role(Standing.FREE, ask=True)
    assert not await _leave(asked, line.SWEEP)
    assert asked.analysis_state == line.WAITING and cancelled == []
    assert await _leave(asked, line.GATE_FAILED)
    assert asked.analysis.get(line.ASKED_BY) == line.ASKED_BY_OWNER, "a gate stopping a role keeps the owner's ask"


async def test_taking_a_role_off_the_deck_spares_the_stages_and_researched_roles_it_names(cancelled):
    spared = [_role(Standing.FREE, status=s) for s in
              (*stages.CLOSED, stages.APPLIED, stages.ACTIVE_DISCUSSION, stages.READY_TO_LAUNCH)]
    spared.append(_role(Standing.FREE, analysed_at=NOW))
    for role in spared:
        assert not await _leave(role, line.OFF_DECK)
    assert cancelled == []
    role = _role(Standing.FREE, status=stages.FIRST_IMPRESSION)
    assert await _leave(role, line.OFF_DECK)
    assert cancelled == [(INSTANCE, "it is not on deck")]
    assert role.analysis_asked_at is None, "its place in line goes with it"


async def test_a_gate_clears_the_refusals_and_a_role_waiting_for_text_goes_without_a_cancel(cancelled):
    role = _role(Standing.NEEDS_TEXT)
    assert await _leave(role, line.GATE_FAILED)
    assert cancelled == [] and role.analysis_instance_id is None and "refused" not in role.analysis
    assert role.analysis_state is None


async def test_not_sourced_keeps_the_refusals_and_a_role_inside_its_step_keeps_the_instance(cancelled):
    role = _role(Standing.FREE)
    assert await _leave(role, line.NOT_SOURCED, inside=True)
    assert cancelled == [] and role.analysis_instance_id == INSTANCE
    assert role.analysis["refused"] == 1 and line.SESSION_KEY not in role.analysis


async def test_a_role_sent_back_for_text_waits_with_its_instance_and_keeps_what_it_names(cancelled):
    role = _role(Standing.TAKEN)
    assert await _leave(role, line.SEND_BACK_TEXT)
    assert (role.analysis_state, role.analysis_claimed_at, role.analysis_instance_id) == (line.NO_TEXT, None, INSTANCE)
    assert cancelled == []
    assert not {line.SESSION_KEY, line.RUNNER_KEY, "claims", "refused", line.UNREAD_ASKED} & set(role.analysis)
    assert role.analysis["sent_back"] == 1 and role.analysis["posting_not_found"] is True


async def test_a_role_closed_leaves_every_standing_but_done_research(cancelled):
    done = _role(Standing.DONE)
    assert not await _leave(done, line.ROLE_CLOSED)
    assert done.analysis_state == line.DONE and done.analysis_instance_id == INSTANCE
    failed = _role(Standing.FAILED)
    assert await _leave(failed, line.ROLE_CLOSED)
    assert (failed.analysis_state, failed.analysis_instance_id) == (None, None) and cancelled == []


async def test_leaving_is_idempotent(cancelled):
    role = _role(Standing.FREE)
    assert await _leave(role, line.SWEEP)
    assert not await _leave(role, line.SWEEP)
    assert len(cancelled) == 1


async def test_a_role_left_inside_a_step_block_keeps_its_instance_and_is_not_cancelled(cancelled):
    role = _role(Standing.FREE)
    with line.in_step():
        assert await _leave(role, line.ROLE_CLOSED)
    assert cancelled == [] and role.analysis_instance_id == INSTANCE and role.analysis_state is None


async def test_many_roles_leave_together_and_the_ones_that_left_are_named(cancelled):
    roles = [_role(Standing.FREE), _role(Standing.FREE, ask=True), _role(Standing.DONE), _role(Standing.TAKEN)]
    left = await line.leave_many(SESSION, roles, line.SWEEP, why="limit")  # type: ignore[arg-type]
    assert left == [roles[0], roles[3]]
    assert [why for _, why in cancelled] == ["limit", "limit"]


# --- the marks --------------------------------------------------------------------------------------------------------


def test_a_role_is_read_next_or_has_its_source_looked_up_never_both():
    role = _role(Standing.OUT)
    role.analysis = {line.READ_NEXT: True}
    assert line.put_mark(role, line.Mark.SOURCE_NEXT)
    assert role.analysis == {SOURCE_NEXT: True}
    assert line.put_mark(role, line.Mark.READ_NEXT)
    assert role.analysis == {line.READ_NEXT: True}


def test_a_mark_to_look_a_source_up_is_refused_for_a_role_in_line_or_researched():
    for standing in (Standing.FREE, Standing.TAKEN, Standing.NEEDS_TEXT, Standing.DONE, Standing.FAILED):
        role = _role(standing)
        before = dict(role.analysis)
        assert not line.put_mark(role, line.Mark.SOURCE_NEXT) and role.analysis == before


def test_a_mark_to_read_again_keeps_the_marks_of_the_research_and_takes_the_look_up_off():
    role = _role(Standing.DONE)
    role.analysis = {SOURCE_NEXT: True, "deck_read": True}
    assert line.put_mark(role, line.Mark.REANALYSE)
    assert role.analysis == {"deck_read": True, line.REANALYSE: True}


# --- a role closed by a rule leaves the line, and its process with it ---------------------------------------------


def _waiting_world(**more) -> Opportunity:
    return Opportunity(
        owner=OWNER, slug="q", company="Firm Q", title="Lead Q", origin="scout", match_key="firm q|lead q",
        status=more.pop("status", stages.SECOND_IMPRESSION), found=NOW.date(), posted=NOW.date(),
        analysis_state=line.WAITING, analysis_claimed_at=NOW, analysis_instance_id=INSTANCE,
        analysis={line.SESSION_KEY: "s1", line.RUNNER_KEY: "r1", "claims": 1}, **more)


async def test_a_posting_that_is_gone_takes_a_waiting_role_out_of_line_cancels_its_analysis_and_clears_its_keys(
        js, cancelled):
    """Owner decision 2026-11-14: before, `end_gone` left the instance id and the session keys
    for the nightly orphan cleanup."""
    async with db.session_factory()() as session:
        role = _waiting_world()
        session.add(role)
        await session.flush()
        assert await quiet.end_gone(session, role, GONE, "404 Not Found")
        assert role.status == stages.GONE_QUIET
        assert (role.analysis_state, role.analysis_claimed_at, role.analysis_instance_id) == (None, None, None)
        assert not {line.SESSION_KEY, line.RUNNER_KEY, "claims"} & set(role.analysis)
    assert [i for i, _ in cancelled] == [INSTANCE]


async def test_a_posting_found_gone_inside_the_text_step_keeps_the_instance_for_the_engine(js, cancelled):
    async with db.session_factory()() as session:
        role = _waiting_world()
        session.add(role)
        await session.flush()
        with line.in_step():
            assert await quiet.end_gone(session, role, GONE, "404 Not Found")
        assert role.analysis_state is None and role.analysis_instance_id == INSTANCE
    assert cancelled == []


async def test_a_role_closed_as_noise_takes_a_waiting_role_out_of_line_and_cancels_its_analysis(js, cancelled):
    """Owner decision 2026-11-14, architecture review Q1."""
    async with db.session_factory()() as session:
        role = _waiting_world(status=stages.FOLLOWUP_NEEDED)
        session.add(role)
        await session.flush()
        assert await noise.close(session, role, "the page had no posting")
        assert role.status == stages.GONE_QUIET
        assert (role.analysis_state, role.analysis_instance_id) == (None, None)
        assert not {line.SESSION_KEY, line.RUNNER_KEY, "claims"} & set(role.analysis)
    assert [i for i, _ in cancelled] == [INSTANCE]


@pytest.mark.parametrize("state", [line.FAILED, line.NO_TEXT])
async def test_a_posting_that_is_gone_still_takes_a_failed_role_or_one_waiting_for_text_out_of_line(
        js, cancelled, state):
    async with db.session_factory()() as session:
        role = _waiting_world()
        role.analysis_state, role.analysis_claimed_at = state, None
        session.add(role)
        await session.flush()
        assert await quiet.end_gone(session, role, GONE, "404 Not Found")
        assert role.analysis_state is None
    assert cancelled == []


async def test_a_posting_that_is_gone_leaves_done_research_done(js, cancelled):
    async with db.session_factory()() as session:
        role = _waiting_world()
        role.analysis_state, role.analysis_claimed_at = line.DONE, None
        session.add(role)
        await session.flush()
        assert await quiet.end_gone(session, role, GONE, "404 Not Found")
        assert role.analysis_state == line.DONE
    assert cancelled == []
