"""The rounds of a turn (`deck._stages`): a round is followed by another only when what it moved can make something else
movable. The turn after a rank report ran two rounds and the second moved nothing, which was a large share of
the loop's time in a wave on a large database.

`advance` is the first step of a round: every later step of that round saw the roles it placed, and a role it placed is
placed by `stage_of` of its facts, which a second `advance` finds already true. The roles the employer limit holds leave
the deck, which no earlier step reads. So a round whose only moves are those is the last. Every other move, and a role
the limit returns to First Impression (the stars' step `favorites` runs before the limit), is followed by a round.
Everything is made up."""

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.models import Opportunity
from app.jobsearch.pipeline import deck as pipeline
from app.jobsearch.pipeline import employer_limit
from app.timeutil import owner_today
from tests.builders import rank_roles
from tests.support import OWNER, PROFILE, load_profile

#: The steps of a round, in the order a round runs them.
STEPS = ("advance", "gate", "employers_for_deck", "favorites", "mark_for_reads", "employer_limit", "rebalance")


def _scripted(monkeypatch, **answers) -> dict[str, int]:
    """Each step of a round replaced by one that answers what `answers` says for its n-th call (a count;
    `employer_limit` a `(held, returned)` pair, `rebalance` a `(ready, deck)` pair) and then nothing. Returns how often
    each was called."""
    calls = dict.fromkeys(STEPS, 0)

    def step(name: str, nothing):
        async def run(*_args, **_kwargs):
            n = calls[name]
            calls[name] += 1
            said = answers.get(name, [])
            return said[n] if n < len(said) else nothing

        return run

    async def settle(*_args, **_kwargs):
        held, returned = await step("employer_limit", (0, 0))()
        return employer_limit.Settled(held=held, returned=returned)

    monkeypatch.setattr(pipeline, "advance", step("advance", 0))
    monkeypatch.setattr(pipeline, "gate", step("gate", 0))
    monkeypatch.setattr(pipeline, "employers_for_deck", step("employers_for_deck", 0))
    monkeypatch.setattr(pipeline.favorites, "advance", step("favorites", 0))
    monkeypatch.setattr(pipeline, "mark_for_reads", step("mark_for_reads", 0))
    monkeypatch.setattr(employer_limit, "settle", settle)
    monkeypatch.setattr(pipeline, "rebalance", step("rebalance", (0, 0)))
    return calls


def _rounds(calls: dict[str, int]) -> int:
    assert len(set(calls.values())) == 1, f"the steps of a round run together: {calls}"
    return calls["advance"]


@pytest.mark.parametrize("moves", [
    {"advance": [3]},
    {"employer_limit": [(2, 0)]},
    {"advance": [1], "employer_limit": [(1, 0)]},
], ids=["roles placed", "roles held by the employer limit", "both"])
async def test_a_round_that_only_placed_roles_or_held_them_is_the_last(js, monkeypatch, moves):
    calls = _scripted(monkeypatch, **moves)
    with pipeline.accounting() as account:
        done = await pipeline.run_in_steps(OWNER)
    assert _rounds(calls) == 1
    assert account["more"] is False, "nothing is owed to a next turn"
    assert done["advanced"] == sum(moves.get("advance", [])) and done["held"] == sum(
        sum(pair) for pair in moves.get("employer_limit", []))


@pytest.mark.parametrize("step, said", [
    ("gate", 1), ("employers_for_deck", 1), ("favorites", 1), ("mark_for_reads", 1),
    ("employer_limit", (0, 1)), ("rebalance", (1, 0)), ("rebalance", (0, 2)),
], ids=["gate", "employers", "stars", "marks", "a role the limit returns", "ready", "deck"])
async def test_a_round_that_moved_anything_else_is_followed_by_another(js, monkeypatch, step, said):
    calls = _scripted(monkeypatch, advance=[2], **{step: [said]})
    with pipeline.accounting() as account:
        await pipeline.run_in_steps(OWNER)
    assert _rounds(calls) == 2, "the round that moved it, and the one that sees nothing move"
    assert account["more"] is False


async def test_a_turn_still_stops_at_its_last_round_when_the_roles_keep_moving(js, monkeypatch):
    calls = _scripted(monkeypatch, gate=[1] * 9)
    with pipeline.accounting() as account:
        await pipeline.run_in_steps(OWNER)
    assert _rounds(calls) == pipeline.ROUNDS
    assert account["more"] is True, "the next round is owed to the next turn"


async def test_a_ranked_role_is_in_first_impression_after_the_turns_first_round(js, monkeypatch):
    """A role the report ranked is placed by the first round, and a turn run again after it finds nothing to do. (The
    deck is full and settled, so the new role, below the weakest on it, moves nothing else.)"""
    day = owner_today()

    def role(slug: str, status: str) -> Opportunity:
        return Opportunity(owner=OWNER, slug=slug, company=f"Firm {slug}", title=f"Programs Lead {slug}",
                           origin="scout", match_key=f"firm {slug}|programs lead {slug}", status=status, found=day,
                           posted=day)

    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={**PROFILE["settings"], "apply_per_day": 2})
        deck = [role(slug, stages.FIRST_IMPRESSION) for slug in "abc"]
        session.add_all(deck)
        await session.flush()
        await rank_roles(session, dict(zip((r.id for r in deck), (90, 80, 70), strict=True)))
        await session.commit()
    for _ in range(4):
        settled = await pipeline.run_in_steps(OWNER)
        if not any(settled.values()):
            break
    assert not any(settled.values()), "the deck was settled"
    async with db.session_factory()() as session:
        new = role("new", stages.FOLLOWUP_NEEDED)
        session.add(new)
        await session.flush()
        await rank_roles(session, {new.id: 40})
        await session.commit()
    rounds = []
    real = pipeline.advance

    async def counted(session, owner):
        rounds.append(1)
        return await real(session, owner)

    monkeypatch.setattr(pipeline, "advance", counted)
    first = await pipeline.run_in_steps(OWNER)
    async with db.session_factory()() as session:
        status = await session.scalar(select(Opportunity.status).where(Opportunity.slug == "new"))
    assert status == stages.FIRST_IMPRESSION and {k: v for k, v in first.items() if v} == {"advanced": 1}
    assert len(rounds) == 1, "the turn did not run a second round to see that nothing moved"
    again = await pipeline.run_in_steps(OWNER)
    assert not any(again.values()), f"nothing was left for the next turn: {again}"
