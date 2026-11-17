"""The score the best-of queue sorts a read role by (`scoring.queue_percent`, `pipeline.score`): w x the graded
requirements' match, as a share so a long posting weighs no more than a short one, + (1 - w) x the share of the owner's
other asks with a known answer it earned. w is the owner's (`Scoring.match_weight`, half unless set). A role scored
without graded requirements is sorted by its percent as before. Everything here is made up."""

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.fit import scoring
from app.jobsearch.models import Opportunity
from app.jobsearch.pipeline import deck as pipeline
from app.jobsearch.profile import schema as p
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.scoring import MATCH_WEIGHT_DEFAULT, Scoring
from app.timeutil import owner_today
from tests.builders import counted
from tests.support import GRADED_ON, OWNER, PROFILE, load_profile

TODAY = owner_today()
#: The made-up profile's asks and a second reader's ask, so one ask can be known while the other is not.
GRADED_SCORING = {**PROFILE["scoring"], "grade_requirements": GRADED_ON,
                  "asks": [*PROFILE["scoring"]["asks"],
                           {"id": "remote", "section": "posting", "says": "Fully remote", "points": 1}]}
GRADED = p.Profile.model_validate({**PROFILE, "scoring": GRADED_SCORING})
_SPAN = scoring.bounds(GRADED)
assert _SPAN is not None
SPAN = _SPAN


def _at(weight: float) -> scoring.Span:
    """The made-up profile's span with the owner's weight set to `weight`."""
    return scoring.Span(SPAN[0], SPAN[1], SPAN.legacy, SPAN.by_role, weight)


def _breakdown(classes: str, promotion: str = "not", remote: str = "not") -> dict:
    """A real breakdown (`scoring.compute`) for required items of these classes (`"PPU"`) and the two reader's asks."""
    out = scoring.compute({"asks": [{"id": "promotion", "answer": promotion, "evidence": "made up"},
                                    {"id": "remote", "answer": remote, "evidence": "made up"}]}, GRADED,
                          classes=list(classes))
    assert out is not None
    return out


def _role(slug: str, classes: str, promotion: str = "not", status: str = stages.SECOND_IMPRESSION,
          remote: str = "not") -> Opportunity:
    bd = _breakdown(classes, promotion, remote)
    return Opportunity(id=uuid.uuid4(), owner=OWNER, slug=slug, company=f"Firm {slug}", title=f"Programs Lead {slug}",
                       origin="scout", match_key=f"firm {slug}|programs lead {slug}", status=status, found=TODAY,
                       posted=TODAY,
                       fit=bd["total"], fit_breakdown=bd, **counted())


def test_match_is_the_proven_share_of_the_required_items_and_a_transferable_one_is_not_met():
    assert scoring.match_share({"required": {"proven": 3, "transferable": 0, "unproven": 0}}) == 1.0
    assert scoring.match_share({"required": {"proven": 0, "transferable": 0, "unproven": 2}}) == 0.0
    assert scoring.match_share({"required": {"proven": 1, "transferable": 1, "unproven": 0}}) == 0.5, (
        "a Transferable item counts as not met")
    assert scoring.match_share({"required": {"proven": 1, "transferable": 0, "unproven": 0},
                                "responsibilities": {"proven": 0, "transferable": 0, "unproven": 1}}) == 0.5, (
        "the duties count with the required items")
    assert scoring.match_share({"required": {}}) is None, "nothing classed: no match to measure"


def test_match_counts_the_preferred_items_proven_only_and_n_is_in_no_count():
    # old behaviour: the Proven share of the required items and duties alone (PRD 24.15); new (2026-11-12): the
    # preferred items count with them, a Transferable or Unproven one as not met. N never reaches a tally.
    tally = {"proven": 1, "transferable": 1, "unproven": 0}
    liked = {"proven": 1, "transferable": 0, "unproven": 1}
    assert scoring.match_share({"required": tally, "preferred": liked}) == 0.5
    assert scoring.match_share({"required": tally, "card_preferred": liked, "preferred": None}) == 0.5
    assert scoring.match_share({"required": {}, "card_preferred": liked, "preferred": None}) == 0.5, (
        "a role with only preferred items classed has a match")
    assert scoring.match_share({"required": tally, "card_preferred": {}, "preferred": liked}) == 0.5, (
        "the card's own tally wins over the shown one")


def test_the_card_reads_the_preferred_items_with_the_preferred_section_off_and_the_fit_is_unchanged():
    off = p.Profile.model_validate({**PROFILE, "scoring": {**GRADED_SCORING, "sections_off": ["preferred"]}})
    assert not off.scoring.grades_preferred()
    plain = scoring.compute({"asks": []}, off, classes=["P", "P"])
    liked = scoring.compute({"asks": []}, off, classes=["P", "P"], preferred=["U", "U", "T"])
    assert plain is not None and liked is not None
    assert liked["percent"] == plain["percent"] and liked["earned"] == plain["earned"], "the fit counts none of them"
    grade = next(r for r in liked["asks"] if r.get("kind") == "grade")
    assert grade["preferred"] is None, "the owner's breakdown still shows no preferred tally"
    assert grade["card_preferred"] == {"proven": 0, "transferable": 1, "unproven": 2}
    assert scoring.match_share(grade) == 0.4


def test_a_role_with_only_preferred_items_classed_is_ranked_by_its_match():
    bd = scoring.compute({"asks": []}, GRADED, classes=[], preferred=["P", "U"])
    assert bd is not None
    role = Opportunity(fit=bd["total"], fit_breakdown=bd)
    assert scoring.queue_percent(role, SPAN) is not None
    assert scoring.match_share(next(r for r in bd["asks"] if r.get("kind") == "grade")) == 0.5


def test_a_transferable_item_ranks_below_a_proven_one():
    assert scoring.queue_percent(_role("pp", "PP"), SPAN) > scoring.queue_percent(_role("pt", "PT"), SPAN)


def test_a_role_with_the_higher_match_outranks_one_with_the_lower_match_all_else_equal():
    better, worse = _role("better", "PPPU"), _role("worse", "PPUU")
    assert scoring.queue_percent(better, SPAN) > scoring.queue_percent(worse, SPAN)
    now = datetime.now(UTC)
    got = pipeline.order([worse, better], {}, TODAY, now, None, SPAN, {better.id, worse.id})
    assert [r.slug for r in got] == ["better", "worse"]


def test_a_longer_posting_with_the_same_match_scores_the_same():
    short, long = _role("short", "PPU", "met"), _role("long", "PPPPPPUUU", "met")
    assert scoring.percent_of(short, SPAN) != scoring.percent_of(long, SPAN), "the percent moves with the item count"
    assert scoring.queue_percent(short, SPAN) == scoring.queue_percent(long, SPAN)


def test_at_equal_asks_the_match_decides_and_at_equal_match_the_asks_do():
    asks_only, matched = _role("asks", "PUU", "met"), _role("matched", "PPPPU", "met")
    assert scoring.queue_percent(matched, SPAN) > scoring.queue_percent(asks_only, SPAN)
    same_match_met, same_match_not = _role("met", "PPU", "met"), _role("not", "PPU", "not")
    assert scoring.queue_percent(same_match_met, SPAN) > scoring.queue_percent(same_match_not, SPAN), \
        "the owner's asks still raise a role among equal matches"


def test_the_owners_weight_splits_match_and_the_other_asks():
    role = _role("r", "PPPU", "met")  # 3 of 4 Proven; of the two known asks, promotion met: 1 of 2
    assert SPAN.match_weight == MATCH_WEIGHT_DEFAULT == 0.5, "half unless the owner says"
    assert scoring.queue_percent(role, SPAN) == 63, "0.5 x 0.75 + 0.5 x 0.5 = 0.625, half up"
    assert scoring.queue_percent(role, _at(0.8)) == 70, "0.8 x 0.75 + 0.2 x 0.5"
    assert scoring.queue_percent(role, _at(1.0)) == 75, "all match"
    assert scoring.queue_percent(role, _at(0.0)) == 50, "all asks"
    assert scoring.queue_percent(role, (SPAN[0], SPAN[1])) == 63, "a plain pair carries no weight: half"


def test_an_ask_nobody_could_tell_is_left_out_of_the_asks_share():
    known_miss, unknown = _role("miss", "PPPU", "met", remote="not"), _role("unk", "PPPU", "met", remote="unknown")
    assert scoring.queue_percent(known_miss, SPAN) == 63, "promotion met, remote not met: 1 of 2"
    assert scoring.queue_percent(unknown, SPAN) == 88, "remote unknown: out of both, so 1 of 1 (0.5 x 0.75 + 0.5)"
    nothing_known = _role("none", "PPPU", "unknown", remote="unknown")
    assert scoring.queue_percent(nothing_known, SPAN) == 75, "no known ask: the match alone"


def test_the_asks_share_is_earned_over_the_decided_reach_and_a_penalty_takes_it_down():
    grade = {"kind": "grade", "points": 8, "earned": 4}
    assert scoring.asks_share({"decided": 12, "earned": 6, "possible": 14}, grade) == 0.5, "(6-4) / (12-8)"
    assert scoring.asks_share({"decided": 12, "earned": 3, "possible": 14}, grade) == 0.0, "a met penalty: held at 0"
    assert scoring.asks_share({"decided": 8, "earned": 4, "possible": 14}, grade) is None, "no decided ask can earn"


def test_match_weight_is_the_owners_setting_between_0_and_1():
    assert Scoring().match_weight == 0.5, "a saved version without it reads as half"
    assert Scoring.model_validate({"match_weight": 0.3}).match_weight == 0.3
    for bad in (-0.1, 1.5, "lots"):
        with pytest.raises(ValidationError):
            Scoring.model_validate({"match_weight": bad})
    weighted = p.Profile.model_validate({**PROFILE, "scoring": {**GRADED_SCORING, "match_weight": 0.8}})
    span = scoring.bounds(weighted)
    assert span is not None and span.match_weight == 0.8, "the queue reads it from the span"


def test_a_role_without_graded_requirements_is_sorted_by_its_percent():
    plain = Opportunity(fit=80, fit_breakdown={"math": 3, "percent": 80, "asks": []})
    assert scoring.queue_percent(plain, SPAN) == 80
    assert scoring.queue_percent(Opportunity(fit=None), SPAN) is None


async def test_the_higher_match_takes_ready_to_launch(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=GRADED_SCORING, settings={**PROFILE["settings"], "apply_per_day": 1})
        session.add_all([_role("worse", "PPUU", "met"), _role("better", "PPPPU", "met")])
        await session.commit()
    async with db.session_factory()() as session:
        await pipeline.rebalance(session, OWNER)
        await session.commit()
    async with db.session_factory()() as session:
        got = {r.slug: r.status for r in await session.scalars(select(Opportunity))}
    assert got == {"better": stages.READY_TO_LAUNCH, "worse": stages.SECOND_IMPRESSION}


async def test_saving_a_new_weight_keeps_it(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=GRADED_SCORING)
        await session.commit()
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "scoring", {**GRADED_SCORING, "match_weight": 0.7}, by="test")
        await session.commit()
    async with db.session_factory()() as session:
        assert (await profile_store.current(session, OWNER)).scoring.match_weight == 0.7
        with pytest.raises(profile_store.ProfileError):
            await profile_store.save(session, OWNER, "scoring", {**GRADED_SCORING, "match_weight": 2}, by="test")
