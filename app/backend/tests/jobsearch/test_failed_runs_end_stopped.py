"""A run that fails still ends as it does today (build_packet, build_library, find_gaps, research_employer and
prepare_interview .bpmn draw a "Closed as failed" end the engine never takes): the app closes it from outside the
engine, so the process ends cancelled with the reason, and never reaches a drawn end. The mail reader's 12-hour wait is
drawn as a timer on its step: a run opens one such wait, and the app's own clock (`give_up_waiting`) still ends the wait
through the match step. Each test copies the setup of the test it names; the drawing changes none of them
(docs/PRD.md paths 10, 14, 15.2, 36, 38, 43). Everything is made up."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from process_engine.models import CANCELLED, COMPLETED, OPEN, ProcHistory, ProcInstance, ProcWait
from sqlalchemy import select

from app import db
from app.jobsearch import process, stages
from app.jobsearch.gaps import runs as find_gaps
from app.jobsearch.interviews import coach
from app.jobsearch.mail import sync as mail_sync
from app.jobsearch.models import Employer, GapRun, InterviewPrep, Mission, MissionRun, Packet, StoryLibrary
from app.jobsearch.research import employer as employer_research
from app.jobsearch.work import hand_outs
from tests.builders import packet_setup, run_seats
from tests.jobsearch.coach_builders import TODAY as COACH_DAY
from tests.jobsearch.test_coach import _world as coach_world  # pyright: ignore[reportPrivateUsage]
from tests.jobsearch.test_employer_research import (  # pyright: ignore[reportPrivateUsage]
    _employer,
    _send,
)
from tests.jobsearch.test_employer_research import (
    _world as employer_world,
)
from tests.jobsearch.test_find_gaps import _row, _start, _stock, _take, client  # noqa: F401  (client is a fixture)
from tests.jobsearch.test_mail_sync import (  # noqa: F401  (mailbox is a fixture)
    GLOBEX,
    UNCLEAR,
    _roles,
    mail,
    mailbox,
)
from tests.jobsearch.test_story_library import LETTERS, _documents
from tests.support import OWNER

DRAWN_ENDS = {"End_failed"}


async def _ended(instance_id: uuid.UUID) -> tuple[str, str | None, list[str]]:
    """How a run ended: its status, its reason, and every step it passed through."""
    async with db.session_factory()() as session:
        instance = await session.get(ProcInstance, instance_id)
        rows = (await session.scalars(select(ProcHistory).where(ProcHistory.instance_id == instance_id)
                                      .order_by(ProcHistory.seq))).all()
        assert instance is not None
        return instance.status, instance.error, [r.to_element for r in rows if r.to_element]


def _stopped(ended: tuple[str, str | None, list[str]]) -> None:
    status, error, passed = ended
    assert status == CANCELLED and error, "the run is stopped, with the reason"
    assert not DRAWN_ENDS & set(passed), "the engine never reaches the drawn failed end"


@pytest.fixture(autouse=True)
def _coach_day(monkeypatch):
    monkeypatch.setattr(coach, "owner_today", lambda: COACH_DAY)


async def test_a_packet_whose_seat_fails_every_try_ends_stopped_with_the_reason(js):
    """test_packet_writer: a reply that is not JSON is a failed try and three close the packet as failed."""
    from app.jobsearch.writing import packets

    role_id = await packet_setup()
    async with db.session_factory()() as session:
        packet = await packets.start(session, OWNER, role_id, "")
        await session.commit()
        packet_id = packet.id
    await run_seats({"Compose": "I could not do this."})
    async with db.session_factory()() as session:
        row = await session.get(Packet, packet_id)
        assert row is not None and row.status == "failed"
        instance_id = row.instance_id
    ended = await _ended(instance_id)
    _stopped(ended)


async def test_a_library_part_that_fails_every_try_ends_the_whole_build_stopped_with_the_reason(js):
    """test_story_library: a part that fails every try closes the build."""
    await _documents()
    await run_seats({"Write_lines": "no json here", "Write_letters": LETTERS})
    async with db.session_factory()() as session:
        (row,) = (await session.scalars(select(StoryLibrary).where(StoryLibrary.owner == OWNER))).all()
        assert row.status == "failed"
        build = row.instance_id
    _stopped(await _ended(build))


async def test_a_find_gaps_run_no_session_finishes_ends_stopped_with_the_reason(client):  # noqa: F811
    """test_find_gaps: a run a session never finishes fails."""
    await _stock(10)
    run_id = await _start()
    await _take()
    async with db.session_factory()() as session:
        await find_gaps.expire(session, OWNER, datetime.now(UTC) + timedelta(minutes=21))
        await session.commit()
    row: GapRun = await _row(run_id)
    assert row.state == "failed" and row.error == find_gaps.NOT_FINISHED
    _stopped(await _ended(row.instance_id))


async def test_an_employer_research_no_session_finishes_ends_stopped_with_the_reason(js):
    """test_employer_research: an employer taken over and over and never answered fails."""
    await employer_world()
    stuck = await _employer("Acme")
    await _send(stuck.id)
    for hours in range(hand_outs.MAX_CLAIMS):
        async with db.session_factory()() as session:
            assert await employer_research.claim_next(session, OWNER, datetime.now(UTC) + timedelta(hours=hours))
            await session.commit()
    async with db.session_factory()() as session:
        await employer_research.claim_next(session, OWNER, datetime.now(UTC) + timedelta(hours=9))
        await session.commit()
        row = await session.get(Employer, stuck.id)
        assert row is not None and row.research_state == employer_research.FAILED
        instance_id = row.research_instance_id
    _stopped(await _ended(instance_id))


async def test_an_interview_prep_no_session_finishes_ends_stopped_with_the_reason(js):
    """test_coach: a write nobody finished fails."""
    _, _, prep_id = await coach_world()
    async with db.session_factory()() as session:
        reply = await coach.task_reply(session, OWNER, "task", None, session_id="s1")
        assert reply
        await session.commit()
    async with db.session_factory()() as session:
        prep = await session.get(InterviewPrep, prep_id)
        assert prep is not None
        prep.taken_at = datetime.now(UTC) - timedelta(hours=5)
        assert await coach.settle_stuck(session, OWNER) == 1
        await session.commit()
    async with db.session_factory()() as session:
        prep = await session.get(InterviewPrep, prep_id)
        assert prep is not None and prep.state == "failed"
        instance_id = prep.instance_id
    _stopped(await _ended(instance_id))


async def test_a_mail_run_opens_one_twelve_hour_wait_and_the_apps_clock_still_ends_it(js, mailbox):  # noqa: F811
    """test_mail_sync: a reader that never answers ends the night and the mail is read again."""
    from app.jobsearch.missions import core as missions
    from app.jobsearch.missions.kinds import read_mail  # noqa: F401  (registers the mission's kind)

    await _roles((GLOBEX, "Product Lead", stages.READY_TO_LAUNCH))
    mailbox.messages = [mail(21, "Following up", "Pat <pat@globexcorp.example>", UNCLEAR,
                             day=datetime.now(UTC).date() - timedelta(days=2))]
    async with db.session_factory()() as session:
        await missions.ensure(session, OWNER)
        mission = (await session.scalars(select(Mission).where(Mission.key == missions.READ_MAIL))).one()
        run = MissionRun(owner=OWNER, mission_id=mission.id, started_by="schedule", outcome="running",
                         started={"mail": True}, summary="", started_at=datetime.now(UTC) - timedelta(hours=13))
        session.add(run)
        await session.commit()
        run_id = run.id
    instance_id = await mail_sync.drive(OWNER, datetime.now(UTC).date() - timedelta(days=3), run_id)
    async with db.session_factory()() as session:
        waits = (await session.scalars(select(ProcWait).where(
            ProcWait.instance_id == instance_id, ProcWait.status == OPEN, ProcWait.kind == "boundary_timer"))).all()
        assert [w.element_id for w in waits] == ["B_reader_late"]
        run = await session.get(MissionRun, run_id)
        assert await mail_sync.give_up_waiting(session, run, datetime.now(UTC)) is True
        await session.commit()
    await mail_sync._close(run_id, OWNER)  # pyright: ignore[reportPrivateUsage]
    status, _, passed = await _ended(instance_id)
    assert status == COMPLETED and "Match_to_roles" in passed and "B_reader_late" not in passed
    assert process.MAIL_STEP == "Mail_reader_reads"
