"""The ledger of the hand-outs locks the rows a take-next call takes in id order and waits for none (`as_read.lock`),
like every other pass, so a take-next call is never half of a cycle of waits with one. A line says only its table and
what a row must still satisfy; the ledger takes the lock. Each of the five lines is run through `take_next` over
made-up ids: the statement the ledger sends for it is the one that says in what order, and no row is needed for it."""

import dataclasses
import re
import uuid
from datetime import UTC, datetime

import pytest

from app import db
from app.jobsearch.gaps import coach as gap_coach
from app.jobsearch.interviews import coach as interview_coach
from app.jobsearch.mail import haiku
from app.jobsearch.research import analysis
from app.jobsearch.research import employer as employer_research
from app.jobsearch.work import hand_outs
from tests.support import OWNER, reads_on

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
IDS = [uuid.UUID(int=n) for n in (7, 3, 9, 1, 5)]  # asked in no order


async def _analysis_line(monkeypatch) -> hand_outs.Line:
    """The analyst's line, which `claim_next` builds around its own give-up: taken from the ledger's call."""
    taken: list[hand_outs.Line] = []

    async def take_next(session, owner, kind, lines, **kw):
        taken.extend(lines)
        return []

    with monkeypatch.context() as patched:
        patched.setattr(hand_outs, "take_next", take_next)
        async with db.session_factory()() as session:
            await analysis.claim_next(session, OWNER, NOW)
    (line,) = taken
    return line


@pytest.mark.parametrize("which", ["researcher", "coach_job", "mail", "interview_prep", "analyst"])
async def test_a_line_reads_its_rows_in_id_order_and_asks_for_no_lock(js, monkeypatch, which):
    line = {"researcher": lambda: employer_research.LINE, "coach_job": lambda: gap_coach.LINE,
            "mail": lambda: haiku.LINE, "interview_prep": lambda: interview_coach._PREPS}.get(which)
    line = line() if line else await _analysis_line(monkeypatch)

    async def free(session, owner, now, *, mine):
        return IDS

    async def take(session, row, now):
        return False

    asked = dataclasses.replace(line, free=free, take=take)  # the line's own table and condition, over made-up ids
    with reads_on(line.model.__tablename__) as reads:
        async with db.session_factory()() as session:
            assert await hand_outs.take_next(session, OWNER, "any", [asked], now=NOW, session_id=None, claim=None,
                                             batch=len(IDS)) == []
            await session.rollback()
    (statement, locked), = reads
    assert re.search(rf"ORDER BY {line.model.__tablename__}\.id\b", statement), statement
    assert "FOR UPDATE" not in statement and "SKIP LOCKED" not in statement, statement
    assert sorted(locked) == sorted(IDS)
