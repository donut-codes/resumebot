"""What the quiet rules leave the classifier to learn from: the new
end reason `no_reply`, and a role that reached Applied or Active Discussion counts as pursued when it ends, whether the
stage move that says so goes into those stages or out of them (an imported role has only the move out). Synthetic roles
only: every title, employer and word here is made up."""

from datetime import date

import pytest
from sqlalchemy import delete, select

from app import db
from app.jobsearch import stages
from app.jobsearch.fit import classifier
from app.jobsearch.jobs import fits_none
from app.jobsearch.models import Opportunity, StageMove
from app.jobsearch.pipeline import ended
from tests.support import OWNER, load_profile


@pytest.fixture(autouse=True)
def fresh_cache():
    classifier.clear_cache()
    yield
    classifier.clear_cache()


def _role(n, title, **more):
    return Opportunity(owner=OWNER, slug=f"co-{n}", company=f"Co {n}", title=title, match_key=f"co {n}|{title.lower()}",
                       origin="scout", fit=3, found=date(2026, 10, 1), **{"status": "second_impression", **more})


async def _imported(session, n, title, status):
    """A role as the tracker import leaves it: in a stage, with no stage move at all."""
    role = _role(n, title, status=status)
    session.add(role)
    await session.flush()
    await session.execute(delete(StageMove).where(StageMove.role_id == role.id))
    await session.refresh(role)
    return role


async def _end(session, role, to, reason):
    """What a rule does to a role: the status and its reason on the object, then a flush (a stage move is written)."""
    role.status, role.ended_reason = to, reason
    await session.flush()


async def _learned(session):
    await load_profile(session, OWNER)
    examples = await classifier.examples(session, OWNER)
    return {text.split(".")[0]: (positive, weight) for text, positive, weight in examples}


async def _ids(session, where) -> set:
    return set(await session.scalars(select(Opportunity.id).where(*where)))


# --- the reason ---------------------------------------------------------------------------------------------------


def test_no_word_came_back_is_an_end_reason_before_unknown_and_the_owner_is_not_offered_it():
    assert stages.NO_REPLY == "no_reply"
    assert stages.END_REASONS.index("no_reply") == stages.END_REASONS.index("unknown") - 1
    assert stages.END_REASON_LABELS["no_reply"] == "No word came back"
    assert stages.END_REASON_PHRASES["no_reply"] == "no word came back"
    assert "no_reply" not in stages.OWNER_END_REASONS
    assert "no_reply" not in [o["reason"] for o in ended.offered()]
    assert ended.check("no_reply") == "no_reply"


# --- an imported role that goes quiet is still a pursued example -------------------------------------------------


@pytest.mark.parametrize("reason", ["no_reply", "posting_gone"])
@pytest.mark.parametrize("was", ["applied", "active_discussion"])
async def test_an_imported_role_that_goes_quiet_is_still_a_pursued_example_in_all_three_places(js, reason, was):
    async with db.session_factory()() as session:
        role = await _imported(session, 1, "Imported Role", was)
        assert await session.scalar(select(StageMove.id).where(StageMove.role_id == role.id)) is None
        await _end(session, role, stages.GONE_QUIET, reason)
        move = (await session.scalars(select(StageMove).where(StageMove.role_id == role.id))).one()
        assert (move.from_status, move.to_status) == (was, "gone_quiet")
        reached = await classifier.reached_set(session, OWNER)
        assert reached == {role.id}
        assert classifier.lesson(role, reached) == (classifier.PURSUED, True, classifier.MOVED_FORWARD_WEIGHT)
        assert await _learned(session) == {"Imported Role": (True, 2.0)}
        assert await _ids(session, fits_none.pursued_where(OWNER)) == {role.id}


async def test_reached_counts_a_move_out_of_the_stages_as_well_as_into_them_and_nothing_else(js):
    async with db.session_factory()() as session:
        into = _role(1, "Went In", status="applied")
        out = await _imported(session, 2, "Went Out", "active_discussion")
        early = _role(3, "Went Early", status="ready_to_launch")
        session.add_all([into, early])
        await session.flush()
        await _end(session, out, stages.GONE_QUIET, "unknown")
        await _end(session, early, stages.GONE_QUIET, "posting_gone")
        assert await classifier.reached_set(session, OWNER) == {into.id, out.id}
        assert set(await session.scalars(classifier.reached(OWNER))) == {into.id, out.id}, "the same rule, as SQL"
        other = await classifier.reached_set(session, "someone-else")
    assert other == set(), "one owner's history is not another's"


# --- a role that never reached Applied is no example when its posting went ------------------------------------


async def test_a_role_that_never_reached_applied_and_ends_posting_gone_is_no_example(js):
    async with db.session_factory()() as session:
        draft = _role(1, "Drafting Role", status="ready_to_launch")
        ready = _role(2, "Ready Role", status="ready_to_launch")
        session.add_all([draft, ready])
        await session.flush()
        before = await _learned(session)
        assert before == {"Drafting Role": (True, 2.0), "Ready Role": (True, 2.0)}, "in play, they are moved forward"
        await _end(session, draft, stages.GONE_QUIET, "posting_gone")
        await _end(session, ready, stages.GONE_QUIET, "posting_gone")
        after = await _learned(session)
        assert await _ids(session, fits_none.pursued_where(OWNER)) == set()
    assert after == {}, "a posting that came down says nothing about the job (31.7)"


async def test_a_role_that_went_applied_and_then_withdrew_with_i_passed_is_pursued(js):
    async with db.session_factory()() as session:
        role = _role(1, "Applied Then Passed", status="applied")
        session.add(role)
        await session.flush()
        await _end(session, role, stages.WITHDREW, "i_passed")
        assert await _learned(session) == {"Applied Then Passed": (True, 2.0)}
