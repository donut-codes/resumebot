"""A Launch skip reason (`go_skip`, "Launch in Browser skips it: ...") is cleared when it stops being true: a packet
that turns ready clears a packet-failure reason (not an employer-gap or no-apply-link one), a role that leaves Ready to
Launch loses it whatever it was, and `clear-stale-go-skips` repairs the ones already stored. Everything is made up."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import cli, stages
from app.jobsearch.models import Opportunity
from app.jobsearch.writing import packets
from tests.jobsearch.test_go_checks import _add, _packet, _roles, role
from tests.support import OWNER

FAILED = "The packet could not be built: the writer did not finish it. Ask for it from its page."
GAP = "Applied to Firm a 3 days ago."
NO_LINK = "It has no apply link."


async def _ready(slug: str) -> None:
    async with db.session_factory()() as session:
        found = next(r for r in (await session.scalars(select(Opportunity))).all() if r.slug == slug)
        from app.jobsearch.models import Packet
        packet = (await session.scalars(select(Packet).where(Packet.role_id == found.id))).first()
        await packets.action_ready(session, None, {"packet": str(packet.id)})
        await session.commit()


@pytest.mark.parametrize("why", [FAILED, "Its packet wasn't finished in time."])
async def test_a_packet_turning_ready_clears_a_packet_failure_skip(js, why):
    await _add(role("a", fit=9, go_skip=why))
    await _packet("a", "writing")
    await _ready("a")
    assert (await _roles())["a"].go_skip is None


@pytest.mark.parametrize("why", [GAP, NO_LINK])
async def test_a_packet_turning_ready_keeps_a_reason_that_is_still_true(js, why):
    await _add(role("a", fit=9, go_skip=why))
    await _packet("a", "writing")
    await _ready("a")
    assert (await _roles())["a"].go_skip == why


@pytest.mark.parametrize("to", [stages.APPLIED, stages.GATED_OUT, stages.GONE_QUIET])
async def test_a_role_that_leaves_ready_to_launch_loses_its_skip(js, to):
    await _add(role("a", fit=9, go_skip=GAP))
    async with db.session_factory()() as session:
        found = (await session.scalars(select(Opportunity))).one()
        found.status = to
        await session.commit()
    assert (await _roles())["a"].go_skip is None


async def test_a_role_still_in_ready_to_launch_keeps_a_skip_that_is_still_true(js):
    await _add(role("a", fit=9, go_skip=GAP))
    async with db.session_factory()() as session:
        found = (await session.scalars(select(Opportunity))).one()
        found.title = "Product Lead renamed"
        await session.commit()
    assert (await _roles())["a"].go_skip == GAP


async def test_the_command_clears_the_stale_ones_only_and_a_dry_run_changes_nothing(js, capsys, monkeypatch):
    monkeypatch.setattr(cli.settings, "owner", OWNER)
    await _add(role("left", fit=9, status=stages.GATED_OUT, go_skip=GAP), role("fixed", fit=8, go_skip=FAILED),
               role("true", fit=7, go_skip=GAP), role("failed", fit=6, go_skip=FAILED))
    await _packet("fixed", "ready")
    await _packet("failed", "failed")
    async with db.session_factory()() as session:  # written after the move: the hook clears on a move only
        found = next(r for r in (await session.scalars(select(Opportunity))).all() if r.slug == "left")
        found.go_skip = GAP
        await session.commit()
    await cli._clear_stale_go_skips(SimpleNamespace(dry_run=True))
    assert "would clear 2 roles (a: 1, b: 1)" in capsys.readouterr().out
    assert {s: r.go_skip for s, r in (await _roles()).items()}["left"] == GAP
    await cli._clear_stale_go_skips(SimpleNamespace(dry_run=False))
    assert "cleared 2 roles (a: 1, b: 1)" in capsys.readouterr().out
    skips = {s: r.go_skip for s, r in (await _roles()).items()}
    assert skips == {"left": None, "fixed": None, "true": GAP, "failed": FAILED}


SENT_BACK = packets.SENT_BACK_SKIP


async def test_the_research_sent_back_reason_is_the_one_launch_writes():
    from app.jobsearch.browser import go

    async def start(*a, **k):
        raise packets.SentBack("whatever")

    class Role:
        id = 1

    real = packets.start
    packets.start = start
    try:
        assert await go._ask(None, OWNER, Role()) == SENT_BACK
    finally:
        packets.start = real


async def test_researching_a_role_again_clears_the_research_sent_back_skip(js):
    from app.jobsearch.research import analysis

    await _add(role("a", fit=9, go_skip=SENT_BACK))
    async with db.session_factory()() as session:
        found = (await session.scalars(select(Opportunity))).one()
        await analysis.action_settle(session, None, {"role": str(found.id)})
        await session.commit()
    assert (await _roles())["a"].go_skip is None


async def test_researching_a_role_keeps_a_skip_that_is_still_true(js):
    from app.jobsearch.research import analysis

    await _add(role("a", fit=9, go_skip=GAP))
    async with db.session_factory()() as session:
        found = (await session.scalars(select(Opportunity))).one()
        await analysis.action_settle(session, None, {"role": str(found.id)})
        await session.commit()
    assert (await _roles())["a"].go_skip == GAP


async def test_the_command_clears_a_research_sent_back_skip_once_the_research_is_current(js, capsys, monkeypatch):
    monkeypatch.setattr(cli.settings, "owner", OWNER)
    await _add(role("current", fit=9, go_skip=SENT_BACK), role("waiting", fit=8, go_skip=SENT_BACK))
    async with db.session_factory()() as session:
        for r in (await session.scalars(select(Opportunity))).all():
            r.analysis_state = "done" if r.slug == "current" else "waiting"
        await session.commit()
    await cli._clear_stale_go_skips(SimpleNamespace(dry_run=False))
    skips = {s: r.go_skip for s, r in (await _roles()).items()}
    assert skips == {"current": None, "waiting": SENT_BACK}
